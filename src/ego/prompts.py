from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from pydantic import BaseModel

from ego.models import (
    InvestigationDraft,
    InvestigationPhase,
    InvestigationReviewBundle,
    InvestigationSynthesis,
    PeerReviewBundle,
    Phase,
    PlanDraft,
    PlanPhase,
    Position,
    Synthesis,
    TurnRequest,
    WorkStage,
)


def response_model(phase: WorkStage) -> type[BaseModel]:
    if isinstance(phase, PlanPhase):
        return PlanDraft
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
        _validate_plan_response(response)
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


def _validate_plan_response(response: BaseModel) -> None:
    if not isinstance(response, PlanDraft):
        raise ValueError("plan stage requires a plan draft")
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
    tool_instruction = (
        "Read and search only the minimum relevant workspace files. Do not use web, shell, "
        "writes, plugins, MCP, or delegation."
        if request.tool_policy.read
        else "Use only the supplied context and no tools."
    )
    sources: list[dict[str, object]] = []
    if previous_response is None:
        for item in request.plan_sources:
            if item.source_kind == "decision":
                evidence = [
                    {
                        key: evidence_value
                        for key, evidence_value in evidence_item.model_dump(mode="json").items()
                        if key not in {"file_sha256", "fragment_sha256", "validation_error"}
                    }
                    for evidence_item in item.evidence
                ]
                sources.append(
                    {
                        "source_kind": item.source_kind,
                        "decision_id": item.decision_id,
                        "question": item.question,
                        "conclusion": item.conclusion,
                        "conclusion_source": item.conclusion_source,
                        "rationale": item.rationale,
                        "constraints": item.constraints,
                        "non_goals": item.non_goals,
                        "assumptions": item.assumptions,
                        "risks": item.risks,
                        "human_note": item.human_note,
                        "evidence": evidence,
                    }
                )
            else:
                sources.append(item.model_dump(mode="json", exclude={"created_at"}))
    return f"""You are the planner in Ego. Translate the supplied accepted decisions or explicit
human instruction into one concise, implementation-ready plan. Do not reconsider, replace, or
expand the supplied direction.
If a material product or architecture choice is missing, record it under open_questions instead of
deciding it. {tool_instruction}
Keep tasks minimal, ordered, independently verifiable, and grounded in the current workspace.
Use relative affected paths. Do not include prose that does not help an implementation agent.
Respond in {request.language}.

Agent: {request.agent_id}
Workflow: {request.workflow_id}
Stage: {request.phase.value}
Task: {request.question}
{correction_text}
Plan sources:
{json.dumps(sources, ensure_ascii=False) if sources else "Omitted on correction."}

Return only JSON matching this schema:
{json.dumps(response_schema(request.phase), ensure_ascii=False)}
"""


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
