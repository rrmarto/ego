from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from ego.config import AppPaths
from ego.events import WorkEventStream
from ego.models import (
    Argument,
    AvailabilityStatus,
    Confidence,
    Evidence,
    FinalDecision,
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
from ego.participants import Participant
from ego.service import EgoServiceServer, ServiceRuntime
from ego.service_auth import ServiceCredentialStore
from ego.service_contract import RunStartParameters
from ego.service_decisions import ServiceDecisionLifecycle
from ego.service_history import ServiceHistory
from ego.service_runs import ActiveRunCoordinator
from ego.storage import Database
from ego.workflow_execution import WorkflowExecutionRuntime


class DecisionParticipant:
    def __init__(self, participant_id: str, blocker: asyncio.Event | None = None) -> None:
        self.participant_id = participant_id
        self.blocker = blocker
        self.responding = asyncio.Event()
        self.probe_calls = 0

    async def probe(self) -> ParticipantAvailability:
        self.probe_calls += 1
        return ParticipantAvailability(
            participant_id=self.participant_id,
            status=AvailabilityStatus.AVAILABLE,
            binary=f"/fake/{self.participant_id}",
            version="1.0",
        )

    async def respond(self, request: TurnRequest) -> ParticipantTurnResult:
        self.responding.set()
        if self.blocker is not None:
            await self.blocker.wait()
        payload = {
            "recommendation": "Keep strict mode.",
            "arguments": [
                Argument(
                    id="strict",
                    claim="Strict mode is enabled.",
                    evidence=[
                        Evidence(
                            path="fact.txt",
                            line_start=1,
                            line_end=1,
                            explanation="The workspace declares strict mode.",
                            critical=True,
                        )
                    ],
                )
            ],
            "confidence": Confidence.HIGH,
            "confidence_reason": "Direct local evidence.",
            "changed_position": False,
            "change_reason": "No challenge changed the supported position.",
        }
        position = Position.model_validate(payload)
        return ParticipantTurnResult(
            participant_id=self.participant_id,
            phase=Phase.INDEPENDENT,
            payload=position,
            raw_output="PRIVATE PROVIDER OUTPUT MUST NOT CROSS THE SERVICE",
            duration_seconds=0.01,
        )


class InvestigationParticipant:
    def __init__(self, participant_id: str) -> None:
        self.participant_id = participant_id

    async def probe(self) -> ParticipantAvailability:
        return ParticipantAvailability(
            participant_id=self.participant_id,
            status=AvailabilityStatus.AVAILABLE,
            binary=f"/fake/{self.participant_id}",
            version="1.0",
        )

    async def respond(self, request: TurnRequest) -> ParticipantTurnResult:
        evidence = Evidence(
            path="fact.txt",
            line_start=1,
            line_end=1,
            explanation="The workspace declares strict mode.",
            critical=True,
        )
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
            raw_output="PRIVATE INVESTIGATION OUTPUT",
            duration_seconds=0.01,
        )


class UnsafeParticipant(DecisionParticipant):
    async def probe(self) -> ParticipantAvailability:
        self.probe_calls += 1
        return ParticipantAvailability(
            participant_id=self.participant_id,
            status=AvailabilityStatus.UNSAFE,
            reason="Seatbelt write probe was not denied.",
        )

    async def respond(self, request: TurnRequest) -> ParticipantTurnResult:
        raise AssertionError(f"unsafe participant must never execute: {request}")


async def workflow_service(
    app_paths: AppPaths,
    participants: dict[str, Participant],
) -> tuple[EgoServiceServer, Database, str]:
    credentials = ServiceCredentialStore(app_paths)
    token = credentials.get_or_create()
    event_stream = WorkEventStream()
    database = Database(app_paths, event_stream=event_stream)
    coordinator = ActiveRunCoordinator(
        WorkflowExecutionRuntime(database, participants, event_stream)
    )
    runtime = ServiceRuntime(
        participants,
        credentials,
        diagnostic_timeout_seconds=1,
        executable="/fake/ego",
        run_coordinator=coordinator,
        history=ServiceHistory(database),
        decisions=ServiceDecisionLifecycle(database),
    )
    server = EgoServiceServer(
        runtime,
        port=0,
        max_message_bytes=64 * 1024,
        request_timeout_seconds=1,
    )
    await server.start()
    return server, database, token


def run_start_request(workspace: Path, *, request_id: str = "run-1") -> dict[str, object]:
    return {
        "protocol_version": 1,
        "request_id": request_id,
        "method": "run.start",
        "params": {
            "agent_id": "decision",
            "question": "Keep strict mode?",
            "workspace": str(workspace),
            "participant_ids": ["codex"],
        },
    }


async def open_authenticated_request(
    server: EgoServiceServer,
    payload: dict[str, object],
    token: str | None,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_connection(*server.address)
    challenge = json.loads(await reader.readline())
    authenticated = dict(payload)
    if token is not None:
        authenticated["authentication"] = {
            "nonce": challenge["nonce"],
            "proof": ServiceCredentialStore.client_proof(
                token,
                challenge["nonce"],
                int(authenticated["protocol_version"]),
                str(authenticated["request_id"]),
                str(authenticated["method"]),
            ),
        }
    writer.write(json.dumps(authenticated).encode() + b"\n")
    await writer.drain()
    return reader, writer


async def read_terminal_frames(reader: asyncio.StreamReader) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    while True:
        line = await reader.readline()
        if not line:
            return frames
        frame = json.loads(line)
        frames.append(frame)
        if frame["kind"] in {"result", "error", "cancelled"}:
            return frames


async def send_authenticated_request(
    server: EgoServiceServer,
    payload: dict[str, object],
    token: str | None,
) -> dict[str, Any]:
    reader, writer = await open_authenticated_request(server, payload, token)
    response = json.loads(await reader.readline())
    await close_writer(writer)
    return response


async def close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio
async def test_coordinator_streams_decision_without_a_transport(
    app_paths: AppPaths, tmp_path: Path
) -> None:
    (tmp_path / "fact.txt").write_text("strict=true\n", encoding="utf-8")
    event_stream = WorkEventStream()
    database = Database(app_paths, event_stream=event_stream)
    coordinator = ActiveRunCoordinator(
        WorkflowExecutionRuntime(
            database,
            {"codex": DecisionParticipant("codex")},
            event_stream,
        )
    )

    subscription = await coordinator.start(
        "direct",
        RunStartParameters(
            agent_id="decision",
            question="Keep strict mode?",
            workspace=tmp_path,
            participant_ids=["codex"],
        ),
    )
    frames: list[dict[str, Any]] = [subscription.accepted.model_dump(mode="json")]
    while True:
        frame = await subscription.next_frame()
        frames.append(frame.model_dump(mode="json"))
        if frame.kind in {"result", "error", "cancelled"}:
            break

    assert frames[0]["kind"] == "accepted"
    assert frames[-1]["kind"] == "result"
    assert frames[-1]["result_kind"] == "decision"
    run_id = frames[-1]["run_id"]
    assert database.get_run(run_id)["status"] == RunStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_coordinator_cancellation_persists_interrupted_without_a_transport(
    app_paths: AppPaths, tmp_path: Path
) -> None:
    (tmp_path / "fact.txt").write_text("strict=true\n", encoding="utf-8")
    blocker = asyncio.Event()
    participant = DecisionParticipant("codex", blocker)
    event_stream = WorkEventStream()
    database = Database(app_paths, event_stream=event_stream)
    coordinator = ActiveRunCoordinator(
        WorkflowExecutionRuntime(database, {"codex": participant}, event_stream)
    )
    subscription = await coordinator.start(
        "direct-cancel",
        RunStartParameters(
            agent_id="decision",
            question="Keep strict mode?",
            workspace=tmp_path,
            participant_ids=["codex"],
        ),
    )
    await participant.responding.wait()

    run_id = await coordinator.cancel("direct-cancel")
    frames: list[dict[str, Any]] = []
    while True:
        frame = await subscription.next_frame()
        frames.append(frame.model_dump(mode="json"))
        if frame.kind in {"result", "error", "cancelled"}:
            break

    assert run_id
    assert frames[-1]["kind"] == "cancelled"
    assert frames[-1]["run_id"] == run_id
    assert database.get_run(run_id)["status"] == RunStatus.INTERRUPTED.value


@pytest.mark.asyncio
async def test_decision_run_streams_committed_events_and_typed_result(
    app_paths: AppPaths, tmp_path: Path
) -> None:
    (tmp_path / "fact.txt").write_text("strict=true\n", encoding="utf-8")
    server, database, token = await workflow_service(
        app_paths, {"codex": DecisionParticipant("codex")}
    )
    try:
        reader, writer = await open_authenticated_request(
            server, run_start_request(tmp_path), token
        )
        frames = await read_terminal_frames(reader)
        await close_writer(writer)
    finally:
        await server.close()

    assert frames[0]["kind"] == "accepted"
    assert frames[-1]["kind"] == "result"
    assert frames[-1]["result_kind"] == "decision"
    assert frames[-1]["decision_id"]
    assert all(frame["request_id"] == "run-1" for frame in frames)
    run_id = frames[-1]["run_id"]
    streamed = [frame["event"]["event_id"] for frame in frames if frame["kind"] == "event"]
    persisted = [event.event_id for event in database.get_run_events(run_id)]
    assert streamed == persisted
    assert "PRIVATE PROVIDER OUTPUT" not in json.dumps(frames)
    assert sum(frame["kind"] in {"result", "error", "cancelled"} for frame in frames) == 1


@pytest.mark.asyncio
async def test_service_allows_only_one_active_workflow(
    app_paths: AppPaths, tmp_path: Path
) -> None:
    (tmp_path / "fact.txt").write_text("strict=true\n", encoding="utf-8")
    blocker = asyncio.Event()
    participant = DecisionParticipant("codex", blocker)
    server, _, token = await workflow_service(app_paths, {"codex": participant})
    try:
        first_reader, first_writer = await open_authenticated_request(
            server, run_start_request(tmp_path, request_id="active"), token
        )
        assert json.loads(await first_reader.readline())["kind"] == "accepted"
        await participant.responding.wait()

        second_reader, second_writer = await open_authenticated_request(
            server, run_start_request(tmp_path, request_id="second"), token
        )
        busy = json.loads(await second_reader.readline())
        await close_writer(second_writer)

        blocker.set()
        terminal = await read_terminal_frames(first_reader)
        await close_writer(first_writer)
    finally:
        await server.close()

    assert busy["code"] == "service_busy"
    assert busy["retryable"] is True
    assert terminal[-1]["kind"] == "result"


@pytest.mark.asyncio
async def test_explicit_cancellation_interrupts_the_run(
    app_paths: AppPaths, tmp_path: Path
) -> None:
    (tmp_path / "fact.txt").write_text("strict=true\n", encoding="utf-8")
    blocker = asyncio.Event()
    participant = DecisionParticipant("codex", blocker)
    server, database, token = await workflow_service(app_paths, {"codex": participant})
    try:
        run_reader, run_writer = await open_authenticated_request(
            server, run_start_request(tmp_path, request_id="cancel-target"), token
        )
        assert json.loads(await run_reader.readline())["kind"] == "accepted"
        await participant.responding.wait()

        cancel_reader, cancel_writer = await open_authenticated_request(
            server,
            {
                "protocol_version": 1,
                "request_id": "cancel-request",
                "method": "run.cancel",
                "params": {"target_request_id": "cancel-target"},
            },
            token,
        )
        cancel_result = json.loads(await cancel_reader.readline())
        await close_writer(cancel_writer)
        terminal = await read_terminal_frames(run_reader)
        await close_writer(run_writer)
    finally:
        await server.close()

    assert cancel_result["kind"] == "result"
    assert cancel_result["result"]["status"] == "cancelled"
    assert terminal[-1]["kind"] == "cancelled"
    run_id = terminal[-1]["run_id"]
    assert run_id
    assert database.get_run(run_id)["status"] == RunStatus.INTERRUPTED.value


@pytest.mark.asyncio
async def test_disconnect_does_not_cancel_the_owned_run(
    app_paths: AppPaths, tmp_path: Path
) -> None:
    (tmp_path / "fact.txt").write_text("strict=true\n", encoding="utf-8")
    blocker = asyncio.Event()
    participant = DecisionParticipant("codex", blocker)
    server, database, token = await workflow_service(app_paths, {"codex": participant})
    try:
        reader, writer = await open_authenticated_request(
            server, run_start_request(tmp_path, request_id="detached"), token
        )
        assert json.loads(await reader.readline())["kind"] == "accepted"
        await participant.responding.wait()
        await close_writer(writer)
        blocker.set()

        async with asyncio.timeout(2):
            while True:
                rows = database.list_runs()
                if rows and rows[0]["status"] == RunStatus.COMPLETED.value:
                    break
                await asyncio.sleep(0.01)
    finally:
        await server.close()

    assert database.list_runs()[0]["status"] == RunStatus.COMPLETED.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("workspace", "participant_ids", "code"),
    [
        ("/definitely/missing/ego-workspace", ["codex"], "invalid_workspace"),
        (None, ["missing"], "unknown_participants"),
    ],
)
async def test_run_validation_fails_before_participant_execution(
    app_paths: AppPaths,
    tmp_path: Path,
    workspace: str | None,
    participant_ids: list[str],
    code: str,
) -> None:
    participant = DecisionParticipant("codex")
    server, database, token = await workflow_service(app_paths, {"codex": participant})
    payload = run_start_request(tmp_path)
    params = payload["params"]
    assert isinstance(params, dict)
    params["workspace"] = workspace or str(tmp_path)
    params["participant_ids"] = participant_ids
    try:
        reader, writer = await open_authenticated_request(server, payload, token)
        response = json.loads(await reader.readline())
        await close_writer(writer)
    finally:
        await server.close()

    assert response["code"] == code
    assert participant.probe_calls == 0
    assert database.list_runs() == []


@pytest.mark.asyncio
async def test_run_start_requires_authentication(app_paths: AppPaths, tmp_path: Path) -> None:
    participant = DecisionParticipant("codex")
    server, database, _ = await workflow_service(app_paths, {"codex": participant})
    try:
        reader, writer = await open_authenticated_request(
            server, run_start_request(tmp_path), None
        )
        response = json.loads(await reader.readline())
        await close_writer(writer)
    finally:
        await server.close()

    assert response["code"] == "missing_credentials"
    assert database.list_runs() == []


@pytest.mark.asyncio
async def test_run_authentication_rejects_mismatched_request_and_replayed_nonce(
    app_paths: AppPaths, tmp_path: Path
) -> None:
    server, database, token = await workflow_service(
        app_paths, {"codex": DecisionParticipant("codex")}
    )
    try:
        reader, writer = await asyncio.open_connection(*server.address)
        first_challenge = json.loads(await reader.readline())
        await close_writer(writer)

        mismatch_reader, mismatch_writer = await asyncio.open_connection(*server.address)
        mismatch_challenge = json.loads(await mismatch_reader.readline())
        mismatch = run_start_request(tmp_path, request_id="actual-request")
        mismatch["authentication"] = {
            "nonce": mismatch_challenge["nonce"],
            "proof": ServiceCredentialStore.client_proof(
                token,
                mismatch_challenge["nonce"],
                1,
                "different-request",
                "run.start",
            ),
        }
        mismatch_writer.write(json.dumps(mismatch).encode() + b"\n")
        await mismatch_writer.drain()
        mismatch_response = json.loads(await mismatch_reader.readline())
        await close_writer(mismatch_writer)

        replay_reader, replay_writer = await asyncio.open_connection(*server.address)
        await replay_reader.readline()
        replay = run_start_request(tmp_path, request_id="replayed")
        replay["authentication"] = {
            "nonce": first_challenge["nonce"],
            "proof": ServiceCredentialStore.client_proof(
                token,
                first_challenge["nonce"],
                1,
                "replayed",
                "run.start",
            ),
        }
        replay_writer.write(json.dumps(replay).encode() + b"\n")
        await replay_writer.drain()
        replay_response = json.loads(await replay_reader.readline())
        await close_writer(replay_writer)
    finally:
        await server.close()

    assert mismatch_response["code"] == "invalid_credentials"
    assert replay_response["code"] == "invalid_credentials"
    assert database.list_runs() == []


@pytest.mark.asyncio
async def test_fragmented_request_is_accepted_and_second_jsonl_frame_is_not_replayed(
    app_paths: AppPaths,
) -> None:
    server, _, token = await workflow_service(app_paths, {})
    try:
        reader, writer = await asyncio.open_connection(*server.address)
        challenge = json.loads(await reader.readline())
        payload = {
            "protocol_version": 1,
            "request_id": "fragmented",
            "method": "schema",
            "authentication": {
                "nonce": challenge["nonce"],
                "proof": ServiceCredentialStore.client_proof(
                    token,
                    challenge["nonce"],
                    1,
                    "fragmented",
                    "schema",
                ),
            },
        }
        encoded = json.dumps(payload).encode()
        midpoint = len(encoded) // 2
        writer.write(encoded[:midpoint])
        await writer.drain()
        writer.write(encoded[midpoint:] + b"\n" + encoded + b"\n")
        await writer.drain()
        first = json.loads(await reader.readline())
        second = await reader.readline()
        await close_writer(writer)
    finally:
        await server.close()

    assert first["kind"] == "result"
    assert first["request_id"] == "fragmented"
    assert second == b""


@pytest.mark.asyncio
async def test_unsafe_participant_fails_closed_without_execution(
    app_paths: AppPaths, tmp_path: Path
) -> None:
    participant = UnsafeParticipant("codex")
    server, database, token = await workflow_service(app_paths, {"codex": participant})
    try:
        reader, writer = await open_authenticated_request(
            server, run_start_request(tmp_path, request_id="unsafe"), token
        )
        frames = await read_terminal_frames(reader)
        await close_writer(writer)
    finally:
        await server.close()

    assert frames[0]["kind"] == "accepted"
    assert frames[-1]["code"] == "no_participants"
    run_id = next(frame["run_id"] for frame in frames if frame.get("run_id"))
    assert database.get_run(run_id)["status"] == RunStatus.FAILED.value


@pytest.mark.asyncio
async def test_investigation_reuses_the_same_service_execution_path(
    app_paths: AppPaths, tmp_path: Path
) -> None:
    (tmp_path / "fact.txt").write_text("strict=true\n", encoding="utf-8")
    participants = {
        name: InvestigationParticipant(name) for name in ("codex", "opencode")
    }
    server, database, token = await workflow_service(app_paths, participants)
    payload = run_start_request(tmp_path, request_id="investigation")
    params = payload["params"]
    assert isinstance(params, dict)
    params["agent_id"] = "investigate"
    params["question"] = "Why does strict mode fail?"
    params["participant_ids"] = list(participants)
    try:
        reader, writer = await open_authenticated_request(server, payload, token)
        frames = await read_terminal_frames(reader)
        await close_writer(writer)
    finally:
        await server.close()

    assert frames[0]["agent_id"] == "investigate"
    assert frames[0]["workflow_id"] == "investigation"
    assert frames[-1]["result_kind"] == "investigation_report"
    assert frames[-1]["decision_id"] is None
    assert "PRIVATE INVESTIGATION OUTPUT" not in json.dumps(frames)
    stages = {
        frame["event"]["stage"]
        for frame in frames
        if frame["kind"] == "event" and frame["event"]["stage"] is not None
    }
    assert stages == {stage.value for stage in InvestigationPhase}
    assert database.list_decisions() == []


@pytest.mark.asyncio
async def test_global_history_detail_and_incremental_events_are_bounded_public_models(
    app_paths: AppPaths, tmp_path: Path
) -> None:
    (tmp_path / "fact.txt").write_text("strict=true\n", encoding="utf-8")
    server, database, token = await workflow_service(
        app_paths, {"codex": DecisionParticipant("codex")}
    )
    try:
        reader, writer = await open_authenticated_request(
            server, run_start_request(tmp_path, request_id="history-run"), token
        )
        frames = await read_terminal_frames(reader)
        await close_writer(writer)
        run_id = frames[-1]["run_id"]

        other_id = database.create_run(
            command="cli",
            question="A run created by another Ego interface",
            workspace=tmp_path,
        )
        database.set_run_status(other_id, RunStatus.FAILED)

        first_page = await send_authenticated_request(
            server,
            {
                "protocol_version": 1,
                "request_id": "history-list-1",
                "method": "runs.list",
                "params": {"limit": 1},
            },
            token,
        )
        second_page = await send_authenticated_request(
            server,
            {
                "protocol_version": 1,
                "request_id": "history-list-2",
                "method": "runs.list",
                "params": {
                    "limit": 1,
                    "cursor": first_page["result"]["next_cursor"],
                },
            },
            token,
        )
        detail = await send_authenticated_request(
            server,
            {
                "protocol_version": 1,
                "request_id": "history-detail",
                "method": "runs.get",
                "params": {"run_id": run_id},
            },
            token,
        )
        first_event_id = database.get_run_events(run_id)[0].event_id
        events = await send_authenticated_request(
            server,
            {
                "protocol_version": 1,
                "request_id": "history-events",
                "method": "runs.events",
                "params": {
                    "run_id": run_id,
                    "after_event_id": first_event_id,
                    "limit": 2,
                },
            },
            token,
        )
    finally:
        await server.close()

    listed_ids = {
        first_page["result"]["runs"][0]["run_id"],
        second_page["result"]["runs"][0]["run_id"],
    }
    assert listed_ids == {run_id, other_id}
    assert detail["result"]["run_id"] == run_id
    assert detail["result"]["result"]["run_id"] == run_id
    serialized_detail = json.dumps(detail)
    assert "raw_path" not in serialized_detail
    assert "result_json" not in serialized_detail
    assert "final_json" not in serialized_detail
    assert "calls" not in detail["result"]
    assert len(events["result"]["events"]) == 2
    assert all(
        event["event_id"] > first_event_id for event in events["result"]["events"]
    )
    assert (
        events["result"]["next_after_event_id"]
        == events["result"]["events"][-1]["event_id"]
    )


@pytest.mark.asyncio
async def test_human_decision_transition_and_contested_resolution_are_append_only(
    app_paths: AppPaths, tmp_path: Path
) -> None:
    server, database, token = await workflow_service(app_paths, {})
    run_id = database.create_run(
        command="ask",
        question="Choose an approach",
        workspace=tmp_path,
    )
    final = FinalDecision(
        run_id=run_id,
        status=RunStatus.CONTESTED,
        recommendation="Material disagreement remains.",
        alternatives=["Use approach A", "Use approach B"],
        disagreements=["The approaches have different operational risks."],
        confidence=Confidence.LOW,
        confidence_reason="The participants did not reconcile.",
        requires_human_resolution=True,
    )
    database.set_run_status(run_id, RunStatus.CONTESTED, final=final)
    decision_id = database.create_decision(final)
    original_record = database.get_decision(decision_id)["record"]
    try:
        invalid_accept = await send_authenticated_request(
            server,
            {
                "protocol_version": 1,
                "request_id": "transition-invalid",
                "method": "decision.transition",
                "params": {
                    "decision_id": decision_id,
                    "state": "accepted",
                },
            },
            token,
        )
        resolution = await send_authenticated_request(
            server,
            {
                "protocol_version": 1,
                "request_id": "resolve",
                "method": "decision.resolve",
                "params": {
                    "decision_id": decision_id,
                    "alternative_index": 2,
                    "note": "Human selected the lower operational risk.",
                },
            },
            token,
        )
    finally:
        await server.close()

    stored = database.get_decision(decision_id)
    assert invalid_accept["code"] == "invalid_decision_transition"
    assert resolution["result"]["recommendation"] == "Use approach B"
    assert resolution["result"]["state"] == "accepted"
    assert stored["state"] == "accepted"
    assert stored["record"] == original_record
    assert [event["state"] for event in stored["events"]] == [
        "recommended",
        "accepted",
    ]
    assert len(stored["resolutions"]) == 1
