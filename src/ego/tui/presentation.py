from __future__ import annotations

from collections.abc import Mapping

from rich.text import Text

from ego.models import FinalDecision
from ego.tui.state import PHASE_LABELS, PHASES, ParticipantState, SessionState

PARTICIPANT_COLORS = {
    "available": "green",
    "completed": "green",
    "working": "yellow",
    "checking": "cyan",
    "failed": "red",
    "unavailable": "bright_black",
    "unsafe": "red",
}

STATUS_COLORS = {
    "completed": "green",
    "running": "green",
    "starting": "yellow",
    "failed": "red",
    "interrupted": "yellow",
}


def session_summary(session: SessionState, *, mode: str, elapsed: int) -> str:
    run_label = session.run_id[:8] if session.run_id else "new"
    return (
        f"Run: {run_label}\nStatus: {session.status}\n"
        f"Mode: {mode}\nElapsed: {elapsed // 60:02d}:{elapsed % 60:02d}"
    )


def session_strip(session: SessionState, *, mode: str, width: int, version: str) -> Text:
    run_label = session.run_id[:8] if session.run_id else "new"
    strip = Text()
    strip.append(f"EGO CLI  v{version}", style="bold magenta")
    if width >= 96:
        strip.append(f"    RUN  {run_label}", style="bright_black")
        strip.append(f"    MODE  {mode}", style="cyan")
    else:
        strip.append(f"    {mode}", style="cyan")
    strip.append("    STATUS  ", style="bright_black")
    status_color = STATUS_COLORS.get(session.status, "bright_black")
    strip.append(session.status.upper(), style=f"bold {status_color}")
    return strip


def participant_texts(
    participants: Mapping[str, ParticipantState],
) -> tuple[Text, Text]:
    active = Text()
    welcome = Text()
    for name, state in sorted(participants.items()):
        color = PARTICIPANT_COLORS.get(state.status, "white")
        active.append(f"● {name.upper()}\n", style=f"bold {color}")
        active.append(f"  {state.detail}\n", style="dim")
        welcome.append("● ", style=color)
        welcome.append(name.upper(), style="bold")
        welcome.append(f"  ·  {state.status}\n", style=color)
    return active, welcome


def protocol_text(session: SessionState, *, running: bool) -> Text:
    protocol = Text()
    for index, phase in enumerate(PHASES):
        if index < session.completed_phases:
            marker, color = "✓", "green"
        elif phase is session.phase and running:
            marker, color = "◆", "yellow"
        else:
            marker, color = "○", "bright_black"
        protocol.append(f"{marker} ", style=f"bold {color}")
        protocol.append(f"{PHASE_LABELS[phase]}\n", style=color)
    return protocol


def welcome_status(participants: Mapping[str, ParticipantState]) -> str:
    if any(state.status == "pending" for state in participants.values()):
        return "Checking participant safety…"
    available = sum(state.status == "available" for state in participants.values())
    return f"Checks complete · {available}/{len(participants)} participants available"


def final_markdown(final: FinalDecision, decision_id: str, *, mode: str) -> str:
    sections = [
        "## Recommendation",
        final.recommendation,
        f"**Confidence:** {final.confidence.value} — {final.confidence_reason}",
    ]
    if mode != "standard":
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
