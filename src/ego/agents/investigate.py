from __future__ import annotations

from ego.agents.base import AgentCapabilities, AgentInput, SpecializedAgent
from ego.investigation import InvestigationOutcome, InvestigationWorkflow


class InvestigationInput(AgentInput):
    command: str = "investigate"


class InvestigateAgent(SpecializedAgent[InvestigationInput, InvestigationOutcome]):
    agent_id = "investigate"
    description = "Investigates only the local workspace and returns an auditable report."
    workflow_id = "investigation"
    input_contract = InvestigationInput
    output_contract = InvestigationOutcome
    required_capabilities = AgentCapabilities()

    def __init__(self, workflow: InvestigationWorkflow) -> None:
        self.workflow = workflow

    async def execute(self, request: InvestigationInput) -> InvestigationOutcome:
        return await self.workflow.investigate(
            question=request.question,
            workspace=request.workspace,
            participant_ids=request.participant_ids,
            command=request.command,
        )
