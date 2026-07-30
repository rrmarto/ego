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
    DecisionResolutionResultFrame,
    DecisionResolveParameters,
    DecisionTransitionParameters,
    DecisionTransitionResultFrame,
    DiagnosticResultFrame,
    RunCancelledFrame,
    RunCancelParameters,
    RunCancelResult,
    RunCancelResultFrame,
    RunDetailResultFrame,
    RunResultFrame,
    RunsEventsParameters,
    RunsEventsResultFrame,
    RunsGetParameters,
    RunsListParameters,
    RunsListResultFrame,
    RunStartParameters,
    SchemaResultFrame,
    ServiceDiagnostic,
    ServiceDiagnosticError,
    ServiceErrorFrame,
    ServiceParticipantDiagnostic,
    ServiceRequest,
    ServiceSeatbeltDiagnostic,
    service_contract_schema,
)
from ego.service_decisions import ServiceDecisionError, ServiceDecisionLifecycle
from ego.service_history import ServiceHistory, ServiceHistoryError
from ego.service_runs import ActiveRunCoordinator, RunSubscription, ServiceRunError

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
        run_coordinator: ActiveRunCoordinator | None = None,
        history: ServiceHistory | None = None,
        decisions: ServiceDecisionLifecycle | None = None,
    ) -> None:
        self.participants = participants
        self.credentials = credentials
        self.diagnostic_timeout_seconds = diagnostic_timeout_seconds
        self.executable = executable or resolve_ego_executable()
        self.sandbox_probe = sandbox_probe
        self.run_coordinator = run_coordinator
        self.history = history
        self.decisions = decisions

    def authentication_challenge(self) -> AuthenticationChallengeFrame:
        token = self.credentials.get_or_create()
        nonce = self.credentials.new_nonce()
        return AuthenticationChallengeFrame(
            nonce=nonce,
            proof=self.credentials.server_proof(token, nonce),
        )

    def authenticate_message(
        self, message: bytes, challenge_nonce: str
    ) -> ServiceRequest | ServiceErrorFrame:
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
        return request

    async def handle_message(self, message: bytes, challenge_nonce: str) -> BaseModel:
        request = self.authenticate_message(message, challenge_nonce)
        if isinstance(request, ServiceErrorFrame):
            return request
        return await self.handle_request(request)

    async def handle_request(self, request: ServiceRequest) -> BaseModel:
        if request.method == "schema":
            return SchemaResultFrame(
                request_id=request.request_id,
                result=service_contract_schema(),
            )
        if request.method == "run.cancel":
            if self.run_coordinator is None:
                return self._error(
                    request.request_id,
                    "workflow_unavailable",
                    "Workflow execution is not configured for this service runtime.",
                )
            params = request.params
            if not isinstance(params, RunCancelParameters):
                return self._error(
                    request.request_id,
                    "invalid_request",
                    "run.cancel requires a target_request_id.",
                )
            try:
                run_id = await self.run_coordinator.cancel(params.target_request_id)
            except ServiceRunError as error:
                return self._error(
                    request.request_id,
                    error.code,
                    str(error),
                    retryable=error.retryable,
                )
            return RunCancelResultFrame(
                request_id=request.request_id,
                result=RunCancelResult(
                    target_request_id=params.target_request_id,
                    run_id=run_id,
                ),
            )
        if request.method == "run.start":
            return self._error(
                request.request_id,
                "stream_required",
                "run.start must be handled as a streaming service request.",
            )
        if request.method in {"runs.list", "runs.get", "runs.events"}:
            if self.history is None:
                return self._error(
                    request.request_id,
                    "history_unavailable",
                    "Run history is not configured for this service runtime.",
                )
            try:
                if request.method == "runs.list":
                    params = request.params
                    if not isinstance(params, RunsListParameters):
                        raise ServiceHistoryError(
                            "invalid_request", "runs.list parameters are invalid."
                        )
                    return RunsListResultFrame(
                        request_id=request.request_id,
                        result=self.history.list_runs(params),
                    )
                if request.method == "runs.get":
                    params = request.params
                    if not isinstance(params, RunsGetParameters):
                        raise ServiceHistoryError(
                            "invalid_request", "runs.get parameters are invalid."
                        )
                    return RunDetailResultFrame(
                        request_id=request.request_id,
                        result=self.history.get_run(params),
                    )
                params = request.params
                if not isinstance(params, RunsEventsParameters):
                    raise ServiceHistoryError(
                        "invalid_request", "runs.events parameters are invalid."
                    )
                return RunsEventsResultFrame(
                    request_id=request.request_id,
                    result=self.history.get_events(params),
                )
            except ServiceHistoryError as error:
                return self._error(request.request_id, error.code, str(error))
        if request.method in {"decision.transition", "decision.resolve"}:
            if self.decisions is None:
                return self._error(
                    request.request_id,
                    "decision_lifecycle_unavailable",
                    "Decision lifecycle is not configured for this service runtime.",
                )
            try:
                if request.method == "decision.transition":
                    params = request.params
                    if not isinstance(params, DecisionTransitionParameters):
                        raise ServiceDecisionError(
                            "invalid_request",
                            "decision.transition parameters are invalid.",
                        )
                    return DecisionTransitionResultFrame(
                        request_id=request.request_id,
                        result=self.decisions.transition(params),
                    )
                params = request.params
                if not isinstance(params, DecisionResolveParameters):
                    raise ServiceDecisionError(
                        "invalid_request",
                        "decision.resolve parameters are invalid.",
                    )
                return DecisionResolutionResultFrame(
                    request_id=request.request_id,
                    result=self.decisions.resolve(params),
                )
            except ServiceDecisionError as error:
                return self._error(request.request_id, error.code, str(error))
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
            line = await self._read_message(reader, writer)
            if line is None:
                return
            request = self.runtime.authenticate_message(
                line.rstrip(b"\r\n"),
                challenge.nonce,
            )
            if isinstance(request, ServiceErrorFrame):
                await self._write(writer, request)
                return
            if request.method == "run.start":
                await self._stream_run(writer, request)
                return
            await self._write(writer, await self.runtime.handle_request(request))
        except (ConnectionError, TimeoutError):
            pass
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()

    async def _read_message(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> bytes | None:
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
            return None
        except ValueError:
            await self._write(
                writer,
                ServiceErrorFrame(
                    code="message_too_large",
                    message=f"Message exceeds {self.max_message_bytes} bytes.",
                ),
            )
            return None
        if not line:
            return None
        if len(line) > self.max_message_bytes:
            await self._write(
                writer,
                ServiceErrorFrame(
                    code="message_too_large",
                    message=f"Message exceeds {self.max_message_bytes} bytes.",
                ),
            )
            return None
        return line

    async def _stream_run(
        self,
        writer: asyncio.StreamWriter,
        request: ServiceRequest,
    ) -> None:
        coordinator = self.runtime.run_coordinator
        params = request.params
        if coordinator is None:
            await self._write(
                writer,
                self.runtime._error(
                    request.request_id,
                    "workflow_unavailable",
                    "Workflow execution is not configured for this service runtime.",
                ),
            )
            return
        if not isinstance(params, RunStartParameters):
            await self._write(
                writer,
                self.runtime._error(
                    request.request_id,
                    "invalid_request",
                    "run.start requires Decision parameters.",
                ),
            )
            return
        try:
            subscription = await coordinator.start(request.request_id, params)
        except ServiceRunError as error:
            await self._write(
                writer,
                self.runtime._error(
                    request.request_id,
                    error.code,
                    str(error),
                    retryable=error.retryable,
                ),
            )
            return
        await self._write(writer, subscription.accepted)
        await self._write_run_frames(writer, subscription)

    async def _write_run_frames(
        self,
        writer: asyncio.StreamWriter,
        subscription: RunSubscription,
    ) -> None:
        try:
            while True:
                frame = await subscription.next_frame()
                await self._write(writer, frame)
                if isinstance(
                    frame,
                    (RunResultFrame, RunCancelledFrame, ServiceErrorFrame),
                ):
                    return
        finally:
            subscription.detach()

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
