import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ego.config import EgoConfig, ParticipantConfig
from ego.models import TurnRequest, UsageMetrics
from ego.participants.base import CliParticipant
from ego.runner import reduced_environment


class CodexParticipant(CliParticipant):
    participant_id = "codex"
    default_binary = "codex"
    requires_external_sandbox = True
    environment_keys = frozenset({"CODEX_HOME"})
    required_help_tokens = (
        "--config",
        "--dangerously-bypass-approvals-and-sandbox",
        "--ephemeral",
        "--output-schema",
        "--ignore-user-config",
    )

    def __init__(self, participant_config: ParticipantConfig, ego_config: EgoConfig) -> None:
        super().__init__(participant_config, ego_config)
        self._runtime_dirs: dict[str, tempfile.TemporaryDirectory[str]] = {}

    @staticmethod
    def _copy_authentication(runtime_home: Path) -> None:
        source_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
        auth_file = source_home / "auth.json"
        if auth_file.is_file():
            destination = runtime_home / "auth.json"
            shutil.copyfile(auth_file, destination)
            destination.chmod(0o600)

    @contextmanager
    def probe_environment(self) -> Iterator[dict[str, str]]:
        with tempfile.TemporaryDirectory(prefix="ego-codex-probe-") as directory:
            runtime_home = Path(directory)
            self._copy_authentication(runtime_home)
            environment = reduced_environment(self.environment_keys)
            environment.update({"HOME": directory, "CODEX_HOME": directory})
            yield environment

    def help_command(self, binary: str) -> list[str]:
        return [binary, "exec", "--help"]

    def auth_command(self, binary: str) -> list[str] | None:
        return [binary, "login", "status"]

    def command(self, binary: str, schema: dict[str, object], request: TurnRequest) -> list[str]:
        del request
        schema_path = self.schema_file(schema)
        runtime = tempfile.TemporaryDirectory(prefix="ego-codex-")
        runtime_home = Path(runtime.name)
        self._copy_authentication(runtime_home)
        self._runtime_dirs[schema_path] = runtime

        codex_command = [
            binary,
            "exec",
            "--config",
            'model_reasoning_effort="medium"',
            "--disable",
            "apps",
            "--disable",
            "browser_use",
            "--disable",
            "browser_use_external",
            "--disable",
            "computer_use",
            "--disable",
            "image_generation",
            "--disable",
            "in_app_browser",
            "--disable",
            "memories",
            "--disable",
            "multi_agent",
            "--disable",
            "standalone_web_search",
            "--dangerously-bypass-approvals-and-sandbox",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--json",
            "--output-schema",
            schema_path,
            "-",
        ]
        if self.config.model:
            codex_command[2:2] = ["--model", self.config.model]
        return [
            "/usr/bin/env",
            f"HOME={runtime_home}",
            f"CODEX_HOME={runtime_home}",
            *codex_command,
        ]

    def cleanup_command(self, command: list[str]) -> None:
        try:
            schema_index = command.index("--output-schema") + 1
            schema_path = command[schema_index]
        except ValueError, IndexError:
            return
        try:
            Path(schema_path).unlink(missing_ok=True)
        finally:
            runtime = self._runtime_dirs.pop(schema_path, None)
            if runtime is not None:
                runtime.cleanup()

    def extract_usage(self, stdout: str) -> UsageMetrics | None:
        for line in reversed(stdout.splitlines()):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("type") != "turn.completed":
                continue
            usage = event.get("usage")
            if not isinstance(usage, dict):
                return None
            input_tokens = _integer(usage.get("input_tokens"))
            output_tokens = _integer(usage.get("output_tokens"))
            cached_input_tokens = _integer(usage.get("cached_input_tokens"))
            return UsageMetrics(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_input_tokens=cached_input_tokens,
                total_tokens=input_tokens + output_tokens,
            )
        return None


def _integer(value: object) -> int:
    return value if isinstance(value, int) and value >= 0 else 0
