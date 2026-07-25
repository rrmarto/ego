from __future__ import annotations

from collections.abc import Mapping

from rich.text import Text

from ego.models import FinalDecision, InvestigationPhase, InvestigationReport
from ego.tui.state import (
    INVESTIGATION_PHASE_LABELS,
    PHASE_LABELS,
    ParticipantState,
    SessionState,
)

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
    "accepted": "green",
    "completed": "green",
    "contested": "yellow",
    "deferred": "yellow",
    "running": "green",
    "rejected": "red",
    "starting": "yellow",
    "failed": "red",
    "interrupted": "yellow",
}


def session_summary(session: SessionState, *, mode: str, elapsed: int) -> str:
    run_label = session.run_id[:8] if session.run_id else "new"
    summary = (
        f"Run: {run_label}\nStatus: {session.status}\n"
        f"Mode: {mode}\nElapsed: {elapsed // 60:02d}:{elapsed % 60:02d}"
    )
    active = [
        (name, state)
        for name, state in sorted(session.participants.items())
        if state.turns_completed
    ]
    if not active:
        return summary
    usage_lines = ["Usage:"]
    for name, state in active:
        if not state.usage_reported:
            usage = "not reported"
        else:
            usage = f"{_compact_tokens(state.total_tokens)} tok"
            if state.cost_usd:
                usage += f" · ${state.cost_usd:.2f}"
        usage_lines.append(f"{name.upper()}: {usage}")
    return summary + "\n" + "\n".join(usage_lines)


def _compact_tokens(tokens: int) -> str:
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}m"
    if tokens >= 1_000:
        return f"{tokens / 1_000:.1f}k"
    return str(tokens)


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
    for index, phase in enumerate(session.phases):
        if index < session.completed_phases:
            marker, color = "✓", "green"
        elif phase is session.phase and running:
            marker, color = "◆", "yellow"
        else:
            marker, color = "○", "bright_black"
        protocol.append(f"{marker} ", style=f"bold {color}")
        label = (
            INVESTIGATION_PHASE_LABELS[phase]
            if isinstance(phase, InvestigationPhase)
            else PHASE_LABELS[phase]
        )
        protocol.append(f"{label}\n", style=color)
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
        f"**Verification scope:** {final.verification_scope}",
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
    if final.warnings:
        sections.extend(("### Warnings", *(f"- {warning}" for warning in final.warnings)))
    if final.needs_human_resolution:
        sections.extend(("## Human decision required",))
        for index, alternative in enumerate(final.alternatives, 1):
            sections.extend((f"### Option {index}", alternative))
        sections.append(
            "Choose an option below, use `/choose <number>`, record your own conclusion with "
            "`/decide <text>`, or use `/defer` or `/reject`."
        )
    else:
        sections.append("Use the buttons below or `/accept`, `/defer`, or `/reject`.")
    sections.append(f"_Decision record: {decision_id}_")
    return "\n\n".join(sections)


def investigation_markdown(report: InvestigationReport) -> str:
    sections = [
        "## Investigation report",
        f"**Status:** {report.status.value}",
    ]
    if report.findings:
        sections.append("### Findings")
        for finding in report.findings:
            sections.append(
                f"- **{finding.claim}** ({finding.confidence.value}) — {finding.explanation}"
            )
            for evidence in finding.evidence:
                sections.append(
                    f"  - `{evidence.path}:{evidence.line_start}-{evidence.line_end}` "
                    f"[{evidence.status.value}] — {evidence.explanation}"
                )
    if report.hypotheses:
        sections.append("### Hypotheses")
        sections.extend(
            f"- **{item.state.value}:** {item.hypothesis} — {item.explanation}"
            for item in report.hypotheses
        )
    if report.disputed_findings:
        sections.append("### Disputed findings")
        sections.extend(f"- {item.claim} — {item.explanation}" for item in report.disputed_findings)
    for heading, values in (
        ("Unknowns", report.unknowns),
        ("Next checks", report.next_checks),
        ("Warnings", report.warnings),
    ):
        if values:
            sections.extend((f"### {heading}", *(f"- {value}" for value in values)))
    sections.append(f"_Immutable run result: {report.run_id}_")
    return "\n\n".join(sections)
