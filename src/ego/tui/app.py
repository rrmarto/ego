from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from time import monotonic

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.events import Resize
from textual.widgets import Button, Markdown, ProgressBar, Static
from textual.worker import Worker

from ego import __version__
from ego.config import AppPaths, load_config
from ego.deliberation import DeliberationEngine, DeliberationOutcome, NoParticipantsError
from ego.events import DeliberationEvent, DeliberationEventStream, DeliberationEventType
from ego.models import FinalDecision, Phase, Position, Synthesis
from ego.participants import Participant, build_participants
from ego.storage import Database
from ego.tui.input import QuestionInput
from ego.tui.presentation import (
    final_markdown,
    participant_texts,
    protocol_text,
    session_strip,
    session_summary,
    welcome_status,
)
from ego.tui.state import PHASES, ParticipantState, SessionState
from ego.tui.timeline import DeliberationTimeline
from ego.tui.views import ActiveView, WelcomeQuestionBar, WelcomeView
from ego.workspace import resolve_workspace


class EgoApp(App[None]):
    TITLE = "ego"
    SUB_TITLE = "collective decision engine"
    CSS_PATH = "ego.tcss"
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("ctrl+c", "cancel_run", "Cancel run", show=True),
        Binding("ctrl+l", "focus_question", "Question", show=True),
    ]

    def __init__(
        self,
        *,
        workspace: Path | None = None,
        paths: AppPaths | None = None,
        participants: dict[str, Participant] | None = None,
    ) -> None:
        super().__init__()
        self.workspace = resolve_workspace(workspace or Path.cwd())
        self.paths = paths or AppPaths.resolve()
        self.config = load_config(self.paths)
        self.participants = (
            participants if participants is not None else build_participants(self.config)
        )
        self.session = SessionState(
            participants={name: self._pending_participant() for name in self.participants}
        )
        self.mode = "standard"
        self.running = False
        self.started_at: float | None = None
        self.elapsed_seconds = 0
        self.turn_started_at: dict[str, float] = {}
        self.active_worker: Worker[None] | None = None
        self.active_view = False
        self.current_decision_id: str | None = None
        self.current_final: FinalDecision | None = None

    @staticmethod
    def _pending_participant() -> ParticipantState:
        return ParticipantState()

    def compose(self) -> ComposeResult:
        yield WelcomeView(id="welcome-view")
        yield ActiveView(id="active-view")
        yield WelcomeQuestionBar(id="bottom-bar")

    def on_mount(self) -> None:
        self.query_one("#timeline", DeliberationTimeline).write_message(
            "Enter a question. Use /help for interactive commands.", style="dim"
        )
        self.set_interval(1, self._update_elapsed)
        self._set_responsive_class(self.size.width)
        self._render_state()
        self.probe_participants()
        self.action_focus_question()

    def on_resize(self, event: Resize) -> None:
        self._set_responsive_class(event.size.width)

    def _set_responsive_class(self, width: int) -> None:
        self.screen.set_class(width < 96, "narrow")

    @work(exclusive=True, group="probe")
    async def probe_participants(self) -> None:
        results = await asyncio.gather(
            *(participant.probe() for participant in self.participants.values()),
            return_exceptions=True,
        )
        for name, result in zip(self.participants, results, strict=True):
            state = self.session.participants[name]
            if isinstance(result, BaseException):
                state.status = "unknown"
                state.detail = str(result)
            else:
                state.status = result.status.value
                state.detail = result.reason or result.version or result.status.value
        self._render_state()

    @on(QuestionInput.Submitted)
    def submit_question(self, event: QuestionInput.Submitted) -> None:
        question = event.question_input.text.strip()
        event.question_input.clear()
        if not question:
            return
        if question.startswith("/"):
            self._handle_command(question)
            return
        if self.running:
            self._write_timeline("A deliberation is already running.", style="yellow")
            return
        self.active_worker = self.deliberate(question)

    def _handle_command(self, raw: str) -> None:
        command, _, value = raw.partition(" ")
        if command in {"/quit", "/exit"}:
            self.exit()
        elif command == "/doctor":
            self.probe_participants()
            self._write_timeline("Refreshing participant availability…", style="cyan")
        elif command == "/mode" and value in {"standard", "discussion", "expert"}:
            self.mode = value
            self._write_timeline(f"Transparency mode: {value}", style="magenta")
        elif command == "/help":
            self._write_timeline(
                "Commands: /choose N · /decide TEXT · /accept · /defer · /reject · "
                "/mode standard|discussion|expert · /doctor · /quit",
                style="cyan",
            )
        elif command == "/choose":
            choice, _, note = value.partition(" ")
            try:
                alternative = int(choice)
            except ValueError:
                self._write_timeline("Use /choose followed by an option number.", style="yellow")
                return
            self._record_human_resolution(alternative_index=alternative, note=note or None)
        elif command == "/decide":
            self._record_human_resolution(custom_text=value)
        elif command in {"/accept", "/defer", "/reject"}:
            state = {"/accept": "accepted", "/defer": "deferred", "/reject": "rejected"}[
                command
            ]
            self._transition_current_decision(state, value or None)
        else:
            self._write_timeline(f"Unknown command: {raw}", style="yellow")

    @work(exclusive=True, group="deliberation", exit_on_error=False)
    async def deliberate(self, question: str) -> None:
        self._begin_run(question)
        stream = DeliberationEventStream()
        consumer: asyncio.Task[None] | None = None
        task: asyncio.Task[DeliberationOutcome] | None = None
        try:
            database = Database(self.paths, event_stream=stream)
            database.cleanup_raw(self.config.raw_retention_days)
            engine = DeliberationEngine(database, self.participants)
            consumer = asyncio.create_task(self._consume_events(stream, database))
            task = asyncio.create_task(
                engine.deliberate(
                    question=question,
                    workspace=self.workspace,
                    participant_ids=list(self.participants),
                    command="ask",
                )
            )
            outcome = await task
            await consumer
            self._render_outcome(outcome)
        except NoParticipantsError as error:
            if consumer is not None:
                await consumer
            self._write_timeline(f"Could not start: {error}", style="red")
        except asyncio.CancelledError:
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            if consumer is not None:
                consumer.cancel()
                with suppress(asyncio.CancelledError):
                    await consumer
            self._write_timeline("Deliberation interrupted.", style="yellow")
            raise
        except Exception as error:
            if consumer is not None and not consumer.done():
                await consumer
            self._write_timeline(f"Run failed: {error}", style="red")
        finally:
            self.running = False
            if self.started_at is not None:
                self.elapsed_seconds = int(monotonic() - self.started_at)
            self.started_at = None
            self.turn_started_at.clear()
            question_input = self.query_one("#active-question-input", QuestionInput)
            question_input.disabled = False
            question_input.placeholder = (
                "Resolve with /choose N, /decide TEXT, /defer, or /reject"
                if self.current_final and self.current_final.needs_human_resolution
                else "What decision do you want to examine next?"
            )
            self.action_focus_question()
            self._render_state()

    async def _consume_events(
        self, stream: DeliberationEventStream, database: Database
    ) -> None:
        while True:
            event = await stream.get()
            self.session.apply(event)
            self._present_event(event, database)
            self._render_state()
            if event.event_type is DeliberationEventType.DECISION_CREATED:
                return
            if event.event_type is DeliberationEventType.RUN_STATUS_CHANGED and event.payload.get(
                "status"
            ) in {"failed", "interrupted"}:
                return

    def _begin_run(self, question: str) -> None:
        self.running = True
        self.started_at = monotonic()
        self.elapsed_seconds = 0
        self.turn_started_at.clear()
        self.session.reset(list(self.participants))
        self.screen.remove_class("decision-ready", "decision-resolved")
        self.current_decision_id = None
        self.current_final = None
        self.active_view = True
        self.query_one("#welcome-view", WelcomeView).display = False
        self.query_one("#bottom-bar", WelcomeQuestionBar).display = False
        self.query_one("#active-view", ActiveView).display = True
        active_input = self.query_one("#active-question-input", QuestionInput)
        active_input.disabled = True
        active_input.placeholder = "Deliberation in progress…"
        task = Text()
        task.append("CURRENT QUESTION\n", style="bold #94a5dc")
        task.append(question, style="white")
        self.query_one("#task-card", Static).update(task)
        result = self.query_one("#result", Markdown)
        result.update("")
        result.display = False
        self.query_one("#resolution-panel").display = False
        self.query_one("#timeline", DeliberationTimeline).clear()
        self._render_state()

    def _present_event(self, event: DeliberationEvent, database: Database) -> None:
        participant = event.participant_id
        if event.event_type is DeliberationEventType.PARTICIPANT_TURN_STARTED and participant:
            self.turn_started_at[participant] = monotonic()
        elif (
            event.event_type
            in {
                DeliberationEventType.PARTICIPANT_TURN_COMPLETED,
                DeliberationEventType.PARTICIPANT_TURN_FAILED,
            }
            and participant
        ):
            self.turn_started_at.pop(participant, None)
        timeline = self.query_one("#timeline", DeliberationTimeline)
        timeline.present(event)
        if (
            event.event_type is DeliberationEventType.PARTICIPANT_TURN_COMPLETED
            and event.phase
            in {
                Phase.INDEPENDENT,
                Phase.REVISION,
                Phase.SYNTHESIS,
                Phase.RECONCILIATION,
            }
            and participant
        ):
            call_id = event.payload.get("call_id")
            if isinstance(call_id, str):
                call = database.get_call(call_id)
                parsed_json = call.get("parsed_json")
                if isinstance(parsed_json, str):
                    payload = (
                        Position.model_validate_json(parsed_json)
                        if event.phase in {Phase.INDEPENDENT, Phase.REVISION}
                        else Synthesis.model_validate_json(parsed_json)
                    )
                    timeline.add_phase_result(
                        participant,
                        event.phase,
                        payload,
                        duration_seconds=call.get("duration_seconds"),
                    )

    def _render_outcome(self, outcome: DeliberationOutcome) -> None:
        self.screen.add_class("decision-ready")
        self.screen.remove_class("decision-resolved")
        self.current_decision_id = outcome.decision_id
        self.current_final = outcome.final
        self.query_one("#result", Markdown).update(
            final_markdown(outcome.final, outcome.decision_id, mode=self.mode)
        )
        result = self.query_one("#result", Markdown)
        result.display = True
        panel = self.query_one("#resolution-panel")
        panel.display = True
        self.query_one("#resolution-actions").display = True
        contested = outcome.final.needs_human_resolution
        self.query_one("#resolution-message", Static).update(
            "The models did not reach an equivalent conclusion. Your choice will be recorded "
            "without erasing their disagreement."
            if contested
            else "The recommendation remains pending until you accept, defer, or reject it."
        )
        for index in (1, 2):
            button = self.query_one(f"#resolve-option-{index}", Button)
            button.display = contested and index <= len(outcome.final.alternatives)
        self.query_one("#accept-final", Button).display = not contested
        self.call_after_refresh(result.scroll_visible, animate=False, top=True)

    @on(Button.Pressed)
    def resolve_from_button(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "resolve-option-1":
            self._record_human_resolution(alternative_index=1)
        elif button_id == "resolve-option-2":
            self._record_human_resolution(alternative_index=2)
        elif button_id == "accept-final":
            self._transition_current_decision("accepted", None)
        elif button_id == "defer-final":
            self._transition_current_decision("deferred", None)
        elif button_id == "reject-final":
            self._transition_current_decision("rejected", None)

    def _record_human_resolution(
        self,
        *,
        alternative_index: int | None = None,
        custom_text: str | None = None,
        note: str | None = None,
    ) -> None:
        if not self.current_decision_id or not self.current_final:
            self._write_timeline("There is no decision waiting for resolution.", style="yellow")
            return
        try:
            resolution = Database(self.paths).resolve_decision(
                self.current_decision_id,
                alternative_index=alternative_index,
                custom_text=custom_text,
                note=note,
            )
        except (KeyError, ValueError) as error:
            self._write_timeline(str(error), style="yellow")
            return
        self._show_human_outcome("accepted", str(resolution["recommendation"]))

    def _transition_current_decision(self, state: str, note: str | None) -> None:
        if not self.current_decision_id or not self.current_final:
            self._write_timeline("There is no decision waiting for action.", style="yellow")
            return
        try:
            Database(self.paths).transition_decision(
                self.current_decision_id,
                state,  # type: ignore[arg-type]
                note,
            )
        except (KeyError, ValueError) as error:
            self._write_timeline(str(error), style="yellow")
            return
        recommendation = note or ""
        self._show_human_outcome(state, recommendation)

    def _show_human_outcome(self, state: str, recommendation: str) -> None:
        self.session.status = state
        self.screen.add_class("decision-resolved")
        message = Text()
        message.append(state.upper(), style="bold green")
        if recommendation:
            message.append(" · Selected conclusion: ", style="dim")
            message.append(recommendation, style="white")
        elif state == "accepted":
            message.append(" · Final recommendation confirmed by you.", style="dim")
        self.query_one("#resolution-message", Static).update(message)
        self.query_one("#resolution-actions").display = False
        resolution_panel = self.query_one("#resolution-panel")
        self.call_after_refresh(resolution_panel.scroll_visible, animate=False)
        self.query_one("#active-question-input", QuestionInput).placeholder = (
            "What decision do you want to examine next?"
        )
        self._write_timeline(f"Human decision recorded as {state}.", style="green")
        self._render_state()

    def _render_state(self) -> None:
        elapsed = (
            int(monotonic() - self.started_at)
            if self.started_at is not None
            else self.elapsed_seconds
        )
        self.query_one("#session-summary", Static).update(
            session_summary(self.session, mode=self.mode, elapsed=elapsed)
        )
        self.query_one("#session-strip", Static).update(
            session_strip(
                self.session,
                mode=self.mode,
                width=self.size.width,
                version=__version__,
            )
        )
        participants, welcome_participants = participant_texts(self.session.participants)
        self.query_one("#participant-list", Static).update(participants)
        self.query_one("#welcome-participant-list", Static).update(welcome_participants)
        self.query_one("#phase-progress", ProgressBar).update(
            progress=self.session.completed_phases
        )
        self.query_one("#phase-summary", Static).update(
            f"{self.session.completed_phases}/{len(PHASES)} · {self.session.phase_label}"
        )
        self.query_one("#protocol-list", Static).update(
            protocol_text(self.session, running=self.running)
        )
        if not self.active_view:
            self.query_one("#welcome-status", Static).update(
                welcome_status(self.session.participants)
            )

    def _update_elapsed(self) -> None:
        if not self.running:
            return
        now = monotonic()
        for participant, started_at in self.turn_started_at.items():
            state = self.session.participants[participant]
            state.detail = f"{self.session.phase_label} · {int(now - started_at)}s"
        self._render_state()

    def _write_timeline(self, message: str, *, style: str) -> None:
        self.query_one("#timeline", DeliberationTimeline).write_message(message, style=style)

    def action_cancel_run(self) -> None:
        if self.running and self.active_worker is not None:
            self.active_worker.cancel()
        else:
            self._write_timeline("No active deliberation.", style="dim")

    def action_focus_question(self) -> None:
        input_id = "#active-question-input" if self.active_view else "#question-input"
        question = self.query_one(input_id, QuestionInput)
        if not question.disabled:
            question.focus()


def run_tui(workspace: Path | None = None) -> None:
    EgoApp(workspace=workspace).run()
