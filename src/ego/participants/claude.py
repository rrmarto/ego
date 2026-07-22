import json

from ego.models import Phase, TurnRequest, UsageMetrics
from ego.participants.base import CliParticipant


class ClaudeParticipant(CliParticipant):
    participant_id = "claude"
    default_binary = "claude"
    environment_keys = frozenset({"ANTHROPIC_API_KEY", "CLAUDE_CONFIG_DIR"})
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

    def extract_usage(self, stdout: str) -> UsageMetrics | None:
        try:
            envelope = json.loads(stdout.strip())
        except json.JSONDecodeError:
            return None
        if not isinstance(envelope, dict) or not isinstance(envelope.get("usage"), dict):
            return None
        usage = envelope["usage"]
        direct_input = _integer(usage.get("input_tokens"))
        cache_creation = _integer(usage.get("cache_creation_input_tokens"))
        cache_read = _integer(usage.get("cache_read_input_tokens"))
        output_tokens = _integer(usage.get("output_tokens"))
        input_tokens = direct_input + cache_creation + cache_read
        cost_value = envelope.get("total_cost_usd")
        cost_usd = float(cost_value) if isinstance(cost_value, int | float) else None
        return UsageMetrics(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cache_read,
            total_tokens=input_tokens + output_tokens,
            cost_usd=cost_usd,
        )


def _integer(value: object) -> int:
    return value if isinstance(value, int) and value >= 0 else 0
