from __future__ import annotations

from dataclasses import dataclass, field

from ego.events import DeliberationEvent, DeliberationEventType
from ego.models import Phase

PHASES = (
    Phase.INDEPENDENT,
    Phase.PEER_REVIEW,
    Phase.REVISION,
    Phase.SYNTHESIS,
    Phase.RECONCILIATION,
)

PHASE_LABELS = {
    Phase.INDEPENDENT: "Independent reasoning",
    Phase.PEER_REVIEW: "Peer review",
    Phase.REVISION: "Position revision",
    Phase.SYNTHESIS: "Cross synthesis",
    Phase.RECONCILIATION: "Reconciliation",
}


@dataclass
class ParticipantState:
    status: str = "pending"
    detail: str = "Waiting"
    turns_completed: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    usage_reported: bool = False


@dataclass
class SessionState:
    run_id: str | None = None
    status: str = "ready"
    phase: Phase | None = None
    completed_phases: int = 0
    participants: dict[str, ParticipantState] = field(default_factory=dict)

    @property
    def phase_label(self) -> str:
        return PHASE_LABELS[self.phase] if self.phase else "Ready"

    def reset(self, participant_ids: list[str]) -> None:
        self.run_id = None
        self.status = "starting"
        self.phase = None
        self.completed_phases = 0
        self.participants = {name: ParticipantState() for name in participant_ids}

    def apply(self, event: DeliberationEvent) -> None:
        participant = event.participant_id
        if event.event_type is DeliberationEventType.RUN_CREATED:
            self.run_id = event.run_id
        elif event.event_type is DeliberationEventType.RUN_STATUS_CHANGED:
            self.status = str(event.payload["status"])
        elif event.event_type is DeliberationEventType.PARTICIPANT_PROBE_STARTED and participant:
            self._participant(participant).status = "checking"
            self._participant(participant).detail = "Checking availability"
        elif event.event_type is DeliberationEventType.PARTICIPANT_PROBE_COMPLETED and participant:
            state = self._participant(participant)
            state.status = str(event.payload["status"])
            state.detail = str(event.payload.get("reason") or state.status)
        elif event.event_type is DeliberationEventType.PHASE_STARTED and event.phase:
            self.phase = event.phase
            for name in event.payload.get("expected", []):
                self._participant(str(name)).detail = "Queued"
        elif event.event_type is DeliberationEventType.PARTICIPANT_TURN_STARTED and participant:
            state = self._participant(participant)
            state.status = "working"
            state.detail = self.phase_label
        elif event.event_type is DeliberationEventType.PARTICIPANT_TURN_COMPLETED and participant:
            state = self._participant(participant)
            state.status = "completed"
            state.turns_completed += 1
            duration = event.payload.get("duration_seconds")
            state.detail = f"Completed in {float(duration):.1f}s" if duration else "Completed"
            usage = event.payload.get("usage")
            if isinstance(usage, dict):
                state.usage_reported = True
                state.total_tokens += int(usage.get("total_tokens") or 0)
                state.cost_usd += float(usage.get("cost_usd") or 0)
        elif event.event_type is DeliberationEventType.PARTICIPANT_TURN_FAILED and participant:
            state = self._participant(participant)
            state.status = "failed"
            state.detail = str(event.payload.get("error") or "Participant failed")
        elif event.event_type is DeliberationEventType.PHASE_COMPLETED and event.phase:
            self.completed_phases = PHASES.index(event.phase) + 1
        elif event.event_type is DeliberationEventType.DECISION_CREATED:
            self.completed_phases = len(PHASES)

    def _participant(self, participant_id: str) -> ParticipantState:
        return self.participants.setdefault(participant_id, ParticipantState())
