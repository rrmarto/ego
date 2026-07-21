from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Protocol, cast

from pydantic import ValidationError

from ego.config import EgoConfig, ParticipantConfig
from ego.models import (
    AvailabilityStatus,
    ParticipantAvailability,
    ParticipantTurnResult,
    PeerReviewBundle,
    Position,
    Synthesis,
    TurnRequest,
)
from ego.prompts import build_prompt, response_model
from ego.runner import ProcessFailure, run_read_only
from ego.sandbox import SandboxProbe, probe_seatbelt


class ParticipantError(RuntimeError):
    pass


class Participant(Protocol):
    participant_id: str

    async def probe(self) -> ParticipantAvailability: ...

    async def respond(self, request: TurnRequest) -> ParticipantTurnResult: ...


_sandbox_probe: SandboxProbe | None = None
_sandbox_lock = asyncio.Lock()


async def shared_sandbox_probe() -> SandboxProbe:
    global _sandbox_probe
    async with _sandbox_lock:
        if _sandbox_probe is None:
            _sandbox_probe = await probe_seatbelt()
        return _sandbox_probe


class CliParticipant(ABC):
    participant_id: str
    default_binary: str
    required_help_tokens: tuple[str, ...]

    def __init__(self, participant_config: ParticipantConfig, ego_config: EgoConfig) -> None:
        self.config = participant_config
        self.ego_config = ego_config

    def resolve_binary(self) -> str | None:
        configured = self.config.binary
        if configured:
            path = Path(configured).expanduser()
            return str(path.resolve()) if path.is_file() else None
        return shutil.which(self.default_binary)

    async def _metadata(self, binary: str) -> tuple[str | None, str]:
        version_process = await asyncio.create_subprocess_exec(
            binary,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        version_out, _ = await version_process.communicate()
        help_process = await asyncio.create_subprocess_exec(
            *self.help_command(binary),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        help_out, _ = await help_process.communicate()
        version = version_out.decode(errors="replace").strip() or None
        return version, help_out.decode(errors="replace")

    def help_command(self, binary: str) -> list[str]:
        return [binary, "--help"]

    def auth_command(self, binary: str) -> list[str] | None:
        del binary
        return None

    async def _authentication(self, binary: str) -> tuple[str, str | None]:
        command = self.auth_command(binary)
        if command is None:
            return "unknown", None
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        await process.communicate()
        if process.returncode == 0:
            return "authenticated", "authentication detected"
        return "unauthenticated", "authentication status command reported no active login"

    async def probe(self) -> ParticipantAvailability:
        binary = self.resolve_binary()
        if not self.config.enabled:
            return ParticipantAvailability(
                participant_id=self.participant_id,
                status=AvailabilityStatus.UNAVAILABLE,
                reason="disabled in Ego configuration",
                model=self.config.model,
            )
        if binary is None:
            return ParticipantAvailability(
                participant_id=self.participant_id,
                status=AvailabilityStatus.UNAVAILABLE,
                reason=f"{self.default_binary} executable not found",
                model=self.config.model,
            )
        try:
            version, help_text = await self._metadata(binary)
        except OSError as error:
            return ParticipantAvailability(
                participant_id=self.participant_id,
                status=AvailabilityStatus.MISCONFIGURED,
                binary=binary,
                reason=str(error),
                model=self.config.model,
            )
        missing = [token for token in self.required_help_tokens if token not in help_text]
        if missing:
            return ParticipantAvailability(
                participant_id=self.participant_id,
                status=AvailabilityStatus.UNSUPPORTED,
                binary=binary,
                version=version,
                model=self.config.model,
                reason=f"missing required CLI options: {', '.join(missing)}",
            )
        sandbox = await shared_sandbox_probe()
        if not sandbox.safe:
            return ParticipantAvailability(
                participant_id=self.participant_id,
                status=AvailabilityStatus.UNSAFE,
                binary=binary,
                version=version,
                model=self.config.model,
                reason=sandbox.reason,
            )
        authentication, auth_detail = await self._authentication(binary)
        if authentication == "unauthenticated":
            return ParticipantAvailability(
                participant_id=self.participant_id,
                status=AvailabilityStatus.MISCONFIGURED,
                binary=binary,
                version=version,
                model=self.config.model,
                authentication="unauthenticated",
                reason=auth_detail,
            )
        return ParticipantAvailability(
            participant_id=self.participant_id,
            status=AvailabilityStatus.AVAILABLE,
            binary=binary,
            version=version,
            model=self.config.model,
            authentication=authentication,  # type: ignore[arg-type]
            reason=auth_detail or "authentication could not be checked without invoking the model",
        )

    @abstractmethod
    def command(self, binary: str, schema: dict[str, object]) -> list[str]:
        raise NotImplementedError

    def extract_json(self, stdout: str) -> object:
        stripped = stdout.strip()
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            for line in reversed(stripped.splitlines()):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    item = event.get("item")
                    if isinstance(item, dict) and item.get("type") == "agent_message":
                        return json.loads(str(item.get("text", "")))
            raise ParticipantError("CLI output did not contain a JSON response") from None

    def unwrap(self, value: object) -> object:
        if not isinstance(value, dict):
            return value
        if isinstance(value.get("structured_output"), dict):
            return value["structured_output"]
        for key in ("response", "result"):
            nested = value.get(key)
            if isinstance(nested, str):
                try:
                    return json.loads(nested)
                except json.JSONDecodeError:
                    continue
            if isinstance(nested, dict):
                return nested
        return value

    def cleanup_command(self, command: list[str]) -> None:
        del command

    async def respond(self, request: TurnRequest) -> ParticipantTurnResult:
        availability = await self.probe()
        if availability.status is not AvailabilityStatus.AVAILABLE or not availability.binary:
            raise ParticipantError(availability.reason or f"{self.participant_id} is unavailable")
        model_type = response_model(request.phase)
        schema = cast(dict[str, object], model_type.model_json_schema())
        errors: str | None = None
        raw_outputs: list[str] = []
        duration = 0.0
        for _ in range(2):
            prompt = build_prompt(request, correction=errors)
            try:
                command = self.command(availability.binary, schema)
                process = await run_read_only(
                    command,
                    workspace=request.workspace,
                    stdin=prompt,
                    timeout_seconds=self.config.timeout_seconds,
                    output_limit_bytes=self.ego_config.output_limit_bytes,
                )
                self.cleanup_command(command)
            except ProcessFailure as error:
                if "command" in locals():
                    self.cleanup_command(command)
                raise ParticipantError(str(error)) from error
            raw_outputs.append(process.stdout)
            duration += process.duration_seconds
            if process.returncode != 0:
                detail = process.stderr.strip() or process.stdout.strip()
                raise ParticipantError(f"CLI exited {process.returncode}: {detail[-1000:]}")
            try:
                parsed = self.unwrap(self.extract_json(process.stdout))
                payload = model_type.model_validate(parsed)
                return ParticipantTurnResult(
                    participant_id=self.participant_id,
                    phase=request.phase,
                    payload=cast(Position | PeerReviewBundle | Synthesis, payload),
                    raw_output="\n--- correction attempt ---\n".join(raw_outputs),
                    duration_seconds=duration,
                    model=self.config.model,
                )
            except (ParticipantError, ValidationError, json.JSONDecodeError) as error:
                errors = str(error)
        raise ParticipantError(f"invalid structured response after correction: {errors}")

    @staticmethod
    def schema_file(schema: dict[str, object]) -> str:
        handle = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        try:
            json.dump(schema, handle)
            return handle.name
        finally:
            handle.close()
