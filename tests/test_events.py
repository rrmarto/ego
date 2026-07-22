from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ego.config import AppPaths
from ego.deliberation import DeliberationEngine, NoParticipantsError
from ego.events import DeliberationEvent, DeliberationEventStream, DeliberationEventType
from ego.models import (
    Argument,
    AvailabilityStatus,
    Confidence,
    Evidence,
    ParticipantAvailability,
    ParticipantTurnResult,
    Phase,
    Position,
    RunStatus,
    TurnRequest,
)
from ego.participants import ParticipantError
from ego.storage import Database


class BlockingParticipant:
    participant_id = "codex"

    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def probe(self) -> ParticipantAvailability:
        return ParticipantAvailability(
            participant_id=self.participant_id,
            status=AvailabilityStatus.AVAILABLE,
            binary="/fake/codex",
            version="1.0",
        )

    async def respond(self, request: TurnRequest) -> ParticipantTurnResult:
        await self.release.wait()
        evidence = Evidence(
            path="architecture.txt",
            line_start=1,
            line_end=1,
            explanation="The boundary is documented.",
        )
        position = Position(
            recommendation="Keep the boundary.",
            arguments=[Argument(id="boundary", claim="It is explicit.", evidence=[evidence])],
            confidence=Confidence.MODERATE,
            confidence_reason="The workspace documents it.",
        )
        return ParticipantTurnResult(
            participant_id=self.participant_id,
            phase=request.phase,
            payload=position,
            raw_output=position.model_dump_json(),
            duration_seconds=0.1,
        )


class FailingParticipant(BlockingParticipant):
    async def respond(self, request: TurnRequest) -> ParticipantTurnResult:
        del request
        raise ParticipantError("simulated participant failure")


async def receive_until(
    stream: DeliberationEventStream,
    event_type: DeliberationEventType,
) -> tuple[list[DeliberationEvent], DeliberationEvent]:
    received: list[DeliberationEvent] = []
    async with asyncio.timeout(2):
        while True:
            event = await stream.get()
            received.append(event)
            if event.event_type is event_type:
                return received, event


def drain(stream: DeliberationEventStream) -> list[DeliberationEvent]:
    events: list[DeliberationEvent] = []
    while not stream.empty():
        events.append(stream.get_nowait())
    return events


@pytest.mark.asyncio
async def test_events_are_streamed_after_persistence_and_before_completion(
    app_paths: AppPaths,
    tmp_path: Path,
) -> None:
    (tmp_path / "architecture.txt").write_text("modules are isolated\n", encoding="utf-8")
    stream = DeliberationEventStream()
    database = Database(app_paths, event_stream=stream)
    participant = BlockingParticipant()
    task = asyncio.create_task(
        DeliberationEngine(database, {participant.participant_id: participant}).deliberate(
            question="Keep the boundary?",
            workspace=tmp_path,
            participant_ids=[participant.participant_id],
            command="ask",
        )
    )

    events, started = await receive_until(
        stream, DeliberationEventType.PARTICIPANT_TURN_STARTED
    )

    assert events[0].event_type is DeliberationEventType.RUN_CREATED
    assert not task.done()
    assert started.phase is Phase.INDEPENDENT
    persisted_ids = {item.event_id for item in database.get_run_events(started.run_id)}
    assert started.event_id in persisted_ids

    participant.release.set()
    outcome = await task
    events.extend(drain(stream))
    event_types = [item.event_type for item in events]

    assert outcome.final.run_id == started.run_id
    assert DeliberationEventType.PARTICIPANT_PROBE_STARTED in event_types
    assert DeliberationEventType.PARTICIPANT_PROBE_COMPLETED in event_types
    assert DeliberationEventType.PHASE_STARTED in event_types
    assert DeliberationEventType.PARTICIPANT_TURN_COMPLETED in event_types
    assert DeliberationEventType.PHASE_COMPLETED in event_types
    assert event_types[-1] is DeliberationEventType.DECISION_CREATED
    assert event_types.index(DeliberationEventType.PARTICIPANT_TURN_STARTED) < event_types.index(
        DeliberationEventType.PARTICIPANT_TURN_COMPLETED
    )
    completed = next(
        item
        for item in events
        if item.event_type is DeliberationEventType.PARTICIPANT_TURN_COMPLETED
        and item.phase is Phase.INDEPENDENT
    )
    call = database.get_call(str(completed.payload["call_id"]))
    assert call["participant_id"] == participant.participant_id
    assert Position.model_validate_json(call["parsed_json"]).recommendation == "Keep the boundary."


@pytest.mark.asyncio
async def test_cancelling_active_deliberation_persists_interrupted_status(
    app_paths: AppPaths,
    tmp_path: Path,
) -> None:
    (tmp_path / "architecture.txt").write_text("modules are isolated\n", encoding="utf-8")
    stream = DeliberationEventStream()
    database = Database(app_paths, event_stream=stream)
    participant = BlockingParticipant()
    task = asyncio.create_task(
        DeliberationEngine(database, {participant.participant_id: participant}).deliberate(
            question="Keep the boundary?",
            workspace=tmp_path,
            participant_ids=[participant.participant_id],
            command="ask",
        )
    )
    _, started = await receive_until(stream, DeliberationEventType.PARTICIPANT_TURN_STARTED)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    remaining = drain(stream)

    assert database.get_run(started.run_id)["status"] == RunStatus.INTERRUPTED.value
    assert any(
        item.event_type is DeliberationEventType.RUN_STATUS_CHANGED
        and item.payload["status"] == RunStatus.INTERRUPTED.value
        for item in remaining
    )


@pytest.mark.asyncio
async def test_participant_failure_is_visible_before_run_failure(
    app_paths: AppPaths,
    tmp_path: Path,
) -> None:
    stream = DeliberationEventStream()
    database = Database(app_paths, event_stream=stream)
    participant = FailingParticipant()

    with pytest.raises(NoParticipantsError):
        await DeliberationEngine(
            database, {participant.participant_id: participant}
        ).deliberate(
            question="Keep the boundary?",
            workspace=tmp_path,
            participant_ids=[participant.participant_id],
            command="ask",
        )
    events = drain(stream)
    failed_turn = next(
        item
        for item in events
        if item.event_type is DeliberationEventType.PARTICIPANT_TURN_FAILED
    )

    assert failed_turn.participant_id == participant.participant_id
    assert failed_turn.phase is Phase.INDEPENDENT
    assert failed_turn.payload["error"] == "simulated participant failure"
    assert events[-1].event_type is DeliberationEventType.RUN_STATUS_CHANGED
    assert events[-1].payload["status"] == RunStatus.FAILED.value
