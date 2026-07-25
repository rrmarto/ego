from __future__ import annotations

from ego.agents.base import AgentInput
from ego.deliberation import DeliberationEngine, DeliberationOutcome


class DecisionInput(AgentInput):
    parent_decision_id: str | None = None


class DecisionWorkflow:
    workflow_id = "decision"

    def __init__(self, engine: DeliberationEngine) -> None:
        self.engine = engine

    async def run(self, request: DecisionInput) -> DeliberationOutcome:
        return await self.engine.deliberate(
            question=request.question,
            workspace=request.workspace,
            participant_ids=request.participant_ids,
            command=request.command,
            parent_decision_id=request.parent_decision_id,
        )
