from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class AgentCapabilities(BaseModel):
    model_config = ConfigDict(frozen=True)

    local_read: bool = True
    local_search: bool = True
    internet: bool = False
    shell: bool = False
    write: bool = False
    plugins: bool = False
    mcp: bool = False
    delegation: bool = False


class AgentInput(BaseModel):
    question: str
    workspace: Path
    participant_ids: list[str] = Field(default_factory=list)
    command: str


class SpecializedAgent[InputT: AgentInput, OutputT](ABC):
    agent_id: str
    description: str
    workflow_id: str
    input_contract: type[InputT]
    output_contract: type[OutputT]
    required_capabilities: AgentCapabilities

    @abstractmethod
    async def execute(self, request: InputT) -> OutputT:
        raise NotImplementedError
