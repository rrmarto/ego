from __future__ import annotations

from rich.text import Text
from textual.widgets import RichLog

from ego.events import DeliberationEvent, DeliberationEventType
from ego.models import Phase

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


class DeliberationTimeline(RichLog):
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
        self.write(Text(message, style=style))

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
        if spaced:
            self.write(Text(""))
        heading = Text()
        heading.append(event.created_at.astimezone().strftime("%H:%M:%S"), style="bright_black")
        heading.append("   ")
        heading.append(f"{marker}  ", style=style)
        heading.append(title, style=style)
        self.write(heading)
        description = Text()
        description.append("             ", style="bright_black")
        description.append(detail, style="dim")
        self.write(description)

    def _write_participant_results(self, successful: list[str], failed: list[str]) -> None:
        results = Text("             ")
        for participant in successful:
            results.append(f"[ {participant.upper()}  ✓ ]  ", style="bold green")
        for participant in failed:
            results.append(f"[ {participant.upper()}  ✗ ]  ", style="bold red")
        if successful or failed:
            self.write(results)
