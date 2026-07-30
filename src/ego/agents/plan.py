from __future__ import annotations

from ego.agents.base import AgentCapabilities, SpecializedAgent
from ego.planning import PlanInput, PlanOutcome, PlanWorkflow


class PlanAgent(SpecializedAgent[PlanInput, PlanOutcome]):
    agent_id = "plan"
    description = (
        "Builds and audits a collaborative implementation plan from accepted decisions "
        "or explicit human instructions."
    )
    workflow_id = "plan"
    input_contract = PlanInput
    output_contract = PlanOutcome
    required_capabilities = AgentCapabilities()

    def __init__(self, workflow: PlanWorkflow) -> None:
        self.workflow = workflow

    async def execute(self, request: PlanInput) -> PlanOutcome:
        return await self.workflow.plan(request)
