from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ego.bridge import BridgeRequest, BridgeRuntime, JsonlWriter
from ego.cli import app
from ego.config import AppPaths
from ego.events import WorkEventStream
from ego.models import (
    Argument,
    AvailabilityStatus,
    Confidence,
    Evidence,
    InvestigationDraft,
    InvestigationFinding,
    InvestigationHypothesis,
    InvestigationHypothesisState,
    InvestigationPhase,
    InvestigationReview,
    InvestigationReviewBundle,
    InvestigationSynthesis,
    ParticipantAvailability,
    ParticipantTurnResult,
    Phase,
    Position,
    RunStatus,
    TurnRequest,
)
from ego.storage import Database


class BridgeParticipant:
    def __init__(self, participant_id: str, block: asyncio.Event | None = None) -> None:
        self.participant_id = participant_id
        self.block = block
        self.responding = asyncio.Event()

    async def probe(self) -> ParticipantAvailability:
        return ParticipantAvailability(
            participant_id=self.participant_id,
            status=AvailabilityStatus.AVAILABLE,
            binary=f"/fake/{self.participant_id}",
            version="1.0",
        )

    async def respond(self, request: TurnRequest) -> ParticipantTurnResult:
        self.responding.set()
        if self.block is not None:
            await self.block.wait()
        evidence = Evidence(
            path="fact.txt",
            line_start=1,
            line_end=1,
            explanation="The workspace declares strict mode.",
            critical=True,
        )
        if isinstance(request.phase, Phase):
            payload = Position(
                recommendation="Keep strict mode.",
                arguments=[
                    Argument(id="strict", claim="Strict mode is enabled.", evidence=[evidence])
                ],
                confidence=Confidence.HIGH,
                confidence_reason="Direct local evidence.",
                changed_position=False,
                change_reason="No challenge changed the supported position.",
            )
        else:
            finding = InvestigationFinding(
                claim="Strict mode is enabled.",
                explanation="The local file enables it.",
                evidence=[evidence],
                confidence=Confidence.HIGH,
            )
            hypothesis = InvestigationHypothesis(
                hypothesis="Strict mode explains the failure.",
                state=InvestigationHypothesisState.SUPPORTED,
                supporting_evidence=[evidence],
                explanation="The setting matches the observed failure.",
            )
            if request.phase in {
                InvestigationPhase.INDEPENDENT,
                InvestigationPhase.REVISION,
            }:
                payload = InvestigationDraft(
                    findings=[finding],
                    hypotheses=[hypothesis],
                    unknowns=["Whether a local override exists."],
                )
            elif request.phase is InvestigationPhase.PEER_CHALLENGE:
                payload = InvestigationReviewBundle(
                    reviews=[
                        InvestigationReview(
                            target_participant=name,
                            valid_points=["The setting is directly cited."],
                            challenges=["Check local overrides."],
                            missing_evidence=["No override evidence."],
                        )
                        for name in request.peer_investigations
                    ]
                )
            else:
                payload = InvestigationSynthesis(
                    facts=[finding],
                    probable_causes=[hypothesis],
                    unknowns=["Whether a local override exists."],
                    next_checks=["Read the local override configuration."],
                )
        return ParticipantTurnResult(
            participant_id=self.participant_id,
            phase=request.phase,
            payload=payload,
            raw_output=payload.model_dump_json(),
            duration_seconds=0.01,
        )


def bridge_runtime(
    app_paths: AppPaths,
    participants: dict[str, BridgeParticipant],
    lines: list[str],
) -> tuple[BridgeRuntime, Database]:
    stream = WorkEventStream()
    database = Database(app_paths, event_stream=stream)
    runtime = BridgeRuntime(database, participants, stream, JsonlWriter(lines.append))
    return runtime, database


@pytest.mark.asyncio
async def test_bridge_streams_committed_decision_events_before_result(
    app_paths: AppPaths, tmp_path: Path
) -> None:
    (tmp_path / "fact.txt").write_text("strict=true\n", encoding="utf-8")
    lines: list[str] = []
    runtime, database = bridge_runtime(app_paths, {"codex": BridgeParticipant("codex")}, lines)

    assert (
        await runtime.execute(
            BridgeRequest(
                request_id="request-1",
                agent_id="decision",
                question="Keep strict mode?",
                workspace=tmp_path,
            )
        )
        == 0
    )

    frames = [json.loads(line) for line in lines]
    assert frames[0]["kind"] == "accepted"
    assert frames[-1]["kind"] == "result"
    assert frames[-1]["result_kind"] == "decision"
    run_id = frames[-1]["run_id"]
    streamed_ids = [frame["event"]["event_id"] for frame in frames if frame["kind"] == "event"]
    assert streamed_ids == [event.event_id for event in database.get_run_events(run_id)]
    assert all(frame["kind"] != "result" for frame in frames[:-1])


@pytest.mark.asyncio
async def test_bridge_runs_all_five_investigation_stages(
    app_paths: AppPaths, tmp_path: Path
) -> None:
    (tmp_path / "fact.txt").write_text("strict=true\n", encoding="utf-8")
    lines: list[str] = []
    participants = {name: BridgeParticipant(name) for name in ("codex", "opencode")}
    runtime, database = bridge_runtime(app_paths, participants, lines)

    assert (
        await runtime.execute(
            BridgeRequest(
                agent_id="investigate",
                question="Why does it fail?",
                workspace=tmp_path,
            )
        )
        == 0
    )

    frames = [json.loads(line) for line in lines]
    result = frames[-1]
    assert result["kind"] == "result"
    assert result["result_kind"] == "investigation_report"
    assert result["result"]["status"] == "completed"
    stages = {
        frame["event"]["stage"]
        for frame in frames
        if frame["kind"] == "event" and frame["event"]["stage"] is not None
    }
    assert stages == {stage.value for stage in InvestigationPhase}
    assert database.list_decisions() == []


@pytest.mark.asyncio
async def test_bridge_rejects_unknown_participant(app_paths: AppPaths, tmp_path: Path) -> None:
    lines: list[str] = []
    runtime, database = bridge_runtime(app_paths, {"codex": BridgeParticipant("codex")}, lines)

    assert (
        await runtime.execute(
            BridgeRequest(
                agent_id="decision",
                question="Question",
                workspace=tmp_path,
                participant_ids=["missing"],
            )
        )
        == 2
    )

    assert json.loads(lines[-1])["code"] == "unknown_participants"
    assert database.list_runs() == []


@pytest.mark.asyncio
async def test_bridge_cancellation_interrupts_and_persists_run(
    app_paths: AppPaths, tmp_path: Path
) -> None:
    (tmp_path / "fact.txt").write_text("strict=true\n", encoding="utf-8")
    blocker = asyncio.Event()
    participant = BridgeParticipant("codex", blocker)
    lines: list[str] = []
    runtime, database = bridge_runtime(app_paths, {"codex": participant}, lines)
    task = asyncio.create_task(
        runtime.execute(
            BridgeRequest(
                request_id="cancel-me",
                agent_id="decision",
                question="Question",
                workspace=tmp_path,
            )
        )
    )
    await participant.responding.wait()

    task.cancel()

    assert await task == 130
    frame = json.loads(lines[-1])
    assert frame["kind"] == "cancelled"
    assert frame["run_id"]
    assert database.get_run(frame["run_id"])["status"] == RunStatus.INTERRUPTED.value


def test_bridge_cli_exposes_schema_and_jsonl_validation_error() -> None:
    runner = CliRunner()

    schema = runner.invoke(app, ["bridge", "--schema"])
    invalid = runner.invoke(app, ["bridge"], input="not json")

    assert schema.exit_code == 0
    assert json.loads(schema.stdout)["protocol_version"] == 1
    assert invalid.exit_code == 2
    assert json.loads(invalid.stdout)["code"] == "invalid_request"
