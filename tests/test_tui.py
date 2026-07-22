from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from textual.widgets import Input, Markdown

from ego.config import AppPaths
from ego.events import DeliberationEvent, DeliberationEventType
from ego.models import (
    AvailabilityStatus,
    Confidence,
    ParticipantAvailability,
    ParticipantTurnResult,
    Phase,
    Position,
    TurnRequest,
)
from ego.tui import EgoApp
from ego.tui.state import SessionState


class FastParticipant:
    participant_id = "codex"

    async def probe(self) -> ParticipantAvailability:
        return ParticipantAvailability(
            participant_id=self.participant_id,
            status=AvailabilityStatus.AVAILABLE,
            binary="/fake/codex",
            version="test",
        )

    async def respond(self, request: TurnRequest) -> ParticipantTurnResult:
        position = Position(
            recommendation="Keep the event boundary.",
            confidence=Confidence.LOW,
            confidence_reason="Single simulated participant.",
        )
        return ParticipantTurnResult(
            participant_id=self.participant_id,
            phase=request.phase,
            payload=position,
            raw_output=position.model_dump_json(),
            duration_seconds=0.1,
        )


def event(
    event_type: DeliberationEventType,
    *,
    participant: str | None = None,
    phase: Phase | None = None,
    payload: dict[str, object] | None = None,
) -> DeliberationEvent:
    event_payload = payload or {}
    if phase:
        event_payload["phase"] = phase.value
    return DeliberationEvent(
        event_id=1,
        run_id="run-1",
        event_type=event_type,
        participant_id=participant,
        phase=phase,
        payload=event_payload,
        created_at=datetime.now(UTC),
    )


def test_session_state_tracks_real_phase_and_participant_events() -> None:
    state = SessionState()
    state.reset(["codex", "claude"])
    state.apply(event(DeliberationEventType.RUN_CREATED))
    state.apply(
        event(
            DeliberationEventType.PHASE_STARTED,
            phase=Phase.INDEPENDENT,
            payload={"expected": ["codex", "claude"], "total": 2},
        )
    )
    state.apply(
        event(
            DeliberationEventType.PARTICIPANT_TURN_STARTED,
            participant="codex",
            phase=Phase.INDEPENDENT,
        )
    )
    state.apply(
        event(
            DeliberationEventType.PARTICIPANT_TURN_COMPLETED,
            participant="codex",
            phase=Phase.INDEPENDENT,
            payload={"duration_seconds": 12.5},
        )
    )

    assert state.run_id == "run-1"
    assert state.phase is Phase.INDEPENDENT
    assert state.participants["codex"].status == "completed"
    assert state.participants["codex"].detail == "Completed in 12.5s"


async def test_tui_runs_a_question_and_renders_the_decision(
    app_paths: AppPaths,
    tmp_path: Path,
) -> None:
    participant = FastParticipant()
    app = EgoApp(
        workspace=tmp_path,
        paths=app_paths,
        participants={participant.participant_id: participant},
    )

    async with app.run_test(size=(120, 40)) as pilot:
        question = app.query_one("#question-input", Input)
        question.value = "Should we keep the event boundary?"
        await pilot.press("enter")
        async with asyncio.timeout(3):
            while app.session.status != "completed":
                await pilot.pause()

        assert app.session.completed_phases == 5
        assert app.query_one("#result", Markdown).display
