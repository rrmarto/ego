from __future__ import annotations

from ego.agents.base import AgentCapabilities, SpecializedAgent
from ego.decision import DecisionInput, DecisionWorkflow
from ego.deliberation import DeliberationOutcome


class DecisionAgent(SpecializedAgent[DecisionInput, DeliberationOutcome]):
    agent_id = "decision"
    description = "Preserves Ego's five-stage decision deliberation and human resolution."
    workflow_id = "decision"
    input_contract = DecisionInput
    output_contract = DeliberationOutcome
    required_capabilities = AgentCapabilities()

    def __init__(self, workflow: DecisionWorkflow) -> None:
        self.workflow = workflow

    async def execute(self, request: DecisionInput) -> DeliberationOutcome:
        return await self.workflow.run(request)
