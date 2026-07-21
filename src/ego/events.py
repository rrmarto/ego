from __future__ import annotations

import asyncio
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from ego.models import JsonObject, Phase


class DeliberationEventType(StrEnum):
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


class DeliberationEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: int = Field(ge=1)
    run_id: str
    event_type: DeliberationEventType
    participant_id: str | None = None
    phase: Phase | None = None
    payload: JsonObject = Field(default_factory=dict)
    created_at: datetime


class DeliberationEventStream:
    """In-process delivery for events that have already committed to SQLite."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[DeliberationEvent] = asyncio.Queue()

    def publish(self, event: DeliberationEvent) -> None:
        self._queue.put_nowait(event)

    async def get(self) -> DeliberationEvent:
        return await self._queue.get()

    def get_nowait(self) -> DeliberationEvent:
        return self._queue.get_nowait()

    def empty(self) -> bool:
        return self._queue.empty()

