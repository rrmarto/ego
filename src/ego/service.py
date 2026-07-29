from __future__ import annotations

import asyncio
import json
import shutil
import signal
import sys
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path

from pydantic import BaseModel, ValidationError

from ego import __version__
from ego.models import AvailabilityStatus, ParticipantAvailability
from ego.participants import Participant
from ego.participants.base import shared_sandbox_probe
from ego.redaction import redact_sensitive_text
from ego.sandbox import SandboxProbe
from ego.service_auth import ServiceCredentialStore
from ego.service_contract import (
    AuthenticationChallengeFrame,
    DiagnosticResultFrame,
    SchemaResultFrame,
    ServiceDiagnostic,
    ServiceDiagnosticError,
    ServiceErrorFrame,
    ServiceParticipantDiagnostic,
    ServiceRequest,
    ServiceSeatbeltDiagnostic,
    service_contract_schema,
)

LOOPBACK_HOST = "127.0.0.1"
SandboxProbeFactory = Callable[[], Awaitable[SandboxProbe]]


def resolve_ego_executable() -> str:
    invoked = Path(sys.argv[0]).expanduser()
    if invoked.is_absolute() or invoked.parent != Path("."):
        return str(invoked.resolve())
    discovered = shutil.which(sys.argv[0])
    return str(Path(discovered).resolve()) if discovered else str(Path(sys.executable).resolve())


def _participant_error(availability: ParticipantAvailability) -> ServiceDiagnosticError | None:
    if availability.status is AvailabilityStatus.AVAILABLE:
        return None
    actions = {
        AvailabilityStatus.UNAVAILABLE: "Install or enable the participant CLI, then retry.",
        AvailabilityStatus.MISCONFIGURED: "Check its absolute binary path and authentication.",
        AvailabilityStatus.UNSUPPORTED: "Install a supported participant CLI version.",
        AvailabilityStatus.UNSAFE: "Run Ego outside a parent App Sandbox and verify Seatbelt.",
        AvailabilityStatus.UNKNOWN: "Inspect the reason and rerun the diagnostic.",
    }
    return ServiceDiagnosticError(
        code=f"participant_{availability.status.value}",
        message=availability.reason or f"{availability.participant_id} is not available",
        action=actions[availability.status],
        participant_id=availability.participant_id,
    )


async def collect_service_diagnostic(
    participants: dict[str, Participant],
    *,
    executable: str,
    sandbox_probe: SandboxProbeFactory = shared_sandbox_probe,
) -> ServiceDiagnostic:
    sandbox, raw_results = await asyncio.gather(
        sandbox_probe(),
        asyncio.gather(
            *(participant.probe() for participant in participants.values()),
            return_exceptions=True,
        ),
    )
    availability: list[ParticipantAvailability] = []
    errors: list[ServiceDiagnosticError] = []
    for participant_id, result in zip(participants, raw_results, strict=True):
        if isinstance(result, BaseException):
            item = ParticipantAvailability(
                participant_id=participant_id,
                status=AvailabilityStatus.UNKNOWN,
                reason=redact_sensitive_text(str(result)),
            )
        else:
            item = result
        availability.append(item)
        if error := _participant_error(item):
            errors.append(error)
    if not sandbox.safe:
        errors.insert(
            0,
            ServiceDiagnosticError(
                code="seatbelt_unsafe",
                message=sandbox.reason,
                action="Run the Ego service outside a parent App Sandbox and retry.",
            ),
        )
    return ServiceDiagnostic(
        ego_version=__version__,
        ego_executable=executable,
        seatbelt=ServiceSeatbeltDiagnostic(safe=sandbox.safe, reason=sandbox.reason),
        participants=[
            ServiceParticipantDiagnostic.from_availability(item) for item in availability
        ],
        errors=errors,
    )


class ServiceRuntime:
    def __init__(
        self,
        participants: dict[str, Participant],
        credentials: ServiceCredentialStore,
        *,
        diagnostic_timeout_seconds: float,
        executable: str | None = None,
        sandbox_probe: SandboxProbeFactory = shared_sandbox_probe,
    ) -> None:
        self.participants = participants
        self.credentials = credentials
        self.diagnostic_timeout_seconds = diagnostic_timeout_seconds
        self.executable = executable or resolve_ego_executable()
        self.sandbox_probe = sandbox_probe

    def authentication_challenge(self) -> AuthenticationChallengeFrame:
        token = self.credentials.get_or_create()
        nonce = self.credentials.new_nonce()
        return AuthenticationChallengeFrame(
            nonce=nonce,
            proof=self.credentials.server_proof(token, nonce),
        )

    async def handle_message(self, message: bytes, challenge_nonce: str) -> BaseModel:
        request_id: str | None = None
        try:
            raw = json.loads(message)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            return self._error(None, "invalid_json", str(error))
        if isinstance(raw, dict):
            candidate = raw.get("request_id")
            if isinstance(candidate, str):
                request_id = candidate
            if raw.get("protocol_version") != 1:
                return self._error(
                    request_id,
                    "incompatible_protocol",
                    "Unsupported service protocol_version; this service requires version 1.",
                )
        try:
            request = ServiceRequest.model_validate(raw)
        except ValidationError as error:
            return self._error(request_id, "invalid_request", str(error))
        if request.authentication is None:
            return self._error(request.request_id, "missing_credentials", "Credential is required.")
        expected = self.credentials.get_or_create()
        authentication = request.authentication
        expected_proof = self.credentials.client_proof(
            expected,
            challenge_nonce,
            request.protocol_version,
            request.request_id,
            request.method,
        )
        if not self.credentials.matches(challenge_nonce, authentication.nonce) or not (
            self.credentials.matches(expected_proof, authentication.proof)
        ):
            return self._error(
                request.request_id, "invalid_credentials", "Credential was not accepted."
            )
        if request.method == "schema":
            return SchemaResultFrame(
                request_id=request.request_id,
                result=service_contract_schema(),
            )
        if request.method != "diagnostic":
            return self._error(
                request.request_id,
                "unknown_method",
                f"Unknown service method: {request.method}",
            )
        try:
            async with asyncio.timeout(self.diagnostic_timeout_seconds):
                diagnostic = await collect_service_diagnostic(
                    self.participants,
                    executable=self.executable,
                    sandbox_probe=self.sandbox_probe,
                )
        except TimeoutError:
            return self._error(
                request.request_id,
                "diagnostic_timeout",
                "The diagnostic did not complete before the configured timeout.",
                retryable=True,
            )
        return DiagnosticResultFrame(request_id=request.request_id, result=diagnostic)

    @staticmethod
    def _error(
        request_id: str | None,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> ServiceErrorFrame:
        return ServiceErrorFrame(
            request_id=request_id,
            code=code,
            message=redact_sensitive_text(message),
            retryable=retryable,
        )


class EgoServiceServer:
    def __init__(
        self,
        runtime: ServiceRuntime,
        *,
        port: int,
        max_message_bytes: int,
        request_timeout_seconds: float,
    ) -> None:
        self.runtime = runtime
        self.port = port
        self.max_message_bytes = max_message_bytes
        self.request_timeout_seconds = request_timeout_seconds
        self._server: asyncio.Server | None = None

    @property
    def address(self) -> tuple[str, int]:
        if self._server is None or not self._server.sockets:
            raise RuntimeError("service is not listening")
        host, port = self._server.sockets[0].getsockname()[:2]
        return str(host), int(port)

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client,
            host=LOOPBACK_HOST,
            port=self.port,
            limit=self.max_message_bytes + 1,
        )

    async def serve_forever(self) -> None:
        if self._server is None:
            raise RuntimeError("service has not been started")
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            challenge = self.runtime.authentication_challenge()
            await self._write(writer, challenge)
            while True:
                try:
                    line = await asyncio.wait_for(
                        reader.readline(), timeout=self.request_timeout_seconds
                    )
                except TimeoutError:
                    await self._write(
                        writer,
                        ServiceErrorFrame(
                            code="request_timeout",
                            message="No complete message arrived before the request timeout.",
                            retryable=True,
                        ),
                    )
                    break
                except ValueError:
                    await self._write(
                        writer,
                        ServiceErrorFrame(
                            code="message_too_large",
                            message=f"Message exceeds {self.max_message_bytes} bytes.",
                        ),
                    )
                    break
                if not line:
                    break
                if len(line) > self.max_message_bytes:
                    await self._write(
                        writer,
                        ServiceErrorFrame(
                            code="message_too_large",
                            message=f"Message exceeds {self.max_message_bytes} bytes.",
                        ),
                    )
                    break
                frame = await self.runtime.handle_message(
                    line.rstrip(b"\r\n"),
                    challenge.nonce,
                )
                await self._write(writer, frame)
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()

    async def _write(self, writer: asyncio.StreamWriter, frame: BaseModel) -> None:
        writer.write(frame.model_dump_json().encode() + b"\n")
        await asyncio.wait_for(writer.drain(), timeout=self.request_timeout_seconds)


async def run_service_until_stopped(server: EgoServiceServer) -> None:
    await server.start()
    host, port = server.address
    print(f"Ego service listening on {host}:{port}", flush=True)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for event in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(event, stopped.set)
    try:
        await stopped.wait()
    finally:
        await server.close()
