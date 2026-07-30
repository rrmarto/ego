from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from ego.agents.base import AgentInput
from ego.agents.runtime import AgentRuntime, NoParticipantsError
from ego.events import WorkEventType
from ego.models import (
    AcceptedDecisionPackage,
    HumanPlanBrief,
    ImplementationPlan,
    PlanDraft,
    PlanFormat,
    PlanPhase,
    PlanSource,
    PlanState,
    RunStatus,
    ToolPolicy,
    TurnRequest,
)
from ego.participants import Participant
from ego.planning.artifacts import PlanArtifactWriter
from ego.storage import Database
from ego.workspace import observe_git

MAX_PLAN_BRIEF_CHARS = 12_000
MAX_PLAN_BRIEF_BYTES = 48_000


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
                raise ValueError(
                    f"plan text exceeds the {MAX_PLAN_BRIEF_CHARS}-character limit"
                )
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
    ) -> None:
        self.database = database
        self.runtime = AgentRuntime(database, participants)
        self.writer = writer or PlanArtifactWriter()

    async def plan(self, request: PlanInput) -> PlanOutcome:
        if request.format is not PlanFormat.MARKDOWN:
            raise ValueError(f"unsupported plan format: {request.format.value}")
        if len(request.participant_ids) != 1:
            raise ValueError("Plan requires exactly one explicitly selected participant")
        sources = self._resolve_sources(request)
        decisions = [
            source for source in sources if isinstance(source, AcceptedDecisionPackage)
        ]
        decision_ids = [source.decision_id for source in decisions]
        git_start = await observe_git(request.workspace)
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
            active = await self.runtime.active_participants(run_id, request.participant_ids)
            if not active:
                raise NoParticipantsError("selected participant did not pass availability checks")
            participant_id, participant = next(iter(active.items()))
            results = await self.runtime.parallel(
                run_id,
                PlanPhase.DRAFT,
                {
                    participant_id: (
                        participant,
                        TurnRequest(
                            run_id=run_id,
                            phase=PlanPhase.DRAFT,
                            question=request.question,
                            workspace=request.workspace,
                            agent_id="plan",
                            workflow_id=self.workflow_id,
                            tool_policy=ToolPolicy.local_read_only(),
                            plan_sources=sources,
                        ),
                    )
                },
            )
            result = results.get(participant_id)
            if result is None or not isinstance(result.payload, PlanDraft):
                raise NoParticipantsError("selected participant did not produce a valid plan")
            source_constraints = _unique(
                value for decision in decisions for value in decision.constraints
            )
            source_non_goals = _unique(
                value for decision in decisions for value in decision.non_goals
            )
            draft = result.payload.model_copy(
                update={
                    "constraints": _unique(
                        [*source_constraints, *result.payload.constraints]
                    ),
                    "non_goals": _unique([*source_non_goals, *result.payload.non_goals]),
                }
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
            )
            git_end = await observe_git(request.workspace)
            warnings = (
                ["The workspace Git revision changed while the plan was generated."]
                if git_start.head != git_end.head
                else []
            )
            plan = ImplementationPlan(
                plan_id=plan_id,
                run_id=run_id,
                state=PlanState.DRAFT,
                format=request.format,
                workspace=request.workspace,
                decision_ids=decision_ids,
                sources=sources,
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

    def _resolve_sources(self, request: PlanInput) -> list[PlanSource]:
        if request.decision_ids:
            decision_ids = list(dict.fromkeys(request.decision_ids))
            return [
                self.database.get_accepted_decision_package(
                    decision_id,
                    workspace=request.workspace,
                )
                for decision_id in decision_ids
            ]
        if request.brief is not None:
            return [self._human_brief(request.brief, source_kind="text")]
        if request.brief_file is None:
            raise ValueError("Plan source is missing")
        workspace = request.workspace.resolve()
        candidate = request.brief_file
        if not candidate.is_absolute():
            candidate = workspace / candidate
        try:
            source_path = candidate.resolve(strict=True)
            relative_path = source_path.relative_to(workspace)
        except (FileNotFoundError, ValueError) as error:
            raise ValueError("plan source file must exist inside the workspace") from error
        if not source_path.is_file():
            raise ValueError("plan source file must be a regular file")
        if source_path.stat().st_size > MAX_PLAN_BRIEF_BYTES:
            raise ValueError(
                f"plan source file exceeds the {MAX_PLAN_BRIEF_BYTES}-byte limit"
            )
        try:
            instruction = source_path.read_text(encoding="utf-8").strip()
        except UnicodeDecodeError as error:
            raise ValueError("plan source file must be UTF-8 text") from error
        if not instruction:
            raise ValueError("plan source file cannot be empty")
        if len(instruction) > MAX_PLAN_BRIEF_CHARS:
            raise ValueError(
                f"plan source file exceeds the {MAX_PLAN_BRIEF_CHARS}-character limit"
            )
        return [
            self._human_brief(
                instruction,
                source_kind="file",
                source_path=str(relative_path),
            )
        ]

    @staticmethod
    def _human_brief(
        instruction: str,
        *,
        source_kind: Literal["text", "file"],
        source_path: str | None = None,
    ) -> HumanPlanBrief:
        return HumanPlanBrief(
            source_kind=source_kind,
            brief_id=str(uuid.uuid4()),
            instruction=instruction,
            source_path=source_path,
            created_at=datetime.now(UTC).isoformat(),
        )


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))
