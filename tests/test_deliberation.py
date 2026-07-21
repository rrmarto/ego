from __future__ import annotations

from pathlib import Path

import pytest

from ego.deliberation import DeliberationEngine
from ego.models import (
    Argument,
    AvailabilityStatus,
    Confidence,
    Evidence,
    ParticipantAvailability,
    ParticipantTurnResult,
    PeerReview,
    PeerReviewBundle,
    Phase,
    Position,
    RunStatus,
    Synthesis,
    TurnRequest,
)
from ego.storage import Database


class FakeParticipant:
    def __init__(self, name: str, equivalent: bool = True) -> None:
        self.participant_id = name
        self.equivalent = equivalent

    async def probe(self) -> ParticipantAvailability:
        return ParticipantAvailability(
            participant_id=self.participant_id,
            status=AvailabilityStatus.AVAILABLE,
            binary=f"/fake/{self.participant_id}",
            version="1.0",
        )

    async def respond(self, request: TurnRequest) -> ParticipantTurnResult:
        citation = Evidence(
            path="architecture.txt",
            line_start=1,
            line_end=1,
            explanation="The architecture boundary is declared here.",
            critical=True,
        )
        if request.phase in {Phase.INDEPENDENT, Phase.REVISION}:
            payload = Position(
                recommendation="Keep the explicit module boundary.",
                arguments=[
                    Argument(id="boundary", claim="The boundary is explicit.", evidence=[citation])
                ],
                confidence=Confidence.HIGH,
                confidence_reason="Direct project evidence.",
                changed_position=False,
                change_reason="The peer evidence supports the initial position.",
            )
        elif request.phase is Phase.PEER_REVIEW:
            payload = PeerReviewBundle(
                reviews=[
                    PeerReview(
                        target_participant=name, valid_points=["Supported by architecture.txt"]
                    )
                    for name in request.peer_positions
                ]
            )
        else:
            payload = Synthesis(
                recommendation="Keep the explicit module boundary.",
                supporting_argument_ids=["boundary"],
                confidence=Confidence.HIGH,
                confidence_reason="Peers independently used the same source.",
                evidence=[citation],
                equivalent_to_peer=self.equivalent
                if request.phase is Phase.RECONCILIATION
                else None,
            )
        return ParticipantTurnResult(
            participant_id=self.participant_id,
            phase=request.phase,
            payload=payload,
            raw_output=payload.model_dump_json(),
            duration_seconds=0.01,
        )


@pytest.mark.asyncio
async def test_four_participant_deliberation_completes(database: Database, tmp_path: Path) -> None:
    (tmp_path / "architecture.txt").write_text("modules are isolated\n", encoding="utf-8")
    participants = {
        name: FakeParticipant(name) for name in ("codex", "claude", "gemini", "copilot")
    }
    engine = DeliberationEngine(database, participants)

    outcome = await engine.deliberate(
        question="Keep the boundary?",
        workspace=tmp_path,
        participant_ids=list(participants),
        command="ask",
    )

    assert outcome.final.status is RunStatus.COMPLETED
    assert outcome.final.confidence is Confidence.HIGH
    assert database.get_decision(outcome.decision_id)["state"] == "recommended"
    phases = {call["phase"] for call in database.get_run(outcome.final.run_id)["calls"]}
    assert phases == {phase.value for phase in Phase}


@pytest.mark.asyncio
async def test_single_participant_is_capped_at_low(database: Database, tmp_path: Path) -> None:
    (tmp_path / "architecture.txt").write_text("modules are isolated\n", encoding="utf-8")
    participants = {"codex": FakeParticipant("codex")}
    outcome = await DeliberationEngine(database, participants).deliberate(
        question="Keep the boundary?",
        workspace=tmp_path,
        participant_ids=["codex"],
        command="summon",
    )
    assert outcome.final.confidence is Confidence.LOW
    assert "individual consultation" in outcome.final.warnings[0].lower()


@pytest.mark.asyncio
async def test_non_equivalent_synthesis_is_contested(database: Database, tmp_path: Path) -> None:
    (tmp_path / "architecture.txt").write_text("modules are isolated\n", encoding="utf-8")
    participants = {
        "codex": FakeParticipant("codex", equivalent=False),
        "claude": FakeParticipant("claude", equivalent=False),
    }
    outcome = await DeliberationEngine(database, participants).deliberate(
        question="Keep the boundary?",
        workspace=tmp_path,
        participant_ids=list(participants),
        command="ask",
    )
    assert outcome.final.status is RunStatus.CONTESTED
    assert outcome.final.confidence is Confidence.LOW
