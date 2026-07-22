from ego.models import TurnRequest
from ego.participants.base import CliParticipant


class CodexParticipant(CliParticipant):
    participant_id = "codex"
    default_binary = "codex"
    required_help_tokens = (
        "--config",
        "--sandbox",
        "--ephemeral",
        "--output-schema",
        "--ignore-user-config",
    )

    def help_command(self, binary: str) -> list[str]:
        return [binary, "exec", "--help"]

    def auth_command(self, binary: str) -> list[str] | None:
        return [binary, "login", "status"]

    def command(self, binary: str, schema: dict[str, object], request: TurnRequest) -> list[str]:
        del request
        command = [
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
            "--sandbox",
            "read-only",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--json",
            "--output-schema",
            self.schema_file(schema),
            "-",
        ]
        if self.config.model:
            command[2:2] = ["--model", self.config.model]
        return command

    def cleanup_command(self, command: list[str]) -> None:
        from pathlib import Path

        try:
            schema_index = command.index("--output-schema") + 1
            Path(command[schema_index]).unlink(missing_ok=True)
        except ValueError, IndexError:
            pass
