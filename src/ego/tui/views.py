from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Markdown, ProgressBar, Static

from ego import __version__
from ego.tui.assets import PORTRAIT, WORDMARK
from ego.tui.input import CommandPalette, QuestionInput
from ego.tui.state import PHASES
from ego.tui.timeline import DeliberationTimeline


class WelcomeView(Vertical):
    def compose(self) -> ComposeResult:
        with Horizontal(id="welcome-hero"):
            yield Static(PORTRAIT, id="portrait")
            with Vertical(id="welcome-copy"):
                yield Static(
                    "“  Fact: Collaboration is just multiple people\n"
                    "   being wrong together until one of us isn’t.  ”",
                    id="quote",
                )
                yield Static("— Dwight K. Schrute", id="quote-author")
                yield Static(WORDMARK, id="brand")
                yield Static("E G O  CLI", id="compact-brand")
                yield Static(f"v{__version__}", id="version")
        with Horizontal(id="welcome-details"):
            with Vertical(id="welcome-commands", classes="welcome-section"):
                yield Static("COMMANDS", classes="section-title")
                yield Static(
                    "/help — commands\n"
                    "/doctor — participant checks\n"
                    "/mode — transparency level\n"
                    "/quit — exit Ego",
                    id="command-list",
                )
            with Vertical(id="welcome-participants", classes="welcome-section"):
                yield Static("PARTICIPANTS", classes="section-title")
                yield Static("Checking local CLIs…", id="welcome-participant-list")
        yield Static("Ready for a question", id="welcome-status")


class ActiveView(Vertical):
    def compose(self) -> ComposeResult:
        yield Static("", id="session-strip")
        with Horizontal(id="workspace-grid"):
            with Vertical(id="main-column"):
                with VerticalScroll(id="main-scroll"):
                    with Horizontal(id="active-brand-row"):
                        yield Static(WORDMARK, id="active-brand")
                        yield Static(
                            "“ Because one confident model\n  was apparently not enough. ”",
                            id="active-tagline",
                        )
                        yield Static("E G O  CLI", id="active-compact-brand")
                    yield Static("No active question", id="task-card")
                    yield Static("DELIBERATION", classes="section-title")
                    yield DeliberationTimeline(id="timeline")
                    yield Markdown("", id="result")
                    with Vertical(id="resolution-panel"):
                        yield Static("HUMAN DECISION", classes="section-title")
                        yield Static("", id="resolution-message")
                        with Horizontal(id="resolution-actions"):
                            yield Button(
                                "Accept option 1",
                                id="resolve-option-1",
                                classes="action-accept",
                            )
                            yield Button(
                                "Accept option 2",
                                id="resolve-option-2",
                                classes="action-accept",
                            )
                            yield Button("Accept", id="accept-final", classes="action-accept")
                            yield Button("Defer", id="defer-final", classes="action-defer")
                            yield Button("Reject", id="reject-final", classes="action-reject")
                with Vertical(id="active-bottom-bar"):
                    yield CommandPalette(id="active-command-palette")
                    yield QuestionInput(
                        placeholder="Deliberation in progress…",
                        soft_wrap=True,
                        highlight_cursor_line=False,
                        id="active-question-input",
                        classes="question-input",
                    )
            with Vertical(id="side-column"):
                with Vertical(id="session-panel", classes="side-panel"):
                    yield Static("SESSION SUMMARY", classes="panel-title")
                    yield Static("", classes="panel-rule")
                    yield Static("Ready", id="session-summary")
                with Vertical(id="participants-panel", classes="side-panel"):
                    yield Static("PARTICIPANTS", classes="panel-title")
                    yield Static("", classes="panel-rule")
                    yield Static("Checking local CLIs…", id="participant-list")
                with Vertical(id="protocol-panel", classes="side-panel"):
                    yield Static("PROTOCOL", classes="panel-title")
                    yield ProgressBar(total=len(PHASES), show_eta=False, id="phase-progress")
                    yield Static("Ready to deliberate", id="phase-summary")
                    yield Static("", id="protocol-list")


class WelcomeQuestionBar(Vertical):
    def compose(self) -> ComposeResult:
        yield CommandPalette(id="welcome-command-palette")
        yield QuestionInput(
            placeholder="What decision do you want to examine?",
            soft_wrap=True,
            highlight_cursor_line=False,
            id="question-input",
            classes="question-input",
        )
