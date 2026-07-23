from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager

from ego.config import EgoConfig, ParticipantConfig
from ego.models import (
    AvailabilityStatus,
    ParticipantAvailability,
    ParticipantCapabilities,
    TurnRequest,
    UsageMetrics,
)
from ego.participants.base import CliParticipant, ParticipantError
from ego.participants.opencode_runtime import OpenCodeRuntime
from ego.runner import reduced_environment


class OpenCodeParticipant(CliParticipant):
    participant_id = "opencode"
    default_binary = "opencode"
    requires_external_sandbox = True
    required_help_tokens = ("--dir", "--format", "--pure")
    environment_keys = frozenset()

    def __init__(self, participant_config: ParticipantConfig, ego_config: EgoConfig) -> None:
        super().__init__(participant_config, ego_config)
        self._runtime = OpenCodeRuntime(participant_config.model)
        self._runtime_dirs: dict[str, tempfile.TemporaryDirectory[str]] = {}
        self.environment_keys = self._runtime.environment_keys
        self.resolved_model = self._runtime.resolved_model

    def help_command(self, binary: str) -> list[str]:
        return [binary, "run", "--help"]

    async def probe(self) -> ParticipantAvailability:
        availability = await super().probe()
        updates: dict[str, object] = {
            "model": self.resolved_model,
            "capabilities": ParticipantCapabilities(native_read_only=False),
        }
        if (
            self._runtime.config_error
            and not self.config.model
            and availability.status is AvailabilityStatus.AVAILABLE
        ):
            updates.update(
                {
                    "status": AvailabilityStatus.MISCONFIGURED,
                    "reason": self._runtime.config_error,
                }
            )
        return availability.model_copy(update=updates)

    @contextmanager
    def probe_environment(self) -> Iterator[dict[str, str]]:
        runtime, runtime_home = self._runtime.create(None)
        try:
            environment = reduced_environment(self.environment_keys)
            environment.update(self._runtime.environment(runtime_home))
            yield environment
        finally:
            runtime.cleanup()

    def command(self, binary: str, schema: dict[str, object], request: TurnRequest) -> list[str]:
        del schema
        runtime, runtime_home = self._runtime.create(request)
        project_root = runtime_home / "project"
        self._runtime_dirs[str(project_root)] = runtime
        command = [
            "/usr/bin/env",
            *(f"{key}={value}" for key, value in self._runtime.environment(runtime_home).items()),
            binary,
            "--pure",
            "run",
            "--format",
            "json",
            "--agent",
            "ego",
            "--dir",
            str(project_root),
            "--title",
            f"Ego {request.run_id}",
        ]
        if self.config.model:
            command.extend(["--model", self.config.model])
        return command

    def cleanup_command(self, command: list[str]) -> None:
        try:
            project_root = command[command.index("--dir") + 1]
        except (ValueError, IndexError):
            return
        runtime = self._runtime_dirs.pop(project_root, None)
        if runtime is not None:
            runtime.cleanup()

    def reported_model(self) -> str | None:
        return self.resolved_model

    def extract_json(self, stdout: str) -> object:
        for line in reversed(stdout.strip().splitlines()):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("type") != "text":
                continue
            part = event.get("part")
            if not isinstance(part, dict) or not isinstance(part.get("text"), str):
                continue
            try:
                return json.loads(_without_code_fence(part["text"]))
            except json.JSONDecodeError:
                continue
        raise ParticipantError("OpenCode output did not contain a JSON text event")

    def extract_usage(self, stdout: str) -> UsageMetrics | None:
        totals = {"input": 0, "output": 0, "cached": 0, "total": 0}
        found = False
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("type") != "step_finish":
                continue
            part = event.get("part")
            tokens = part.get("tokens") if isinstance(part, dict) else None
            if not isinstance(tokens, dict):
                continue
            found = True
            current_input = _integer(tokens.get("input"))
            current_output = _integer(tokens.get("output"))
            reasoning = _integer(tokens.get("reasoning"))
            cache = tokens.get("cache")
            cache_read = _integer(cache.get("read")) if isinstance(cache, dict) else 0
            reported_total = tokens.get("total")
            totals["input"] += current_input
            totals["output"] += current_output
            totals["cached"] += cache_read
            totals["total"] += (
                reported_total
                if isinstance(reported_total, int) and reported_total >= 0
                else current_input + current_output + reasoning + cache_read
            )
        if not found:
            return None
        return UsageMetrics(
            input_tokens=totals["input"],
            output_tokens=totals["output"],
            cached_input_tokens=totals["cached"],
            total_tokens=totals["total"],
        )


def _without_code_fence(value: str) -> str:
    stripped = value.strip()
    if not stripped.startswith("```") or not stripped.endswith("```"):
        return stripped
    first_newline = stripped.find("\n")
    return stripped[first_newline + 1 : -3].strip() if first_newline >= 0 else stripped


def _integer(value: object) -> int:
    return value if isinstance(value, int) and value >= 0 else 0
