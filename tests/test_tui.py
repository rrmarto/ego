from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from textual.color import Color
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Collapsible, Markdown, Static

from ego.config import AppPaths
from ego.events import DeliberationEvent, DeliberationEventType
from ego.models import (
    Argument,
    AvailabilityStatus,
    Confidence,
    ParticipantAvailability,
    ParticipantTurnResult,
    PeerReview,
    PeerReviewBundle,
    Phase,
    Position,
    RunStatus,
    Synthesis,
    TurnRequest,
    UsageMetrics,
)
from ego.storage import Database
from ego.tui.app import EgoApp, QuestionInput
from ego.tui.input import CommandPalette
from ego.tui.state import SessionState
from ego.tui.timeline import DeliberationTimeline


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
            arguments=[Argument(id="boundary", claim="Committed events remain authoritative.")],
            alternatives=["Poll mutable run state."],
            risks=["Consumers must handle event ordering."],
            confidence=Confidence.LOW,
            confidence_reason="Single simulated participant.",
        )
        return ParticipantTurnResult(
            participant_id=self.participant_id,
            phase=request.phase,
            payload=position,
            raw_output=position.model_dump_json(),
            duration_seconds=0.1,
            usage=UsageMetrics(
                input_tokens=1_000,
                output_tokens=200,
                total_tokens=1_200,
            ),
        )


class ContestedParticipant:
    def __init__(self, participant_id: str) -> None:
        self.participant_id = participant_id

    async def probe(self) -> ParticipantAvailability:
        return ParticipantAvailability(
            participant_id=self.participant_id,
            status=AvailabilityStatus.AVAILABLE,
            binary=f"/fake/{self.participant_id}",
            version="test",
        )

    async def respond(self, request: TurnRequest) -> ParticipantTurnResult:
        if request.phase in {Phase.INDEPENDENT, Phase.REVISION}:
            payload: Position | PeerReviewBundle | Synthesis = Position(
                recommendation=f"{self.participant_id} recommendation",
                arguments=[
                    Argument(
                        id=f"{self.participant_id}-claim",
                        claim=f"{self.participant_id} claim",
                    )
                ],
                confidence=Confidence.MODERATE,
                confidence_reason="The simulated participants intentionally disagree.",
                change_reason="The simulated position remains intentionally different.",
            )
        elif request.phase is Phase.PEER_REVIEW:
            payload = PeerReviewBundle(
                reviews=[
                    PeerReview(
                        target_participant=name,
                        challenges=["The alternative remains materially different."],
                    )
                    for name in request.peer_positions
                ]
            )
        else:
            payload = Synthesis(
                recommendation=f"Accept the {self.participant_id} approach.",
                supporting_argument_ids=[f"{self.participant_id}-claim"],
                confidence=Confidence.MODERATE,
                confidence_reason="The simulated recommendations remain materially different.",
                equivalent_to_peer=False if request.phase is Phase.RECONCILIATION else None,
                material_conflicts=["The approaches use different boundaries."],
            )
        return ParticipantTurnResult(
            participant_id=self.participant_id,
            phase=request.phase,
            payload=payload,
            raw_output=payload.model_dump_json(),
            duration_seconds=0.01,
            usage=UsageMetrics(
                input_tokens=1_000,
                output_tokens=200,
                total_tokens=1_200,
                cost_usd=0.01 if self.participant_id == "claude" else None,
            ),
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
            payload={
                "duration_seconds": 12.5,
                "usage": {
                    "input_tokens": 1_000,
                    "output_tokens": 200,
                    "cached_input_tokens": 0,
                    "total_tokens": 1_200,
                    "cost_usd": 0.02,
                },
            },
        )
    )

    assert state.run_id == "run-1"
    assert state.phase is Phase.INDEPENDENT
    assert state.participants["codex"].status == "completed"
    assert state.participants["codex"].detail == "Completed in 12.5s"
    assert state.participants["codex"].total_tokens == 1_200
    assert state.participants["codex"].cost_usd == 0.02


async def test_tui_runs_a_question_and_renders_the_decision(
    app_paths: AppPaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock_calls = 0

    def fake_monotonic() -> float:
        nonlocal clock_calls
        clock_calls += 1
        return 100.0 if clock_calls == 1 else 225.0

    monkeypatch.setattr("ego.tui.app.monotonic", fake_monotonic)
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
            while app.current_final is None or app.running:
                await pilot.pause()

        assert app.session.completed_phases == 5
        assert not app.query_one("#welcome-view", Vertical).display
        assert app.query_one("#active-view", Vertical).display
        assert app.query_one("#result", Markdown).display
        assert not app.query_one("#bottom-bar", Vertical).display
        accept_button = app.query_one("#accept-final", Button)
        defer_button = app.query_one("#defer-final", Button)
        reject_button = app.query_one("#reject-final", Button)
        assert accept_button.has_class("action-accept")
        assert accept_button.styles.background == Color.parse("#060a12")
        assert defer_button.has_class("action-defer")
        assert reject_button.has_class("action-reject")
        assert reject_button.styles.background == Color.parse("#060a12")
        assert defer_button.styles.background == Color.parse("#060a12")
        assert accept_button.styles.border_top[1] == Color.parse("#42d66b")
        assert defer_button.styles.border_top[1] == Color.parse("#367db7")
        assert reject_button.styles.border_top[1] == Color.parse("#c94f5d")
        assert defer_button.region.height == accept_button.region.height
        assert defer_button.region.height == reject_button.region.height
        assert app.elapsed_seconds == 125
        session_text = str(app.query_one("#session-summary", Static).render())
        assert "Elapsed: 02:05" in session_text
        assert "CODEX: 1.2k tok" in session_text
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
        panel_rules = list(app.query(".panel-rule"))
        assert len(panel_rules) == 2
        for panel_id in ("session-panel", "participants-panel", "protocol-panel"):
            assert app.query_one(f"#{panel_id}", Vertical).parent is side_column
        assert panel_rules[0].parent is app.query_one("#session-panel", Vertical)
        assert panel_rules[1].parent is app.query_one("#participants-panel", Vertical)

        timeline = app.query_one("#timeline", DeliberationTimeline)
        timeline_text = timeline.transcript_text
        assert "Question received by the protocol" in timeline_text
        assert "Proposals generated (1/1)" in timeline_text
        assert "Recommendation ready" in timeline_text
        assert "CODEX started" not in timeline_text

        reasoning = list(timeline.query(Collapsible))[0]
        assert reasoning.collapsed
        reasoning_markdown = reasoning.query_one(Markdown)
        assert "Keep the event boundary." in reasoning_markdown.source
        assert "Committed events remain authoritative." in reasoning_markdown.source
        assert "Consumers must handle event ordering." in reasoning_markdown.source
        reasoning.query_one("CollapsibleTitle").focus()
        await pilot.press("enter")
        assert not reasoning.collapsed

        main_column = app.query_one("#main-column", Vertical)
        main_scroll = app.query_one("#main-scroll", VerticalScroll)
        active_input = app.query_one("#active-question-input", QuestionInput)
        protocol_panel = app.query_one("#protocol-panel", Vertical)
        assert timeline.parent is main_scroll
        assert main_scroll.parent is main_column
        assert active_input.parent is app.query_one("#active-bottom-bar", Vertical)
        assert active_input.region.x == main_column.content_region.x
        assert active_input.region.right == main_column.content_region.right
        assert protocol_panel.region.bottom == side_column.content_region.bottom
        assert timeline.region.height >= 8
        assert main_scroll.max_scroll_y > 0
        panel_heights = [
            app.query_one(f"#{panel_id}", Vertical).region.height
            for panel_id in ("session-panel", "participants-panel", "protocol-panel")
        ]
        assert max(panel_heights) - min(panel_heights) <= 1

        main_scroll.scroll_home(animate=False)
        await pilot.pause()
        assert main_scroll.scroll_y == 0
        main_scroll.scroll_end(animate=False)
        await pilot.pause()
        assert main_scroll.scroll_y == main_scroll.max_scroll_y
        assert await pilot.click(accept_button)
        await pilot.pause()
        human_message = app.query_one("#resolution-message", Static).render()
        assert "Final recommendation confirmed by you." in str(human_message)
        assert "Keep the event boundary." not in str(human_message)
        assert timeline.region.height >= 12


async def test_tui_suggests_and_executes_leading_slash_commands(
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
        question = app.query_one("#question-input", QuestionInput)
        palette = app.query_one("#welcome-command-palette", CommandPalette)

        question.text = "/"
        await pilot.pause()
        assert palette.display
        assert [option.id for option in palette.options] == [
            "/help",
            "/ask",
            "/summon",
            "/cd",
            "/pwd",
            "/mode",
            "/doctor",
            "/participants",
            "/runs",
            "/inspect",
            "/decisions",
            "/show",
            "/reconsider",
            "/choose",
            "/decide",
            "/accept",
            "/defer",
            "/reject",
            "/quit",
            "/exit",
        ]

        question.text = "/mo"
        await pilot.pause()
        assert palette.display
        assert [option.id for option in palette.options] == ["/mode"]

        await pilot.press("down", "enter")
        await pilot.pause()
        assert question.text == "/mode "
        assert question.has_focus
        assert not palette.display

        question.text = "/help"
        await pilot.press("enter")
        await pilot.pause()

        assert app.active_view
        assert app.screen.has_class("console-only")
        assert not app.query_one("#welcome-view", Vertical).display
        assert app.query_one("#active-view", Vertical).display
        timeline = app.query_one("#timeline", DeliberationTimeline)
        assert "Interactive commands:\n  <question>" in timeline.transcript_text
        assert "/runs" in timeline.transcript_text
        assert "List previous runs" in timeline.transcript_text
        assert "/decisions" in timeline.transcript_text
        assert "List decision records" in timeline.transcript_text
        active_question = app.query_one("#active-question-input", QuestionInput)
        assert active_question.has_focus

        active_question.text = "/runs"
        await pilot.press("enter")
        await pilot.pause()
        assert "Runs:\n  No persisted runs." in timeline.transcript_text

        active_question.text = "/decisions"
        await pilot.press("enter")
        await pilot.pause()
        assert "Decisions:\n  No decision records." in timeline.transcript_text

        active_question.text = "/doctor"
        await pilot.press("enter")
        async with asyncio.timeout(3):
            while "Participant checks:" not in timeline.transcript_text:
                await pilot.pause()
        assert "CODEX  available (test)" in timeline.transcript_text

        target = tmp_path / "project"
        target.mkdir()
        active_question.text = "/cd project"
        await pilot.press("enter")
        await pilot.pause()
        assert f"Workspace:\n  {target}" in timeline.transcript_text

        active_question.text = "/pwd"
        await pilot.press("enter")
        await pilot.pause()
        assert timeline.transcript_text.count(f"Workspace:\n  {target}") == 2


async def test_tui_treats_slashes_after_the_first_character_as_question_text(
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
        question = app.query_one("#question-input", QuestionInput)
        palette = app.query_one("#welcome-command-palette", CommandPalette)
        question.text = "Explain the /help syntax"
        await pilot.pause()
        assert not palette.display

        question.text = " /help"
        await pilot.pause()
        assert not palette.display

        await pilot.press("enter")
        async with asyncio.timeout(3):
            while app.current_final is None or app.running:
                await pilot.pause()

        task_text = str(app.query_one("#task-card", Static).render())
        assert "/help" in task_text
        assert "Interactive commands:" not in app.query_one(
            "#timeline", DeliberationTimeline
        ).transcript_text


async def test_tui_summon_runs_with_selected_participants(
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
        question = app.query_one("#question-input", QuestionInput)
        question.text = "/summon codex -- Keep the event boundary?"
        await pilot.press("enter")
        async with asyncio.timeout(3):
            while app.current_final is None or app.running:
                await pilot.pause()

        assert app.session.run_id is not None
        run = Database(app_paths).get_run(app.session.run_id)
        assert run["command"] == "summon"
        assert {call["participant_id"] for call in run["calls"]} == {"codex"}
        assert app.current_decision_id is not None
        original_decision_id = app.current_decision_id
        active_question = app.query_one("#active-question-input", QuestionInput)
        timeline = app.query_one("#timeline", DeliberationTimeline)

        active_question.text = "/runs"
        await pilot.press("enter")
        await pilot.pause()
        assert app.session.run_id in timeline.transcript_text

        active_question.text = "/decisions"
        await pilot.press("enter")
        await pilot.pause()
        assert original_decision_id in timeline.transcript_text

        active_question.text = f"/inspect {app.session.run_id}"
        await pilot.press("enter")
        await pilot.pause()
        assert "Run details:" in timeline.transcript_text

        active_question.text = f"/show {original_decision_id}"
        await pilot.press("enter")
        await pilot.pause()
        assert f"Decision: {original_decision_id}" in timeline.transcript_text

        active_question.text = (
            f"/reconsider {original_decision_id} -- New evidence changes the boundary."
        )
        await pilot.press("enter")
        async with asyncio.timeout(3):
            while app.current_decision_id == original_decision_id or app.running:
                await pilot.pause()

        assert app.session.run_id is not None
        reconsidered_run = Database(app_paths).get_run(app.session.run_id)
        assert reconsidered_run["command"] == "reconsider"
        assert reconsidered_run["parent_decision_id"] == original_decision_id


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


async def test_ctrl_c_copies_selection_before_falling_back_to_cancel(
    app_paths: AppPaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    participant = FastParticipant()
    app = EgoApp(
        workspace=tmp_path,
        paths=app_paths,
        participants={participant.participant_id: participant},
    )

    async with app.run_test(size=(120, 40)) as pilot:
        copied_to_system: list[str] = []
        monkeypatch.setattr(
            "ego.tui.app.copy_to_macos_clipboard",
            lambda text: copied_to_system.append(text) or True,
        )

        question = app.query_one("#question-input", QuestionInput)
        question.text = "/help"
        await pilot.press("enter")
        await pilot.pause()

        timeline = app.query_one("#timeline", DeliberationTimeline)
        help_message = list(timeline.query(".timeline-message"))[-1]
        assert await pilot.mouse_down(help_message, offset=(0, 0))
        assert await pilot.hover(help_message, offset=(15, 0))
        assert await pilot.mouse_up(help_message, offset=(15, 0))
        await pilot.pause()
        assert app.screen.get_selected_text() == "Interactive comm"
        assert app.clipboard == "Interactive comm"
        assert copied_to_system == ["Interactive comm"]

        await pilot.press("ctrl+c")
        assert app.clipboard == "Interactive comm"
        assert copied_to_system == ["Interactive comm", "Interactive comm"]

        app.clear_selection()
        await pilot.pause()
        question = app.query_one("#active-question-input", QuestionInput)
        question.text = "input selection"
        question.selection = ((0, 0), (0, 5))
        question.focus()
        await pilot.pause()
        app.action_copy_or_cancel()
        assert app.clipboard == "input"
        assert copied_to_system[-1] == "input"

        question.cursor_location = (0, 0)
        cancelled: list[bool] = []
        monkeypatch.setattr(app, "action_cancel_run", lambda: cancelled.append(True))
        app.action_copy_or_cancel()
        assert cancelled == [True]


async def test_selecting_a_decision_id_copies_it_on_mouse_release(
    app_paths: AppPaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    participant = FastParticipant()
    app = EgoApp(
        workspace=tmp_path,
        paths=app_paths,
        participants={participant.participant_id: participant},
    )

    async with app.run_test(size=(120, 40)) as pilot:
        copied_to_system: list[str] = []
        monkeypatch.setattr(
            "ego.tui.app.copy_to_macos_clipboard",
            lambda text: copied_to_system.append(text) or True,
        )

        question = app.query_one("#question-input", QuestionInput)
        question.text = "Keep the event boundary?"
        await pilot.press("enter")
        async with asyncio.timeout(3):
            while app.current_final is None or app.running:
                await pilot.pause()

        decision_id = app.current_decision_id
        assert decision_id is not None
        active_question = app.query_one("#active-question-input", QuestionInput)
        active_question.text = "/decisions"
        await pilot.press("enter")
        await pilot.pause()

        timeline = app.query_one("#timeline", DeliberationTimeline)
        decisions_message = list(timeline.query(".timeline-message"))[-1]
        assert decision_id in str(decisions_message.render())
        assert await pilot.mouse_down(decisions_message, offset=(2, 1))
        assert await pilot.hover(
            decisions_message,
            offset=(len(decision_id) + 1, 1),
        )
        assert await pilot.mouse_up(
            decisions_message,
            offset=(len(decision_id) + 1, 1),
        )
        await pilot.pause()

        assert app.screen.get_selected_text() == decision_id
        assert app.clipboard == decision_id
        assert copied_to_system[-1] == decision_id


async def test_tui_requires_and_records_human_resolution_for_contested_result(
    app_paths: AppPaths,
    tmp_path: Path,
) -> None:
    participants = {
        name: ContestedParticipant(name) for name in ("codex", "claude")
    }
    app = EgoApp(workspace=tmp_path, paths=app_paths, participants=participants)

    async with app.run_test(size=(140, 50)) as pilot:
        question = app.query_one("#question-input", QuestionInput)
        question.text = "Which boundary should we choose?"
        await pilot.press("enter")
        async with asyncio.timeout(3):
            while app.current_final is None:
                await pilot.pause()

        assert app.current_final.status is RunStatus.CONTESTED
        assert app.session.status == "contested"
        assert app.query_one("#resolution-panel", Vertical).display
        assert app.query_one("#resolve-option-1", Button).display
        assert app.query_one("#resolve-option-2", Button).display
        assert not app.query_one("#accept-final", Button).display

        timeline = app.query_one("#timeline", DeliberationTimeline)
        main_scroll = app.query_one("#main-scroll", VerticalScroll)
        assert timeline.region.height >= 8
        assert timeline.parent is main_scroll
        assert main_scroll.max_scroll_y > 0
        phase_cards = list(timeline.query(Collapsible))
        assert len(phase_cards) == 8
        assert all(card.collapsed for card in phase_cards)
        timeline_text = timeline.transcript_text
        assert timeline_text.count("proposal ready") == 2
        assert timeline_text.count("revised position") == 2
        assert timeline_text.count("cross-synthesis") == 2
        assert timeline_text.count("final reconciliation") == 2

        phase_sources = [card.query_one(Markdown).source for card in phase_cards]
        assert sum("### Proposal" in source for source in phase_sources) == 2
        assert sum("### Revised position" in source for source in phase_sources) == 2
        assert sum("### Cross-synthesis" in source for source in phase_sources) == 2
        assert sum("### Final reconciliation" in source for source in phase_sources) == 2
        assert any("**Changed position:** no" in source for source in phase_sources)
        assert any(
            "The simulated position remains intentionally different." in source
            for source in phase_sources
        )
        assert any("**Equivalent to peer:** no" in source for source in phase_sources)
        assert any("The approaches use different boundaries." in source for source in phase_sources)
        assert not any("### Peer review" in source for source in phase_sources)
        collapsed_scroll_max = main_scroll.max_scroll_y
        final_reconciliation = phase_cards[-1]
        final_reconciliation.query_one("CollapsibleTitle").focus()
        await pilot.press("enter")
        assert not final_reconciliation.collapsed
        assert main_scroll.max_scroll_y > collapsed_scroll_max

        resolve_button = app.query_one("#resolve-option-1", Button)
        resolve_button.scroll_visible(animate=False)
        await pilot.pause()
        assert await pilot.click(resolve_button)
        await pilot.pause()

        assert app.session.status == "accepted"
        assert app.current_decision_id is not None
        decision = Database(app_paths).get_decision(app.current_decision_id)
        assert decision["state"] == "accepted"
        assert decision["resolutions"][0]["alternative_index"] == 1
        assert not app.query_one("#resolution-actions", Horizontal).display
        assert timeline.region.height >= 12
