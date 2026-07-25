from __future__ import annotations

import asyncio
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ego.models import InvestigationPhase, JsonObject, Phase, WorkStage


class WorkEventType(StrEnum):
    RUN_CREATED = "run_created"
    RUN_STATUS_CHANGED = "run_status_changed"
    PARTICIPANT_PROBE_STARTED = "participant_probe_started"
    PARTICIPANT_PROBE_COMPLETED = "participant_probe_completed"
    PHASE_STARTED = "phase_started"
    PARTICIPANT_TURN_STARTED = "participant_turn_started"
    PARTICIPANT_TURN_COMPLETED = "participant_turn_completed"
    PARTICIPANT_TURN_FAILED = "participant_turn_failed"
    PHASE_COMPLETED = "phase_completed"
    DECISION_CREATED = "decision_created"
    RESULT_CREATED = "result_created"


class WorkEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: int = Field(ge=1)
    run_id: str
    event_type: WorkEventType
    agent_id: str = "decision"
    workflow_id: str = "decision"
    participant_id: str | None = None
    phase: WorkStage | None = None
    stage: str | None = None
    payload: JsonObject = Field(default_factory=dict)
    created_at: datetime

    @model_validator(mode="after")
    def normalize_stage(self) -> WorkEvent:
        if self.stage is None and self.phase is not None:
            object.__setattr__(self, "stage", self.phase.value)
        elif self.phase is None and self.stage is not None:
            if self.agent_id == "investigate":
                object.__setattr__(self, "phase", InvestigationPhase(self.stage))
            else:
                object.__setattr__(self, "phase", Phase(self.stage))
        return self


class WorkEventStream:
    """In-process delivery for events that have already committed to SQLite."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[WorkEvent] = asyncio.Queue()

    def publish(self, event: WorkEvent) -> None:
        self._queue.put_nowait(event)

    async def get(self) -> WorkEvent:
        return await self._queue.get()

    def get_nowait(self) -> WorkEvent:
        return self._queue.get_nowait()

    def empty(self) -> bool:
        return self._queue.empty()


# Public compatibility aliases for the original Decision workflow.
DeliberationEventType = WorkEventType
DeliberationEvent = WorkEvent
DeliberationEventStream = WorkEventStream
