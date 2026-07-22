from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from time import monotonic

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Input, Markdown, ProgressBar, RichLog, Static
from textual.worker import Worker

from ego import __version__
from ego.config import AppPaths, load_config
from ego.deliberation import DeliberationEngine, DeliberationOutcome, NoParticipantsError
from ego.events import DeliberationEvent, DeliberationEventStream, DeliberationEventType
from ego.models import FinalDecision
from ego.participants import Participant, build_participants
from ego.storage import Database
from ego.tui.state import PHASES, ParticipantState, SessionState
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
        self.turn_started_at: dict[str, float] = {}
        self.active_worker: Worker[None] | None = None

    @staticmethod
    def _pending_participant() -> ParticipantState:
        return ParticipantState()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="workspace-grid"):
            with Vertical(id="main-column"):
                yield Static(f"E G O  ·  v{__version__}", id="brand")
                yield Static(
                    "Multiple perspectives. One auditable decision.", id="tagline"
                )
                yield Static("No active question", id="task-card")
                yield RichLog(id="timeline", wrap=True, markup=False)
                yield Markdown("", id="result")
            with Vertical(id="side-column"):
                yield Static("SESSION", classes="panel-title")
                yield Static("Ready", id="session-summary")
                yield Static("PARTICIPANTS", classes="panel-title")
                yield Static("Checking local CLIs…", id="participant-list")
                yield Static("PROTOCOL", classes="panel-title")
                yield ProgressBar(total=len(PHASES), show_eta=False, id="phase-progress")
                yield Static("Ready to deliberate", id="phase-summary")
        yield Input(
            placeholder="What decision do you want to examine?",
            id="question-input",
        )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#timeline", RichLog).write(
            Text("Enter a question. Use /help for interactive commands.", style="dim")
        )
        self.set_interval(1, self._update_elapsed)
        self.probe_participants()
        self.action_focus_question()

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

    @on(Input.Submitted, "#question-input")
    def submit_question(self, event: Input.Submitted) -> None:
        question = event.value.strip()
        event.input.clear()
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
                "Commands: /mode standard|discussion|expert · /doctor · /quit",
                style="cyan",
            )
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
            consumer = asyncio.create_task(self._consume_events(stream))
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
            self.started_at = None
            self.turn_started_at.clear()
            self.query_one("#question-input", Input).disabled = False
            self.action_focus_question()
            self._render_state()

    async def _consume_events(self, stream: DeliberationEventStream) -> None:
        while True:
            event = await stream.get()
            self.session.apply(event)
            self._present_event(event)
            self._render_state()
            if event.event_type is DeliberationEventType.DECISION_CREATED:
                return
            if (
                event.event_type is DeliberationEventType.RUN_STATUS_CHANGED
                and event.payload.get("status") in {"failed", "interrupted"}
            ):
                return

    def _begin_run(self, question: str) -> None:
        self.running = True
        self.started_at = monotonic()
        self.turn_started_at.clear()
        self.session.reset(list(self.participants))
        self.query_one("#question-input", Input).disabled = True
        self.query_one("#task-card", Static).update(f"CURRENT QUESTION\n{question}")
        result = self.query_one("#result", Markdown)
        result.update("")
        result.display = False
        timeline = self.query_one("#timeline", RichLog)
        timeline.clear()
        timeline.write(Text("Starting deliberation…", style="bold cyan"))
        self._render_state()

    def _present_event(self, event: DeliberationEvent) -> None:
        participant = event.participant_id
        if event.event_type is DeliberationEventType.PHASE_STARTED:
            self._write_timeline(f"▶ {self.session.phase_label}", style="bold cyan")
        elif event.event_type is DeliberationEventType.PARTICIPANT_TURN_STARTED and participant:
            self.turn_started_at[participant] = monotonic()
            self._write_timeline(f"  {participant} started", style="yellow")
        elif event.event_type is DeliberationEventType.PARTICIPANT_TURN_COMPLETED and participant:
            self.turn_started_at.pop(participant, None)
            duration = float(event.payload.get("duration_seconds") or 0)
            self._write_timeline(f"  ✓ {participant} completed · {duration:.1f}s", style="green")
        elif event.event_type is DeliberationEventType.PARTICIPANT_TURN_FAILED and participant:
            self.turn_started_at.pop(participant, None)
            self._write_timeline(f"  ✗ {participant} failed", style="red")
        elif event.event_type is DeliberationEventType.PHASE_COMPLETED:
            successful = len(event.payload.get("successful", []))
            total = int(event.payload.get("total", 0))
            self._write_timeline(f"  Phase complete · {successful}/{total}", style="dim")

    def _render_outcome(self, outcome: DeliberationOutcome) -> None:
        final = outcome.final
        self.query_one("#result", Markdown).update(self._final_markdown(final, outcome.decision_id))
        self.query_one("#result", Markdown).display = True
        self._write_timeline("Decision ready.", style="bold green")

    def _final_markdown(self, final: FinalDecision, decision_id: str) -> str:
        sections = [
            "## Recommendation",
            final.recommendation,
            f"**Confidence:** {final.confidence.value} — {final.confidence_reason}",
        ]
        if self.mode != "standard":
            for heading, values in (
                ("Supporting reasoning", final.supporting_arguments),
                ("Alternatives", final.alternatives),
                ("Disagreements", final.disagreements),
                ("Assumptions", final.assumptions),
                ("Risks", final.risks),
            ):
                if values:
                    sections.extend((f"### {heading}", *(f"- {value}" for value in values)))
        sections.append(f"_Decision record: {decision_id}_")
        return "\n\n".join(sections)

    def _render_state(self) -> None:
        elapsed = int(monotonic() - self.started_at) if self.started_at else 0
        run_label = self.session.run_id[:8] if self.session.run_id else "new"
        self.query_one("#session-summary", Static).update(
            f"Run: {run_label}\nStatus: {self.session.status}\n"
            f"Mode: {self.mode}\nElapsed: {elapsed // 60:02d}:{elapsed % 60:02d}"
        )
        participants = Text()
        colors = {
            "available": "green",
            "completed": "green",
            "working": "yellow",
            "checking": "cyan",
            "failed": "red",
            "unavailable": "bright_black",
            "unsafe": "red",
        }
        for name, state in sorted(self.session.participants.items()):
            color = colors.get(state.status, "white")
            participants.append(f"● {name.upper()}\n", style=f"bold {color}")
            participants.append(f"  {state.detail}\n\n", style="dim")
        self.query_one("#participant-list", Static).update(participants)
        self.query_one("#phase-progress", ProgressBar).update(
            progress=self.session.completed_phases
        )
        self.query_one("#phase-summary", Static).update(
            f"{self.session.completed_phases}/{len(PHASES)} · {self.session.phase_label}"
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
        self.query_one("#timeline", RichLog).write(Text(message, style=style))

    def action_cancel_run(self) -> None:
        if self.running and self.active_worker is not None:
            self.active_worker.cancel()
        else:
            self._write_timeline("No active deliberation.", style="dim")

    def action_focus_question(self) -> None:
        question = self.query_one("#question-input", Input)
        if not question.disabled:
            question.focus()


def run_tui(workspace: Path | None = None) -> None:
    EgoApp(workspace=workspace).run()
