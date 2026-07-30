from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from ego.agents.base import SpecializedAgent
from ego.participants import Participant
from ego.storage import Database


class AgentRegistry:
    """Explicit discovery and dispatch; automatic routing is intentionally absent."""

    def __init__(self, agents: list[SpecializedAgent[Any, Any]]) -> None:
        self._agents = {agent.agent_id: agent for agent in agents}
        if len(self._agents) != len(agents):
            raise ValueError("specialized agent identifiers must be unique")

    def __iter__(self) -> Iterator[SpecializedAgent[Any, Any]]:
        return iter(self._agents.values())

    def list(self) -> list[SpecializedAgent[Any, Any]]:
        return list(self._agents.values())

    def get(self, agent_id: str) -> SpecializedAgent[Any, Any]:
        try:
            return self._agents[agent_id]
        except KeyError:
            raise KeyError(f"unknown specialized agent: {agent_id}") from None

    async def dispatch(self, agent_id: str, request: Any) -> Any:
        agent = self.get(agent_id)
        if not isinstance(request, agent.input_contract):
            raise TypeError(
                f"{agent_id} requires {agent.input_contract.__name__}, "
                f"received {type(request).__name__}"
            )
        return await agent.execute(request)


def build_agent_registry(
    database: Database, participants: dict[str, Participant]
) -> AgentRegistry:
    from ego.agents.decision import DecisionAgent
    from ego.agents.investigate import InvestigateAgent
    from ego.agents.plan import PlanAgent
    from ego.decision import DecisionWorkflow
    from ego.deliberation import DeliberationEngine
    from ego.investigation import InvestigationWorkflow
    from ego.planning import PlanWorkflow

    return AgentRegistry(
        [
            DecisionAgent(DecisionWorkflow(DeliberationEngine(database, participants))),
            InvestigateAgent(InvestigationWorkflow(database, participants)),
            PlanAgent(PlanWorkflow(database, participants)),
        ]
    )
