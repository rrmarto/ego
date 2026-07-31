from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Iterable

from ego.events import WorkEventType
from ego.models import (
    AvailabilityStatus,
    ParticipantAvailability,
    ParticipantTurnResult,
    TurnRequest,
    WorkStage,
)
from ego.participants import Participant, ParticipantError
from ego.redaction import redact_sensitive_text
from ego.storage import Database


class NoParticipantsError(RuntimeError):
    pass


class AgentRuntime:
    """Shared participant execution boundary for every reproducible workflow."""

    def __init__(self, database: Database, participants: dict[str, Participant]) -> None:
        self.database = database
        self.participants = participants

    async def active_participants(
        self, run_id: str, participant_ids: list[str]
    ) -> dict[str, Participant]:
        selected = {name: self.participants[name] for name in participant_ids}
        availability = await asyncio.gather(
            *(self._probe_participant(run_id, item) for item in selected.values())
        )
        return {
            item.participant_id: selected[item.participant_id]
            for item in availability
            if item.status is AvailabilityStatus.AVAILABLE
        }

    async def parallel(
        self,
        run_id: str,
        stage: WorkStage,
        requests: dict[str, tuple[Participant, TurnRequest]],
    ) -> dict[str, ParticipantTurnResult]:
        expected = sorted(requests)
        self.database.add_event(
            run_id,
            WorkEventType.PHASE_STARTED,
            {
                "phase": stage.value,
                "stage": stage.value,
                "expected": expected,
                "total": len(expected),
            },
        )
        tasks: dict[str, asyncio.Task[ParticipantTurnResult | None]] = {}
        async with asyncio.TaskGroup() as group:
            for name, (participant, request) in requests.items():
                tasks[name] = group.create_task(self._invoke(run_id, participant, request))
        results = {
            name: result for name, task in tasks.items() if (result := task.result()) is not None
        }
        self.database.add_event(
            run_id,
            WorkEventType.PHASE_COMPLETED,
            {
                "phase": stage.value,
                "stage": stage.value,
                "successful": sorted(results),
                "failed": sorted(set(expected) - set(results)),
                "total": len(expected),
            },
        )
        return results

    async def _invoke(
        self, run_id: str, participant: Participant, request: TurnRequest
    ) -> ParticipantTurnResult | None:
        self.database.add_event(
            run_id,
            WorkEventType.PARTICIPANT_TURN_STARTED,
            {"phase": request.phase.value, "stage": request.phase.value},
            participant.participant_id,
        )
        try:
            result = await participant.respond(request)
            self.database.record_call(
                run_id,
                result,
                participant_id=participant.participant_id,
                phase=request.phase.value,
            )
            return result
        except (ParticipantError, OSError, ValueError) as error:
            self.database.record_call(
                run_id,
                None,
                participant_id=participant.participant_id,
                phase=request.phase.value,
                error=str(error),
                raw_output=(error.raw_output if isinstance(error, ParticipantError) else None),
                duration_seconds=(
                    error.duration_seconds if isinstance(error, ParticipantError) else None
                ),
                model=error.model if isinstance(error, ParticipantError) else None,
                usage=error.usage if isinstance(error, ParticipantError) else None,
            )
            return None

    async def _probe_participant(
        self, run_id: str, participant: Participant
    ) -> ParticipantAvailability:
        self.database.add_event(
            run_id,
            WorkEventType.PARTICIPANT_PROBE_STARTED,
            {},
            participant.participant_id,
        )
        try:
            availability = await participant.probe()
        except Exception as error:
            self.database.add_event(
                run_id,
                WorkEventType.PARTICIPANT_PROBE_COMPLETED,
                {
                    "status": AvailabilityStatus.UNKNOWN.value,
                    "error": redact_sensitive_text(str(error)),
                },
                participant.participant_id,
            )
            raise
        self.database.add_participant(run_id, availability)
        self.database.add_event(
            run_id,
            WorkEventType.PARTICIPANT_PROBE_COMPLETED,
            {
                "status": availability.status.value,
                "version": availability.version,
                "model": availability.model,
                "authentication": availability.authentication,
                "reason": availability.reason,
            },
            participant.participant_id,
        )
        return availability

    @staticmethod
    def rotating_pair(run_id: str, participant_ids: Iterable[str]) -> tuple[str, str]:
        ordered = sorted(participant_ids)
        start = int(hashlib.sha256(run_id.encode()).hexdigest()[:8], 16) % len(ordered)
        return ordered[start], ordered[(start + 1) % len(ordered)]
