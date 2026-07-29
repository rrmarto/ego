from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter
from typer.testing import CliRunner

from ego.cli import app
from ego.config import AppPaths
from ego.models import (
    AvailabilityStatus,
    ParticipantAvailability,
    ParticipantCapabilities,
    ParticipantTurnResult,
    TurnRequest,
)
from ego.sandbox import SandboxProbe
from ego.service import LOOPBACK_HOST, EgoServiceServer, ServiceRuntime
from ego.service_auth import ServiceCredentialStore
from ego.service_contract import (
    ServiceDiagnostic,
    ServiceErrorFrame,
    ServiceRequest,
    service_contract_schema,
)


class DiagnosticParticipant:
    def __init__(self, availability: ParticipantAvailability) -> None:
        self.participant_id = availability.participant_id
        self.availability = availability

    async def probe(self) -> ParticipantAvailability:
        return self.availability

    async def respond(self, request: TurnRequest) -> ParticipantTurnResult:
        raise AssertionError(f"diagnostic must not invoke a participant: {request}")


async def safe_sandbox() -> SandboxProbe:
    return SandboxProbe(True, "read allowed and write denied")


def available_participant(name: str = "codex") -> DiagnosticParticipant:
    return DiagnosticParticipant(
        ParticipantAvailability(
            participant_id=name,
            status=AvailabilityStatus.AVAILABLE,
            binary=f"/fake/{name}",
            version="1.2.3",
            authentication="authenticated",
            capabilities=ParticipantCapabilities(
                structured_output=True,
                model_selection=True,
                file_reading=True,
                native_read_only=name != "opencode",
            ),
            reason="authentication detected",
        )
    )


async def start_service(
    app_paths: AppPaths,
    participants: dict[str, DiagnosticParticipant],
    *,
    max_message_bytes: int = 64 * 1024,
) -> tuple[EgoServiceServer, str]:
    credentials = ServiceCredentialStore(app_paths)
    token = credentials.get_or_create()
    runtime = ServiceRuntime(
        participants,
        credentials,
        diagnostic_timeout_seconds=1,
        executable="/fake/ego",
        sandbox_probe=safe_sandbox,
    )
    server = EgoServiceServer(
        runtime,
        port=0,
        max_message_bytes=max_message_bytes,
        request_timeout_seconds=1,
    )
    await server.start()
    return server, token


async def send_message(
    server: EgoServiceServer,
    message: dict[str, object] | bytes,
    *,
    token: str | None = None,
) -> dict[str, Any]:
    reader, writer = await asyncio.open_connection(*server.address)
    challenge = json.loads(await reader.readline())
    if isinstance(message, dict):
        payload = dict(message)
        if token is not None:
            nonce = challenge["nonce"]
            payload["authentication"] = {
                "nonce": nonce,
                "proof": ServiceCredentialStore.client_proof(
                    token,
                    nonce,
                    int(payload["protocol_version"]),
                    str(payload["request_id"]),
                    str(payload["method"]),
                ),
            }
        encoded = json.dumps(payload).encode()
    else:
        encoded = message
    writer.write(encoded + b"\n")
    await writer.drain()
    response = await reader.readline()
    writer.close()
    await writer.wait_closed()
    return json.loads(response)


def request(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "protocol_version": 1,
        "request_id": "request-1",
        "method": "diagnostic",
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_service_only_listens_on_ipv4_loopback(app_paths: AppPaths) -> None:
    server, _ = await start_service(app_paths, {})
    try:
        assert server.address[0] == LOOPBACK_HOST == "127.0.0.1"
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_authenticated_diagnostic_reports_versions_participants_and_seatbelt(
    app_paths: AppPaths,
) -> None:
    server, token = await start_service(
        app_paths,
        {
            "codex": available_participant(),
            "opencode": available_participant("opencode"),
        },
    )
    try:
        response = await send_message(server, request(), token=token)
    finally:
        await server.close()

    assert response["kind"] == "result"
    diagnostic = ServiceDiagnostic.model_validate(response["result"])
    assert diagnostic.service_protocol_version == 1
    assert diagnostic.ego_version == "0.1.0"
    assert diagnostic.bridge_protocol_version == 1
    assert diagnostic.ego_executable == "/fake/ego"
    assert diagnostic.seatbelt.safe
    assert [item.participant_id for item in diagnostic.participants] == ["codex", "opencode"]
    assert all(item.status is AvailabilityStatus.AVAILABLE for item in diagnostic.participants)
    assert diagnostic.participants[0].authentication == "authenticated"
    assert not diagnostic.participants[1].capabilities.native_read_only
    assert diagnostic.errors == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("credential", "code"),
    [(None, "missing_credentials"), ("incorrect", "invalid_credentials")],
)
async def test_service_rejects_missing_or_incorrect_credentials(
    app_paths: AppPaths, credential: str | None, code: str
) -> None:
    server, _ = await start_service(app_paths, {})
    try:
        response = await send_message(server, request(), token=credential)
    finally:
        await server.close()
    assert ServiceErrorFrame.model_validate(response).code == code


@pytest.mark.asyncio
async def test_service_accepts_compatible_protocol_and_rejects_incompatible_version(
    app_paths: AppPaths,
) -> None:
    server, token = await start_service(app_paths, {})
    try:
        compatible = await send_message(server, request(), token=token)
        incompatible = await send_message(
            server, request(protocol_version=2), token=token
        )
    finally:
        await server.close()
    assert compatible["protocol_version"] == 1
    assert incompatible["code"] == "incompatible_protocol"


@pytest.mark.asyncio
async def test_empty_participant_list_is_valid(app_paths: AppPaths) -> None:
    server, token = await start_service(app_paths, {})
    try:
        response = await send_message(server, request(), token=token)
    finally:
        await server.close()
    assert response["result"]["participants"] == []
    assert response["result"]["errors"] == []


@pytest.mark.asyncio
async def test_unsafe_participant_and_structured_reason_are_preserved(
    app_paths: AppPaths,
) -> None:
    unsafe = DiagnosticParticipant(
        ParticipantAvailability(
            participant_id="codex",
            status=AvailabilityStatus.UNSAFE,
            binary="/fake/codex",
            version="1.0",
            reason="sandbox_apply: Operation not permitted",
        )
    )
    server, token = await start_service(app_paths, {"codex": unsafe})
    try:
        response = await send_message(server, request(), token=token)
    finally:
        await server.close()
    participant = response["result"]["participants"][0]
    error = response["result"]["errors"][0]
    assert participant["status"] == "unsafe"
    assert participant["reason"] == "sandbox_apply: Operation not permitted"
    assert error == {
        "code": "participant_unsafe",
        "message": "sandbox_apply: Operation not permitted",
        "action": "Run Ego outside a parent App Sandbox and verify Seatbelt.",
        "participant_id": "codex",
    }


@pytest.mark.asyncio
async def test_service_rejects_oversized_message(app_paths: AppPaths) -> None:
    server, _ = await start_service(app_paths, {}, max_message_bytes=1024)
    try:
        response = await send_message(server, b"{" + b"x" * 1024)
    finally:
        await server.close()
    assert response["code"] == "message_too_large"


@pytest.mark.asyncio
async def test_service_rejects_invalid_json_and_unknown_method(app_paths: AppPaths) -> None:
    server, token = await start_service(app_paths, {})
    try:
        invalid = await send_message(server, b"not json")
        unknown = await send_message(server, request(method="execute"), token=token)
    finally:
        await server.close()
    assert invalid["code"] == "invalid_json"
    assert unknown["code"] == "unknown_method"


@pytest.mark.asyncio
async def test_service_closes_cleanly(app_paths: AppPaths) -> None:
    server, _ = await start_service(app_paths, {})
    address = server.address
    await server.close()
    with pytest.raises(OSError):
        await asyncio.open_connection(*address)


@pytest.mark.asyncio
async def test_authenticated_schema_is_decodable(app_paths: AppPaths) -> None:
    server, token = await start_service(app_paths, {})
    try:
        response = await send_message(server, request(method="schema"), token=token)
    finally:
        await server.close()
    encoded = json.dumps(response["result"])
    decoded = json.loads(encoded)
    assert decoded["protocol_version"] == 1
    assert decoded["methods"] == ["diagnostic", "schema"]
    TypeAdapter(dict[str, object]).validate_python(decoded)


@pytest.mark.asyncio
async def test_contract_has_no_arbitrary_command_path(app_paths: AppPaths) -> None:
    schema = service_contract_schema()
    assert schema["methods"] == ["diagnostic", "schema"]
    request_schema = schema["request"]
    assert isinstance(request_schema, dict)
    assert request_schema["additionalProperties"] is False
    assert "command" not in request_schema["properties"]
    assert "arguments" not in request_schema["properties"]

    server, token = await start_service(app_paths, {})
    try:
        response = await send_message(
            server,
            request(command="/bin/sh", arguments=["-c", "id"]),
            token=token,
        )
    finally:
        await server.close()
    assert response["code"] == "invalid_request"


def test_service_credential_permissions_and_explicit_rotation(app_paths: AppPaths) -> None:
    store = ServiceCredentialStore(app_paths)
    first = store.get_or_create()
    second = store.regenerate()

    assert first != second
    assert store.get_or_create() == second
    assert stat.S_IMODE(app_paths.data_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(app_paths.service_token_file.stat().st_mode) == 0o600


def test_service_cli_exposes_decodable_schema_and_token_rotation(tmp_path: Path) -> None:
    runner = CliRunner()
    environment = {"EGO_DATA_DIR": str(tmp_path / "ego-data")}

    schema = runner.invoke(app, ["service", "schema"], env=environment)
    first = runner.invoke(app, ["service", "token"], env=environment)
    second = runner.invoke(app, ["service", "token", "--regenerate"], env=environment)

    assert schema.exit_code == 0
    assert ServiceRequest.model_validate_json(
        json.dumps(
            {
                "protocol_version": json.loads(schema.stdout)["protocol_version"],
                "request_id": "schema-test",
                "method": "schema",
                "authentication": {
                    "nonce": "n" * 32,
                    "proof": "0" * 64,
                },
            }
        )
    )
    assert first.exit_code == second.exit_code == 0
    assert first.stdout.strip() != second.stdout.strip()
