from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from pathlib import Path

from pydantic import Field, model_validator

from ego.agents.base import AgentInput
from ego.agents.runtime import AgentRuntime, NoParticipantsError
from ego.events import WorkEventType
from ego.models import (
    AcceptedDecisionPackage,
    CritiqueDispositionAction,
    FinalPlanAssembly,
    ImplementationPlan,
    PlanFormat,
    PlanState,
    RunStatus,
)
from ego.participants import Participant
from ego.planning.artifacts import PlanArtifactWriter
from ego.planning.assembly import (
    apply_source_contract,
    blocking_issues,
    merge_variants,
    normalize_final_assembly,
    normalize_joint_draft,
)
from ego.planning.collaboration import PlanCollaboration
from ego.planning.context import (
    WorkspaceContextBuilder,
    fallback_workspace_context,
)
from ego.planning.evidence import (
    freeze_discovered_evidence,
    stale_workspace_evidence_ids,
)
from ego.planning.sources import MAX_PLAN_BRIEF_CHARS, resolve_plan_sources
from ego.storage import Database
from ego.workspace import observe_git


class PlanInput(AgentInput):
    decision_ids: list[str] = Field(default_factory=list)
    brief: str | None = None
    brief_file: Path | None = None
    format: PlanFormat = PlanFormat.MARKDOWN
    destination: str | None = None

    @model_validator(mode="after")
    def validate_source(self) -> PlanInput:
        source_count = sum(
            (
                bool(self.decision_ids),
                self.brief is not None,
                self.brief_file is not None,
            )
        )
        if source_count != 1:
            raise ValueError("Plan requires exactly one source: text, --decision, or --file")
        if self.brief is not None:
            instruction = self.brief.strip()
            if not instruction:
                raise ValueError("plan text cannot be empty")
            if len(instruction) > MAX_PLAN_BRIEF_CHARS:
                raise ValueError(f"plan text exceeds the {MAX_PLAN_BRIEF_CHARS}-character limit")
            self.brief = instruction
        return self


@dataclass(frozen=True)
class PlanOutcome:
    plan: ImplementationPlan


class PlanWorkflow:
    workflow_id = "plan"

    def __init__(
        self,
        database: Database,
        participants: dict[str, Participant],
        writer: PlanArtifactWriter | None = None,
        context_builder: WorkspaceContextBuilder | None = None,
    ) -> None:
        self.database = database
        self.runtime = AgentRuntime(database, participants)
        self.collaboration = PlanCollaboration(self.runtime)
        self.writer = writer or PlanArtifactWriter()
        self.context_builder = context_builder or WorkspaceContextBuilder()

    async def plan(self, request: PlanInput) -> PlanOutcome:
        if request.format is not PlanFormat.MARKDOWN:
            raise ValueError(f"unsupported plan format: {request.format.value}")
        selected_ids = request.participant_ids or list(self.runtime.participants)
        if len(selected_ids) < 2:
            raise ValueError("Plan requires at least two selected participants")
        sources = resolve_plan_sources(
            self.database,
            workspace=request.workspace,
            decision_ids=request.decision_ids,
            brief=request.brief,
            brief_file=request.brief_file,
        )
        decisions = [source for source in sources if isinstance(source, AcceptedDecisionPackage)]
        decision_ids = [source.decision_id for source in decisions]
        git_start = await observe_git(request.workspace)
        try:
            workspace_context = await self.context_builder.build(
                workspace=request.workspace,
                question=request.question,
                sources=sources,
                git_head=git_start.head,
                git_status=git_start.status,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            workspace_context = fallback_workspace_context(
                question=request.question,
                git_head=git_start.head,
                git_status=git_start.status,
                reason=f"context builder failed ({type(error).__name__})",
            )
        run_id = self.database.create_run(
            command=request.command,
            question=request.question,
            workspace=request.workspace,
            git_head=git_start.head,
            git_status=git_start.status,
            agent_id="plan",
            workflow_id=self.workflow_id,
            result_kind="implementation_plan",
        )
        self.database.set_run_status(run_id, RunStatus.RUNNING)
        try:
            active = await self.runtime.active_participants(run_id, selected_ids)
            if len(active) < 2:
                raise NoParticipantsError(
                    "Plan requires at least two participants that pass availability checks"
                )
            candidates = await self.collaboration.independent_plans(
                run_id,
                request,
                sources,
                workspace_context,
                active,
            )
            if len(candidates) < 2:
                raise NoParticipantsError(
                    "fewer than two participants produced valid independent plans"
                )
            warnings = [
                f"{participant_id} did not produce a valid independent plan."
                for participant_id in active
                if participant_id not in candidates
            ]
            if not workspace_context.manifest.sufficient:
                warnings.append(
                    "The initial workspace context was incomplete; independent participants "
                    "used protected workspace reads"
                    + (
                        f": {workspace_context.manifest.fallback_reason}."
                        if workspace_context.manifest.fallback_reason
                        else "."
                    )
                )
            collaborative_context, candidates, evidence_issues = freeze_discovered_evidence(
                request.workspace,
                workspace_context,
                candidates,
            )
            joint_author, final_author = self.runtime.rotating_pair(run_id, candidates)
            joint = await self.collaboration.joint_draft(
                run_id,
                request,
                sources,
                collaborative_context,
                candidates,
                joint_author,
                active[joint_author],
            )
            joint, coverage_warnings = normalize_joint_draft(joint, candidates)
            warnings.extend(coverage_warnings)
            audits, missing_audits = await self.collaboration.author_audits(
                run_id,
                request,
                sources,
                collaborative_context,
                candidates,
                joint,
                active,
            )
            warnings.extend(
                f"{participant_id} did not produce a valid audit."
                for participant_id in missing_audits
            )
            final_assembly: FinalPlanAssembly | None = None
            if any(audit.criticisms for audit in audits.values()):
                final_assembly = await self.collaboration.final_assembly(
                    run_id,
                    request,
                    sources,
                    collaborative_context,
                    joint,
                    audits,
                    final_author,
                    active[final_author],
                )
                if final_assembly is None:
                    warnings.append(
                        f"{final_author} did not produce a valid final assembly; "
                        "the joint candidate was preserved."
                    )
            final_assembly, assembly_warnings = normalize_final_assembly(
                final_assembly,
                audits,
                joint,
            )
            warnings.extend(assembly_warnings)
            resolved_variant_ids = (
                []
                if final_assembly is None
                else [
                    variant_id
                    for disposition in final_assembly.critique_dispositions
                    if disposition.action is CritiqueDispositionAction.APPLIED
                    for variant_id in disposition.resolved_variant_ids
                ]
            )
            variants = merge_variants(
                joint.variants,
                [] if final_assembly is None else final_assembly.variants,
                resolved_variant_ids,
            )
            draft = joint.draft if final_assembly is None else final_assembly.draft
            unresolved = blocking_issues(
                joint,
                audits,
                final_assembly,
                missing_audits,
                variants,
            )
            unresolved.extend(
                "Independent plan contains unsupported workspace evidence: " + issue
                for issue in evidence_issues
            )
            try:
                stale_evidence_ids = await stale_workspace_evidence_ids(
                    request.workspace,
                    collaborative_context.manifest,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                stale_evidence_ids = []
                unresolved.append(
                    "Workspace evidence could not be revalidated after collaboration "
                    f"({type(error).__name__})."
                )
            if stale_evidence_ids:
                collaborative_context = collaborative_context.model_copy(
                    update={
                        "manifest": collaborative_context.manifest.model_copy(
                            update={"stale_evidence_ids": stale_evidence_ids}
                        )
                    }
                )
                unresolved.append(
                    "Workspace evidence changed after planning began; rerun Plan against "
                    f"the current workspace ({len(stale_evidence_ids)} stale fragments)."
                )
            draft = apply_source_contract(
                draft,
                decisions,
                variants,
            )
            plan_id = str(uuid.uuid4())
            artifact = self.writer.write(
                workspace=request.workspace,
                plan_id=plan_id,
                run_id=run_id,
                draft=draft,
                sources=sources,
                destination=None if request.destination is None else Path(request.destination),
                workspace_git_head=git_start.head,
                participant_ids=sorted(candidates),
                variants=variants,
                blocking_issues=unresolved,
                workspace_context_manifest=collaborative_context.manifest,
            )
            git_end = await observe_git(request.workspace)
            if git_start.head != git_end.head:
                warnings.append("The workspace Git revision changed while the plan was generated.")
            if git_start.status != git_end.status:
                warnings.append("The workspace Git status changed while the plan was generated.")
            plan = ImplementationPlan(
                plan_id=plan_id,
                run_id=run_id,
                state=PlanState.DRAFT,
                format=request.format,
                workspace=request.workspace,
                decision_ids=decision_ids,
                sources=sources,
                workspace_context_manifest=collaborative_context.manifest,
                participant_plans=candidates,
                joint_draft=joint,
                audits=audits,
                final_assembly=final_assembly,
                variants=variants,
                blocking_issues=unresolved,
                artifact_path=artifact.path.relative_to(request.workspace),
                workspace_git_head=git_start.head,
                manifest_sha256=artifact.manifest_sha256,
                plan_sha256=artifact.plan_sha256,
                draft=draft,
                warnings=warnings,
            )
            self.database.set_run_status(
                run_id,
                RunStatus.COMPLETED,
                result=plan,
                git_head=git_end.head,
                git_status=git_end.status,
            )
            self.database.create_plan(plan)
            self.database.add_event(
                run_id,
                WorkEventType.RESULT_CREATED,
                {"result_kind": "implementation_plan", "status": RunStatus.COMPLETED.value},
            )
            return PlanOutcome(plan=plan)
        except KeyboardInterrupt, asyncio.CancelledError:
            self.database.set_run_status(run_id, RunStatus.INTERRUPTED)
            raise
        except BaseException:
            self.database.set_run_status(run_id, RunStatus.FAILED)
            raise
