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
            "Do not infer or imitate other participants."
        ),
        Phase.PEER_REVIEW: (
            "Review every peer position. Identify valid points, factual mistakes, unsupported "
            "assumptions, missing evidence, and objectively stronger arguments."
        ),
        Phase.REVISION: (
            "Reconsider your position using the peer reviews. Change it only for a stronger "
            "argument, disproven assumption, error, or superior evidence."
        ),
        Phase.SYNTHESIS: (
            "Synthesize the strongest supported arguments without voting and without adding new "
            "evidence. Preserve credible alternatives and material disagreement."
        ),
        Phase.RECONCILIATION: (
            "Compare the two syntheses. Set equivalent_to_peer true only when their material "
            "recommendations are equivalent. List every material conflict. Do not force consensus."
        ),
    }
    context = request.model_dump(mode="json", exclude_none=True)
    context["workspace"] = str(request.workspace)
    schema = response_schema(request.phase)
    correction_text = f"\nPrevious response validation error: {correction}\n" if correction else ""
    return f"""You are a peer in Ego, a decision-only deliberation engine.
You have equal authority with every other participant. You may read and search the workspace, but
you must not write files, run commands, use the web, delegate, or implement the recommendation.
Give concise, auditable rationale rather than private chain-of-thought. Every repository-specific
claim should cite a relative file path and exact line range. Respond in {request.language}.

Phase: {request.phase.value}
Task: {instructions[request.phase]}
{correction_text}
Context:
{json.dumps(context, ensure_ascii=False)}

Return only JSON matching this schema:
{json.dumps(schema, ensure_ascii=False)}
"""
