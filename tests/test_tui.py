from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from textual.containers import Horizontal, Vertical
from textual.widgets import Markdown, RichLog, Static

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
from ego.tui.app import EgoApp, QuestionInput
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
        assert len(app.query("Header")) == 0
        assert len(app.query("Footer")) == 0
        assert app.query_one("#welcome-view", Vertical).display
        assert not app.query_one("#active-view", Vertical).display

        question = app.query_one("#question-input", QuestionInput)
        assert question.region.width <= 110
        assert question.soft_wrap

        question.text = "First line"
        await pilot.press("shift+enter")
        assert "\n" in question.text

        question.text = "A deliberately long question " * 10
        await pilot.pause()
        assert question.wrapped_document.height > 1

        question.text = "Should we keep the event boundary?"
        await pilot.press("enter")
        async with asyncio.timeout(3):
            while app.session.status != "completed":
                await pilot.pause()

        assert app.session.completed_phases == 5
        assert not app.query_one("#welcome-view", Vertical).display
        assert app.query_one("#active-view", Vertical).display
        assert app.query_one("#result", Markdown).display
        assert not app.query_one("#bottom-bar", Vertical).display
        active_brand = app.query_one("#active-brand", Static)
        active_tagline = app.query_one("#active-tagline", Static)
        active_brand_row = app.query_one("#active-brand-row", Horizontal)
        assert active_brand.region.height == 6
        assert active_tagline.region.x == active_brand.region.right
        assert (
            abs(
                active_brand.region.x
                + active_tagline.region.right
                - active_brand_row.region.x
                - active_brand_row.region.right
            )
            <= 1
        )
        assert len(app.query("#active-portrait")) == 0
        assert len(app.query("#active-quote")) == 0

        side_column = app.query_one("#side-column", Vertical)
        for panel_id in ("session-panel", "participants-panel", "protocol-panel"):
            assert app.query_one(f"#{panel_id}", Vertical).parent is side_column

        timeline_text = "\n".join(line.text for line in app.query_one("#timeline", RichLog).lines)
        assert "Question received by the protocol" in timeline_text
        assert "Proposals generated (1/1)" in timeline_text
        assert "Recommendation ready" in timeline_text
        assert "CODEX started" not in timeline_text

        main_column = app.query_one("#main-column", Vertical)
        active_input = app.query_one("#active-question-input", QuestionInput)
        protocol_panel = app.query_one("#protocol-panel", Vertical)
        assert active_input.region.x == main_column.content_region.x
        assert active_input.region.right == main_column.content_region.right
        assert protocol_panel.region.bottom == side_column.content_region.bottom
        panel_heights = [
            app.query_one(f"#{panel_id}", Vertical).region.height
            for panel_id in ("session-panel", "participants-panel", "protocol-panel")
        ]
        assert max(panel_heights) - min(panel_heights) <= 1


async def test_tui_uses_compact_layout_for_narrow_terminals(
    app_paths: AppPaths,
    tmp_path: Path,
) -> None:
    participant = FastParticipant()
    app = EgoApp(
        workspace=tmp_path,
        paths=app_paths,
        participants={participant.participant_id: participant},
    )

    async with app.run_test(size=(80, 32)) as pilot:
        await pilot.pause()

        assert app.screen.has_class("narrow")
        assert app.query_one("#welcome-view", Vertical).display
        assert not app.query_one("#side-column", Vertical).display
