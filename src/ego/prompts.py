from __future__ import annotations

import json
from typing import cast

from pydantic import BaseModel

from ego.models import PeerReviewBundle, Phase, Position, Synthesis, TurnRequest


def response_model(phase: Phase) -> type[BaseModel]:
    if phase in {Phase.INDEPENDENT, Phase.REVISION}:
        return Position
    if phase is Phase.PEER_REVIEW:
        return PeerReviewBundle
    return Synthesis


def response_schema(phase: Phase) -> dict[str, object]:
    schema = response_model(phase).model_json_schema()
    return cast(dict[str, object], _strict_schema(schema))


def validate_response(request: TurnRequest, response: BaseModel) -> None:
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


def build_prompt(request: TurnRequest, *, correction: str | None = None) -> str:
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
