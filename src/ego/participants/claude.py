import json

from ego.participants.base import CliParticipant


class ClaudeParticipant(CliParticipant):
    participant_id = "claude"
    default_binary = "claude"
    required_help_tokens = ("--safe-mode", "--permission-mode", "--json-schema", "--tools")

    def auth_command(self, binary: str) -> list[str] | None:
        return [binary, "auth", "status"]

    def command(self, binary: str, schema: dict[str, object]) -> list[str]:
        command = [
            binary,
            "--print",
            "--safe-mode",
            "--permission-mode",
            "plan",
            "--tools",
            "Read,Glob,Grep",
            "--disable-slash-commands",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--no-session-persistence",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema),
        ]
        if self.config.model:
            command.extend(["--model", self.config.model])
        return command
