import json

from ego.models import Phase, TurnRequest
from ego.participants.base import CliParticipant


class ClaudeParticipant(CliParticipant):
    participant_id = "claude"
    default_binary = "claude"
    required_help_tokens = (
        "--safe-mode",
        "--permission-mode",
        "--json-schema",
        "--tools",
        "--effort",
    )

    def auth_command(self, binary: str) -> list[str] | None:
        return [binary, "auth", "status"]

    def command(self, binary: str, schema: dict[str, object], request: TurnRequest) -> list[str]:
        tools = {
            Phase.INDEPENDENT: "Read,Glob,Grep",
            Phase.PEER_REVIEW: "Read,Grep",
        }.get(request.phase, "")
        command = [
            binary,
            "--print",
            "--safe-mode",
            "--permission-mode",
            "plan",
            "--tools",
            tools,
            "--effort",
            "medium",
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
