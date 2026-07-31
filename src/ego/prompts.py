from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from pydantic import BaseModel

from ego.models import (
    CritiqueDispositionAction,
    FinalPlanAssembly,
    InvestigationDraft,
    InvestigationPhase,
    InvestigationReviewBundle,
    InvestigationSynthesis,
    JointPlanDraft,
    PeerReviewBundle,
    Phase,
    PlanAudit,
    PlanDraft,
    PlanPhase,
    Position,
    Synthesis,
    TurnRequest,
    WorkStage,
)


def response_model(phase: WorkStage) -> type[BaseModel]:
    if isinstance(phase, PlanPhase):
        if phase is PlanPhase.INDEPENDENT:
            return PlanDraft
        if phase is PlanPhase.JOINT_DRAFT:
            return JointPlanDraft
        if phase is PlanPhase.AUTHOR_AUDIT:
            return PlanAudit
        return FinalPlanAssembly
    if isinstance(phase, InvestigationPhase):
        if phase in {InvestigationPhase.INDEPENDENT, InvestigationPhase.REVISION}:
            return InvestigationDraft
        if phase is InvestigationPhase.PEER_CHALLENGE:
            return InvestigationReviewBundle
        return InvestigationSynthesis
    if phase in {Phase.INDEPENDENT, Phase.REVISION}:
        return Position
    if phase is Phase.PEER_REVIEW:
        return PeerReviewBundle
    return Synthesis


def response_schema(phase: WorkStage) -> dict[str, object]:
    schema = response_model(phase).model_json_schema()
    return cast(dict[str, object], _strict_schema(schema))


def validate_response(request: TurnRequest, response: BaseModel) -> None:
    if isinstance(request.phase, PlanPhase):
        _validate_plan_response(request, response)
        return
    if isinstance(request.phase, InvestigationPhase):
        _validate_investigation_response(request, response)
        return
    if isinstance(response, Synthesis):
        _validate_synthesis_response(request, response)
        return
    if request.phase is not Phase.REVISION or not isinstance(response, Position):
        return
    if request.own_position is None:
        raise ValueError("position revision requires the participant's previous position")
    if len(response.change_reason.strip()) < 12:
        raise ValueError("position revision requires a substantive change reason")
    if len(response.confidence_reason.strip()) < 12:
        raise ValueError("position revision requires a substantive confidence reason")
    if response.changed_position:
        return

    previous_ids = {argument.id for argument in request.own_position.arguments}
    revised_ids = {argument.id for argument in response.arguments}
    if previous_ids and not previous_ids.intersection(revised_ids):
        raise ValueError(
            "a maintained position must preserve at least one prior argument id; "
            "otherwise mark changed_position true and explain the change"
        )


def _validate_investigation_response(request: TurnRequest, response: BaseModel) -> None:
    if isinstance(response, InvestigationReviewBundle):
        known = set(request.peer_investigations)
        unknown = sorted(
            {
                review.target_participant
                for review in response.reviews
                if review.target_participant not in known
            }
        )
        if unknown:
            raise ValueError("review referenced unknown participants: " + ", ".join(unknown))
        return
    if isinstance(response, InvestigationDraft):
        if not response.findings and not response.hypotheses and not response.unknowns:
            raise ValueError("investigation must contain findings, hypotheses, or unknowns")
        return
    if isinstance(response, InvestigationSynthesis):
        if not (
            response.facts
            or response.probable_causes
            or response.disputed_findings
            or response.unknowns
        ):
            raise ValueError("investigation synthesis must contain a material result")


def _validate_synthesis_response(request: TurnRequest, response: Synthesis) -> None:
    placeholders = {"test", "placeholder", "tbd"}
    errors: list[str] = []
    if response.recommendation.strip().casefold() in placeholders:
        errors.append("synthesis requires a real recommendation, not a placeholder")
    if len(response.confidence_reason.strip()) < 12:
        errors.append("synthesis requires a substantive confidence reason")
    known_argument_ids: set[str] | None = None
    if request.phase is Phase.SYNTHESIS:
        known_argument_ids = {
            argument.id
            for position in request.peer_positions.values()
            for argument in position.arguments
        }
    elif request.phase is Phase.RECONCILIATION:
        known_argument_ids = {
            argument_id
            for synthesis in request.syntheses.values()
            for argument_id in synthesis.supporting_argument_ids
        }
    if known_argument_ids is not None:
        unknown_argument_ids = sorted(
            set(response.supporting_argument_ids) - known_argument_ids
        )
        if unknown_argument_ids:
            errors.append(
                "synthesis referenced unknown argument ids: "
                + ", ".join(unknown_argument_ids)
            )
    if request.phase is Phase.RECONCILIATION and response.equivalent_to_peer is None:
        errors.append("reconciliation requires an explicit equivalence decision")
    if errors:
        raise ValueError("; ".join(errors))


def _validate_plan_response(request: TurnRequest, response: BaseModel) -> None:
    evidence_ids = (
        {
            item.id
            for item in request.workspace_context.manifest.evidence
        }
        if request.workspace_context is not None
        else set()
    )
    if isinstance(response, PlanDraft):
        _validate_plan_draft(response, evidence_ids=evidence_ids)
        return
    if isinstance(response, JointPlanDraft):
        _validate_plan_draft(response.draft, evidence_ids=evidence_ids)
        _validate_unique_ids(
            [item.source_task_id for item in response.coverage],
            "plan coverage source task",
        )
        _validate_unique_ids([item.id for item in response.variants], "plan variant")
        return
    if isinstance(response, PlanAudit):
        _validate_unique_ids([item.id for item in response.criticisms], "plan critique")
        return
    if isinstance(response, FinalPlanAssembly):
        _validate_plan_draft(response.draft, evidence_ids=evidence_ids)
        _validate_unique_ids(
            [item.critique_id for item in response.critique_dispositions],
            "critique disposition",
        )
        _validate_unique_ids([item.id for item in response.variants], "plan variant")
        _validate_final_plan_assembly(request, response)
        return
    raise ValueError("unsupported structured Plan response")


def _validate_final_plan_assembly(
    request: TurnRequest,
    response: FinalPlanAssembly,
) -> None:
    known_tasks = {task.id for task in response.draft.tasks}
    known_variants: set[str] = set()
    joint_tasks: set[str] = set()
    if request.joint_plan is not None:
        joint_tasks.update(task.id for task in request.joint_plan.draft.tasks)
        known_tasks.update(joint_tasks)
        known_variants.update(item.id for item in request.joint_plan.variants)
    critiques = {
        item.id: item
        for audit in request.plan_audits.values()
        for item in audit.criticisms
    }
    resolved_variants = [
        variant_id
        for item in response.critique_dispositions
        for variant_id in item.resolved_variant_ids
    ]
    _validate_unique_ids(resolved_variants, "resolved variant")
    unknown_tasks = sorted(
        {
            task_id
            for item in response.critique_dispositions
            for task_id in item.target_task_ids
            if task_id not in known_tasks
        }
    )
    unknown_variants = sorted(set(resolved_variants) - known_variants)
    returned_variants = {item.id for item in response.variants}
    conflicting_variants = sorted(set(resolved_variants) & returned_variants)
    untargeted = [
        item.critique_id
        for item in response.critique_dispositions
        if item.action is CritiqueDispositionAction.APPLIED
        and not item.target_task_ids
        and not item.target_sections
        and not item.resolved_variant_ids
    ]
    invalid_resolutions = [
        item.critique_id
        for item in response.critique_dispositions
        if item.action is not CritiqueDispositionAction.APPLIED
        and item.resolved_variant_ids
    ]
    unknown_critiques: list[str] = []
    unauthorized_targets: list[str] = []
    for item in response.critique_dispositions:
        critique = critiques.get(item.critique_id)
        if critique is None:
            unknown_critiques.append(item.critique_id)
            continue
        unauthorized_targets.extend(
            f"{item.critique_id}:task:{task_id}"
            for task_id in (
                set(item.target_task_ids)
                & joint_tasks
                - set(critique.candidate_task_ids)
            )
        )
        unauthorized_targets.extend(
            f"{item.critique_id}:section:{section.value}"
            for section in set(item.target_sections) - set(critique.candidate_sections)
        )
        unauthorized_targets.extend(
            f"{item.critique_id}:variant:{variant_id}"
            for variant_id in (
                set(item.resolved_variant_ids)
                - set(critique.candidate_variant_ids)
            )
        )
    errors: list[str] = []
    if unknown_critiques:
        errors.append(
            "unknown critique dispositions: " + ", ".join(sorted(unknown_critiques))
        )
    if unknown_tasks:
        errors.append("unknown disposition target tasks: " + ", ".join(unknown_tasks))
    if unknown_variants:
        errors.append("unknown resolved variants: " + ", ".join(unknown_variants))
    if conflicting_variants:
        errors.append(
            "variants cannot be both resolved and returned: "
            + ", ".join(conflicting_variants)
        )
    if untargeted:
        errors.append(
            "applied dispositions require task, section, or variant targets: "
            + ", ".join(untargeted)
        )
    if invalid_resolutions:
        errors.append(
            "only applied dispositions may resolve variants: "
            + ", ".join(invalid_resolutions)
        )
    if unauthorized_targets:
        errors.append(
            "disposition targets were not identified by their critiques: "
            + ", ".join(sorted(unauthorized_targets))
        )
    if errors:
        raise ValueError("; ".join(errors))


def _validate_plan_draft(
    response: PlanDraft,
    *,
    evidence_ids: set[str] | None = None,
) -> None:
    if len(response.title.strip()) < 4 or len(response.objective.strip()) < 12:
        raise ValueError("plan requires a substantive title and objective")
    if not response.tasks:
        raise ValueError("plan requires at least one implementation task")
    task_ids = [task.id.strip() for task in response.tasks]
    if any(not task_id for task_id in task_ids) or len(set(task_ids)) != len(task_ids):
        raise ValueError("plan task identifiers must be non-empty and unique")
    known = set(task_ids)
    unknown_dependencies = sorted(
        {
            dependency
            for task in response.tasks
            for dependency in task.depends_on
            if dependency not in known
        }
    )
    if unknown_dependencies:
        raise ValueError(
            "plan tasks reference unknown dependencies: " + ", ".join(unknown_dependencies)
        )
    unsafe_paths = sorted(
        {
            path
            for task in response.tasks
            for path in task.affected_paths
            if Path(path).is_absolute() or ".." in Path(path).parts
        }
    )
    if unsafe_paths:
        raise ValueError("plan contains unsafe affected paths: " + ", ".join(unsafe_paths))
    if evidence_ids is not None:
        unknown_evidence = sorted(
            {
                evidence_id
                for task in response.tasks
                for evidence_id in task.evidence_ids
                if evidence_id not in evidence_ids
            }
        )
        if unknown_evidence:
            raise ValueError(
                "plan tasks reference unknown workspace evidence: "
                + ", ".join(unknown_evidence)
            )


def _validate_unique_ids(values: list[str], label: str) -> None:
    normalized = [value.strip() for value in values]
    if any(not value for value in normalized) or len(set(normalized)) != len(normalized):
        raise ValueError(f"{label} identifiers must be non-empty and unique")


def _strict_schema(value: object) -> object:
    if isinstance(value, list):
        return [_strict_schema(item) for item in value]
    if not isinstance(value, dict):
        return value

    normalized = {
        key: _strict_schema(item)
        for key, item in value.items()
        if key != "default"
    }
    properties = normalized.get("properties")
    if normalized.get("type") == "object" and isinstance(properties, dict):
        normalized["additionalProperties"] = False
        normalized["required"] = list(properties)
    return normalized


def build_prompt(
    request: TurnRequest,
    *,
    correction: str | None = None,
    previous_response: object | None = None,
) -> str:
    if isinstance(request.phase, PlanPhase):
        return _build_plan_prompt(
            request,
            correction=correction,
            previous_response=previous_response,
        )
    if isinstance(request.phase, InvestigationPhase):
        return _build_investigation_prompt(
            request,
            correction=correction,
            previous_response=previous_response,
        )
    instructions = {
        Phase.INDEPENDENT: (
            "Analyze independently. Inspect relevant files before making repository claims. "
            "Do not infer or imitate other participants. For every critical claim, actively try "
            "to falsify it. When behavior depends on a language, runtime, framework, or tool "
            "version, inspect the repository manifest or version constraint before concluding."
        ),
        Phase.PEER_REVIEW: (
            "Review every peer position. Identify valid points, factual mistakes, unsupported "
            "assumptions, missing evidence, and objectively stronger arguments. Try to disprove "
            "every critical claim instead of treating agreement as corroboration. Check relevant "
            "runtime and manifest constraints for version-sensitive claims."
        ),
        Phase.REVISION: (
            "Reconsider your position using the peer reviews. Change it only for a stronger "
            "argument, disproven assumption, error, or superior evidence. If you maintain it, "
            "preserve the ids of arguments that remain valid."
        ),
        Phase.SYNTHESIS: (
            "Synthesize the strongest supported arguments without voting and without adding new "
            "evidence. Preserve credible alternatives and material disagreement. Never describe "
            "a semantic claim as verified merely because its citation status is valid or because "
            "multiple models repeated it."
        ),
        Phase.RECONCILIATION: (
            "Compare the two syntheses. Set equivalent_to_peer true only when their material "
            "recommendations are equivalent. List every material conflict. Do not force consensus."
        ),
    }
    context = _phase_context(request)
    schema = response_schema(request.phase)
    correction_text = f"\nPrevious response validation error: {correction}\n" if correction else ""
    tool_instruction = (
        "You may read and search the workspace, but must not write files or run project commands."
        if request.phase in {Phase.INDEPENDENT, Phase.PEER_REVIEW}
        else "Use only the structured context below; do not inspect the workspace or use tools."
    )
    return f"""You are a peer in Ego, a decision-only deliberation engine.
You have equal authority with every other participant. {tool_instruction}
You must not use the web, delegate, or implement the recommendation.
Give concise, auditable rationale rather than private chain-of-thought. Every repository-specific
claim should cite a relative file path and exact line range. Respond in {request.language}.
A citation status only confirms that the cited path, lines, and hash match the workspace. It does
not prove that the explanation or claim is semantically correct. Model agreement is not independent
proof. Keep unproven semantic claims explicit in assumptions, risks, or disagreements.
Never return test or placeholder content. Supporting argument ids must exactly match ids supplied
in the structured context.

Phase: {request.phase.value}
Question: {request.question}
Task: {instructions[request.phase]}
{correction_text}
Context:
{json.dumps(context, ensure_ascii=False)}

Return only JSON matching this schema:
{json.dumps(schema, ensure_ascii=False)}
"""


def _build_plan_prompt(
    request: TurnRequest,
    *,
    correction: str | None,
    previous_response: object | None,
) -> str:
    phase = request.phase
    if not isinstance(phase, PlanPhase):
        raise TypeError("plan prompt requires a planning stage")
    correction_text = ""
    if correction and previous_response is not None:
        correction_text = (
            "\nRepair the previous structured plan. Do not inspect again or add scope. "
            f"Resolve only this validation error: {correction}\n"
            "Previous structured response:\n"
            f"{json.dumps(previous_response, ensure_ascii=False)}\n"
        )
    elif correction:
        correction_text = f"\nPrevious response validation error: {correction}\n"
    if phase is PlanPhase.INDEPENDENT and request.tool_policy.read:
        tool_instruction = (
            "The prebuilt workspace context is incomplete. Read and search only the minimum "
            "additional workspace files needed. Do not use web, shell, writes, plugins, MCP, "
            "or delegation."
        )
    else:
        tool_instruction = "Use only the supplied context and no tools."
    context: dict[str, object] | str = "Omitted on correction."
    instructions = _plan_stage_instructions(phase)
    if previous_response is None:
        context = {
            "sources": _compact_plan_sources(
                request,
                include_evidence=phase is PlanPhase.INDEPENDENT,
            )
        }
        if request.workspace_context is not None:
            context["workspace_context"] = _workspace_context_payload(
                request,
                include_content=phase is PlanPhase.INDEPENDENT,
                include_enrichment_content=phase is not PlanPhase.INDEPENDENT,
            )
        if phase is PlanPhase.JOINT_DRAFT:
            context["candidate_plans"] = _candidate_plans(request.plan_candidates)
        elif phase is PlanPhase.AUTHOR_AUDIT:
            context.update(
                {
                    "own_plan": (
                        request.own_plan.model_dump(mode="json")
                        if request.own_plan is not None
                        else None
                    ),
                    "own_task_ids": _own_task_ids(request),
                    "joint_plan": (
                        request.joint_plan.model_dump(mode="json")
                        if request.joint_plan is not None
                        else None
                    ),
                }
            )
        elif phase is PlanPhase.FINAL_ASSEMBLY:
            context.update(
                {
                    "joint_plan": (
                        request.joint_plan.model_dump(mode="json")
                        if request.joint_plan is not None
                        else None
                    ),
                    "audits": {
                        participant_id: audit.model_dump(mode="json")
                        for participant_id, audit in request.plan_audits.items()
                    },
                }
            )
    return f"""You are a collaborative planner in Ego. The supplied direction is frozen: do not
reconsider what product to build or silently make a missing product or architecture decision.
{instructions} {tool_instruction}
Keep output concise, ordered, independently verifiable, and grounded in the supplied context.
Use only relative affected paths. Preserve material disagreement as variants or open questions.
Respond in {request.language}.

Agent: {request.agent_id}
Workflow: {request.workflow_id}
Stage: {request.phase.value}
Task: {request.question}
{correction_text}
Context:
{json.dumps(context, ensure_ascii=False)}

Return only JSON matching this schema:
{json.dumps(response_schema(request.phase), ensure_ascii=False)}
"""


def _plan_stage_instructions(phase: PlanPhase) -> str:
    if phase is PlanPhase.INDEPENDENT:
        return (
            "Create an independent implementation plan. Inspect only the minimum workspace "
            "surface needed. Do not imitate an assumed peer plan. Give every task a short, "
            "stable id. Reference only supplied CTX evidence ids in evidence_ids; record "
            "material context gaps as open questions."
        )
    if phase is PlanPhase.JOINT_DRAFT:
        return (
            "Create one joint candidate from every independent plan. Merge compatible work, "
            "order dependencies, and retain unique useful work. For every source task use the "
            "exact qualified id shown in candidate_plans and provide one coverage disposition. "
            "Never omit a task silently; incompatible approaches become variants. Use supplied "
            "adaptive evidence to resolve factual workspace gaps discovered by the authors; do "
            "not preserve a technical question when that evidence directly answers it."
        )
    if phase is PlanPhase.AUTHOR_AUDIT:
        return (
            "Audit the joint candidate strictly against your own independent plan and the frozen "
            "sources. Report only concrete omissions, incorrect additions or merges, dependency "
            "errors, lost constraints, risks, validation gaps, or variants. Each criticism must "
            "be self-contained and identify the required change. Identify affected existing "
            "tasks in candidate_task_ids, plan-level fields in candidate_sections, and joint "
            "variants in candidate_variant_ids. Check technical claims against supplied adaptive "
            "evidence and flag contradictions or obsolete open questions. Return no criticism "
            "when the joint candidate preserves your material contribution correctly."
        )
    return (
        "Revise the joint candidate using every audit. Return one disposition for every exact "
        "critique id. Apply compatible corrections. A material criticism that cannot be applied "
        "must remain explicit as a variant; never discard it silently. Every applied disposition "
        "must identify each changed task in target_task_ids, each changed plan-level field in "
        "target_sections, and each removed joint variant in resolved_variant_ids. Return every "
        "still-unresolved variant. Existing task, section, and variant targets must have been "
        "identified by the corresponding critique. Preserve all tasks and plan-level fields not "
        "explicitly targeted by an applied disposition. Use supplied adaptive evidence only to "
        "apply or reject audited corrections, never to introduce unrelated scope."
    )


def _compact_plan_sources(
    request: TurnRequest,
    *,
    include_evidence: bool,
) -> list[dict[str, object]]:
    sources: list[dict[str, object]] = []
    for item in request.plan_sources:
        if item.source_kind != "decision":
            sources.append(item.model_dump(mode="json", exclude={"created_at"}))
            continue
        source: dict[str, object] = {
            "source_kind": item.source_kind,
            "decision_id": item.decision_id,
            "question": item.question,
            "conclusion": item.conclusion,
            "conclusion_source": item.conclusion_source,
            "constraints": item.constraints,
            "non_goals": item.non_goals,
            "risks": item.risks,
            "human_note": item.human_note,
        }
        if include_evidence:
            source.update(
                {
                    "rationale": item.rationale,
                    "assumptions": item.assumptions,
                    "evidence": [
                        {
                            key: evidence_value
                            for key, evidence_value in evidence_item.model_dump(
                                mode="json"
                            ).items()
                            if key
                            not in {
                                "file_sha256",
                                "fragment_sha256",
                                "validation_error",
                            }
                        }
                        for evidence_item in item.evidence
                    ],
                }
            )
        sources.append(source)
    return sources


def _workspace_context_payload(
    request: TurnRequest,
    *,
    include_content: bool,
    include_enrichment_content: bool = False,
) -> dict[str, object]:
    context = request.workspace_context
    if context is None:
        return {}
    manifest = context.manifest.model_dump(
        mode="json",
        exclude={"evidence"} if include_content else None,
    )
    value: dict[str, object] = {
        "manifest": manifest,
        "project_map": context.project_map,
    }
    if include_content:
        value["evidence"] = [
            item.model_dump(mode="json")
            for item in context.evidence
        ]
    elif include_enrichment_content and context.manifest.enrichment_evidence_ids:
        enrichment_ids = set(context.manifest.enrichment_evidence_ids)
        value["adaptive_evidence"] = [
            item.model_dump(mode="json")
            for item in context.evidence
            if item.id in enrichment_ids
        ]
    return value


def _candidate_plans(candidates: dict[str, PlanDraft]) -> dict[str, object]:
    result: dict[str, object] = {}
    for participant_id, draft in candidates.items():
        value = draft.model_dump(mode="json")
        tasks = value.get("tasks")
        if isinstance(tasks, list):
            for task in tasks:
                if isinstance(task, dict) and isinstance(task.get("id"), str):
                    task["source_task_id"] = f"{participant_id}:{task['id']}"
        result[participant_id] = value
    return result


def _own_task_ids(request: TurnRequest) -> list[str]:
    if request.own_plan is None or request.plan_author_id is None:
        return []
    return [
        f"{request.plan_author_id}:{task.id}"
        for task in request.own_plan.tasks
    ]


def _build_investigation_prompt(
    request: TurnRequest,
    *,
    correction: str | None = None,
    previous_response: object | None = None,
) -> str:
    phase = request.phase
    if not isinstance(phase, InvestigationPhase):
        raise TypeError("investigation prompt requires an investigation stage")
    instructions = {
        InvestigationPhase.INDEPENDENT: (
            "Inspect the workspace independently. Collect facts, hypotheses, unknowns, and exact "
            "local evidence. Try to falsify each important hypothesis."
        ),
        InvestigationPhase.PEER_CHALLENGE: (
            "Challenge every peer investigation. Identify valid points, unsupported claims, "
            "missing evidence, and hypotheses that were omitted."
        ),
        InvestigationPhase.REVISION: (
            "Revise your investigation after the targeted challenges. Keep, modify, or refute "
            "hypotheses explicitly according to the available evidence."
        ),
        InvestigationPhase.SYNTHESIS: (
            "Consolidate the supplied investigations without adding evidence. Separate facts, "
            "probable causes, disputes, unknowns, and useful next read-only checks."
        ),
        InvestigationPhase.RECONCILIATION: (
            "Reconcile the two supplied reports. Merge matching findings and preserve every "
            "material disputed finding. Treat items as matching only when their material claim, "
            "scope, conditions, and hypothesis state agree. For each matching item, emit it once "
            "and copy its claim or hypothesis wording verbatim from the first synthesis in "
            "Context, while merging distinct evidence and the strongest explanation. Keep items "
            "separate when any material difference remains. Never turn disagreement into "
            "alternatives for the user."
        ),
    }
    correction_text = ""
    if correction and previous_response is not None:
        correction_text = (
            "\nRepair the previous structured response. Do not restart the investigation or "
            "gather new evidence. Preserve every valid conclusion and change only what is "
            f"needed to resolve this validation error: {correction}\n"
            "Previous structured response:\n"
            f"{json.dumps(_without_storage_metadata(previous_response), ensure_ascii=False)}\n"
        )
    elif correction:
        correction_text = f"\nPrevious response validation error: {correction}\n"
    if request.tool_policy.read:
        tool_instruction = (
            "You may only read, glob, grep, and search files inside the target workspace. "
            "Do not use a shell, run commands or tests, write files, access the web or URLs, "
            "load plugins or MCP, or delegate. Search narrowly, batch related reads, and stop "
            "when the cited evidence is sufficient. Do not enumerate the whole workspace or "
            "inspect .git, .venv, node_modules, build, dist, or cache directories unless the "
            "question specifically requires them."
        )
    else:
        tool_instruction = (
            "Use only the structured context below. Do not use any tool or inspect the workspace."
        )
    return f"""You are a peer in Ego's local-only investigation workflow.
You have equal authority with every other participant. {tool_instruction}
Return concise auditable conclusions, not private chain-of-thought. Cite repository claims with a
relative path and exact line range. Return only distinct material items; merge overlapping claims
instead of repeating paraphrases. A verified citation proves only the file fragment's integrity,
not the semantic claim. Respond in {request.language}.

Agent: {request.agent_id}
Workflow: {request.workflow_id}
Stage: {request.phase.value}
Question: {request.question}
Task: {instructions[phase]}
{correction_text}
Context:
{json.dumps(_compact_investigation_context(request), ensure_ascii=False)}

Return only JSON matching this schema:
{json.dumps(response_schema(request.phase), ensure_ascii=False)}
"""


def _compact_investigation_context(request: TurnRequest) -> dict[str, object]:
    compacted = _without_storage_metadata(_investigation_context(request))
    assert isinstance(compacted, dict)
    return compacted


def _without_storage_metadata(value: object) -> object:
    if isinstance(value, list):
        return [_without_storage_metadata(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        key: _without_storage_metadata(item)
        for key, item in value.items()
        if key not in {"file_sha256", "fragment_sha256"}
        and not (key == "validation_error" and item is None)
    }


def _investigation_context(request: TurnRequest) -> dict[str, object]:
    if request.phase is InvestigationPhase.INDEPENDENT:
        return {"workspace": str(request.workspace)}
    if request.phase is InvestigationPhase.PEER_CHALLENGE:
        return {
            "workspace": str(request.workspace),
            "own_investigation": request.own_investigation.model_dump(mode="json")
            if request.own_investigation
            else None,
            "peer_investigations": {
                key: value.model_dump(mode="json")
                for key, value in request.peer_investigations.items()
            },
        }
    if request.phase is InvestigationPhase.REVISION:
        return {
            "workspace": str(request.workspace),
            "own_investigation": request.own_investigation.model_dump(mode="json")
            if request.own_investigation
            else None,
            "peer_investigations": {
                key: value.model_dump(mode="json")
                for key, value in request.peer_investigations.items()
            },
            "reviews": {
                key: [item.model_dump(mode="json") for item in value]
                for key, value in request.investigation_reviews.items()
            },
        }
    if request.phase is InvestigationPhase.SYNTHESIS:
        return {
            "investigations": {
                key: value.model_dump(mode="json")
                for key, value in request.peer_investigations.items()
            }
        }
    return {
        "syntheses": {
            key: value.model_dump(mode="json")
            for key, value in request.investigation_syntheses.items()
        }
    }


def _phase_context(request: TurnRequest) -> dict[str, object]:
    if request.phase is Phase.INDEPENDENT:
        return {"workspace": str(request.workspace)}
    if request.phase is Phase.PEER_REVIEW:
        return {
            "workspace": str(request.workspace),
            "own_position": request.own_position.model_dump(mode="json")
            if request.own_position
            else None,
            "peer_positions": {
                key: value.model_dump(mode="json") for key, value in request.peer_positions.items()
            },
        }
    if request.phase is Phase.REVISION:
        return {
            "own_position": request.own_position.model_dump(mode="json")
            if request.own_position
            else None,
            "peer_positions": {
                key: value.model_dump(mode="json") for key, value in request.peer_positions.items()
            },
            "peer_reviews": {
                key: [item.model_dump(mode="json") for item in value]
                for key, value in request.peer_reviews.items()
            },
        }
    if request.phase is Phase.SYNTHESIS:
        return {
            "peer_positions": {
                key: value.model_dump(mode="json") for key, value in request.peer_positions.items()
            }
        }
    return {
        "syntheses": {
            key: value.model_dump(mode="json") for key, value in request.syntheses.items()
        }
    }
