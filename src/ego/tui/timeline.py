from __future__ import annotations

from rich.text import Text
from textual.containers import VerticalGroup
from textual.widgets import Collapsible, Markdown, Static

from ego.events import DeliberationEvent, DeliberationEventType
from ego.models import Phase, Position, Synthesis

PHASE_TIMELINE = {
    Phase.INDEPENDENT: (
        "Independent proposals in progress",
        "Participants are preparing their responses independently.",
        "bold yellow",
    ),
    Phase.PEER_REVIEW: (
        "Cross-evaluation in progress",
        "Participants are reviewing the other proposals.",
        "bold bright_cyan",
    ),
    Phase.REVISION: (
        "Position revision in progress",
        "Participants are refining their positions after peer feedback.",
        "bold magenta",
    ),
    Phase.SYNTHESIS: (
        "Cross-synthesis in progress",
        "Participants are combining the strongest supported points.",
        "bold bright_cyan",
    ),
    Phase.RECONCILIATION: (
        "Reconciliation in progress",
        "Participants are examining the remaining disagreements.",
        "bold green",
    ),
}

PHASE_COMPLETION_LABELS = {
    Phase.INDEPENDENT: "Proposals generated",
    Phase.PEER_REVIEW: "Cross-evaluation completed",
    Phase.REVISION: "Positions revised",
    Phase.SYNTHESIS: "Syntheses generated",
    Phase.RECONCILIATION: "Reconciliation completed",
}

PHASE_COMPLETION_DETAILS = {
    Phase.INDEPENDENT: "Independent responses are ready for the next phase.",
    Phase.PEER_REVIEW: "Peer reviews have been recorded.",
    Phase.REVISION: "Revised positions have been recorded.",
    Phase.SYNTHESIS: "Cross-syntheses have been recorded.",
    Phase.RECONCILIATION: "Remaining disagreements have been examined.",
}

PHASE_RESULT_LABELS = {
    Phase.INDEPENDENT: "proposal ready",
    Phase.REVISION: "revised position",
    Phase.SYNTHESIS: "cross-synthesis",
    Phase.RECONCILIATION: "final reconciliation",
}

PHASE_RESULT_CLASSES = {
    Phase.INDEPENDENT: "independent-card",
    Phase.REVISION: "revision-card",
    Phase.SYNTHESIS: "synthesis-card",
    Phase.RECONCILIATION: "reconciliation-card",
}


class DeliberationTimeline(VerticalGroup):
    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._transcript: list[str] = []

    @property
    def transcript_text(self) -> str:
        return "\n".join(self._transcript)

    def clear(self) -> None:
        self._transcript.clear()
        self.remove_children()

    def present(self, event: DeliberationEvent) -> None:
        participant = event.participant_id
        if event.event_type is DeliberationEventType.RUN_CREATED:
            self._write_event_block(
                event,
                marker=">",
                title="Question received by the protocol",
                detail="The deliberation run has been created.",
                style="bold yellow",
                spaced=False,
            )
        elif event.event_type is DeliberationEventType.PHASE_STARTED and event.phase:
            title, detail, style = PHASE_TIMELINE[event.phase]
            self._write_event_block(
                event,
                marker="◉",
                title=title,
                detail=detail,
                style=style,
            )
        elif event.event_type is DeliberationEventType.PARTICIPANT_TURN_FAILED and participant:
            self._write_event_block(
                event,
                marker="!",
                title=f"{participant.upper()} could not complete this phase",
                detail=str(event.payload.get("error") or "Participant failed"),
                style="bold red",
            )
        elif event.event_type is DeliberationEventType.PHASE_COMPLETED and event.phase:
            successful = [str(name) for name in event.payload.get("successful", [])]
            failed = [str(name) for name in event.payload.get("failed", [])]
            total = int(event.payload.get("total", 0))
            self._write_event_block(
                event,
                marker="✓",
                title=f"{PHASE_COMPLETION_LABELS[event.phase]} ({len(successful)}/{total})",
                detail=PHASE_COMPLETION_DETAILS[event.phase],
                style="bold green",
            )
            self._write_participant_results(successful, failed)
        elif event.event_type is DeliberationEventType.DECISION_CREATED:
            self._write_event_block(
                event,
                marker="●",
                title="Recommendation ready",
                detail="The final decision record has been created.",
                style="bold green",
            )

    def write_message(self, message: str, *, style: str) -> None:
        self._append(Text(message, style=style), message, classes="timeline-message")

    def add_independent_reasoning(
        self,
        participant_id: str,
        position: Position,
        *,
        duration_seconds: float | None,
    ) -> None:
        self.add_phase_result(
            participant_id,
            Phase.INDEPENDENT,
            position,
            duration_seconds=duration_seconds,
        )

    def add_phase_result(
        self,
        participant_id: str,
        phase: Phase,
        payload: Position | Synthesis,
        *,
        duration_seconds: float | None,
    ) -> None:
        label = PHASE_RESULT_LABELS.get(phase)
        if label is None:
            return
        duration = f" · {duration_seconds:.1f}s" if duration_seconds is not None else ""
        title = (
            f"{participant_id.upper()} · {label}{duration} · "
            f"{payload.confidence.value} confidence"
        )
        if isinstance(payload, Position):
            content = _position_markdown(payload, revised=phase is Phase.REVISION)
        else:
            content = _synthesis_markdown(
                payload,
                reconciliation=phase is Phase.RECONCILIATION,
            )
        self._transcript.append(title)
        card = Collapsible(
            Markdown(content),
            title=title,
            collapsed=True,
            classes=f"reasoning-card {PHASE_RESULT_CLASSES[phase]}",
        )
        self.mount(card)
        self.call_after_refresh(card.scroll_visible, animate=False)

    def _write_event_block(
        self,
        event: DeliberationEvent,
        *,
        marker: str,
        title: str,
        detail: str,
        style: str,
        spaced: bool = True,
    ) -> None:
        heading = Text()
        heading.append(event.created_at.astimezone().strftime("%H:%M:%S"), style="bright_black")
        heading.append("   ")
        heading.append(f"{marker}  ", style=style)
        heading.append(title, style=style)
        heading.append("\n")
        heading.append("             ", style="bright_black")
        heading.append(detail, style="dim")
        classes = "timeline-event timeline-event-spaced" if spaced else "timeline-event"
        self._append(heading, f"{title}\n{detail}", classes=classes)

    def _write_participant_results(self, successful: list[str], failed: list[str]) -> None:
        results = Text("             ")
        for participant in successful:
            results.append(f"[ {participant.upper()}  ✓ ]  ", style="bold green")
        for participant in failed:
            results.append(f"[ {participant.upper()}  ✗ ]  ", style="bold red")
        if successful or failed:
            labels = [f"{participant.upper()} completed" for participant in successful]
            labels.extend(f"{participant.upper()} failed" for participant in failed)
            self._append(results, " · ".join(labels), classes="timeline-results")

    def _append(self, content: Text, transcript: str, *, classes: str) -> None:
        self._transcript.append(transcript)
        entry = Static(content, classes=classes)
        self.mount(entry)
        self.call_after_refresh(entry.scroll_visible, animate=False)


def _position_markdown(position: Position, *, revised: bool = False) -> str:
    sections = [
        "### Revised position" if revised else "### Proposal",
        position.recommendation,
        "",
        f"**Confidence:** {position.confidence.value} — {position.confidence_reason}",
    ]
    if revised:
        changed = "yes" if position.changed_position else "no"
        sections.extend(
            (
                "",
                f"**Changed position:** {changed}",
                f"**Why:** {position.change_reason}",
            )
        )
    if position.arguments:
        sections.extend(("", "### Supporting arguments"))
        for argument in position.arguments:
            sections.append(f"- {argument.claim}")
            for evidence in argument.evidence:
                sections.append(
                    f"  - Evidence: `{evidence.path}:{evidence.line_start}-{evidence.line_end}` "
                    f"— {evidence.explanation}"
                )
    _append_list(sections, "Alternatives", position.alternatives)
    _append_list(sections, "Disagreements", position.disagreements)
    _append_list(sections, "Assumptions", position.assumptions)
    _append_list(sections, "Risks", position.risks)
    return "\n".join(sections)


def _synthesis_markdown(synthesis: Synthesis, *, reconciliation: bool) -> str:
    sections = [
        "### Final reconciliation" if reconciliation else "### Cross-synthesis",
        synthesis.recommendation,
        "",
        f"**Confidence:** {synthesis.confidence.value} — {synthesis.confidence_reason}",
    ]
    if reconciliation:
        equivalent = (
            "not stated"
            if synthesis.equivalent_to_peer is None
            else "yes"
            if synthesis.equivalent_to_peer
            else "no"
        )
        sections.extend(("", f"**Equivalent to peer:** {equivalent}"))
    _append_list(
        sections,
        "Referenced arguments",
        [f"`{argument_id}`" for argument_id in synthesis.supporting_argument_ids],
    )
    if synthesis.evidence:
        sections.extend(("", "### Evidence"))
        for evidence in synthesis.evidence:
            sections.append(
                f"- `{evidence.path}:{evidence.line_start}-{evidence.line_end}` "
                f"— {evidence.explanation}"
            )
    _append_list(sections, "Alternatives", synthesis.alternatives)
    _append_list(sections, "Disagreements", synthesis.disagreements)
    _append_list(sections, "Material conflicts", synthesis.material_conflicts)
    _append_list(sections, "Assumptions", synthesis.assumptions)
    _append_list(sections, "Risks", synthesis.risks)
    return "\n".join(sections)


def _append_list(sections: list[str], heading: str, values: list[str]) -> None:
    if values:
        sections.extend(("", f"### {heading}"))
        sections.extend(f"- {value}" for value in values)
