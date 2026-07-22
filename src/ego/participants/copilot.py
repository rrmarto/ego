from ego.models import TurnRequest
from ego.participants.base import CliParticipant


class CopilotParticipant(CliParticipant):
    participant_id = "copilot"
    default_binary = "copilot"
    required_help_tokens = ("--deny-tool", "--no-ask-user")

    def command(self, binary: str, schema: dict[str, object], request: TurnRequest) -> list[str]:
        del schema, request
        command = [
            binary,
            "--silent",
            "--no-ask-user",
            "--deny-tool=shell,write,url,memory",
            "--excluded-tools=web_fetch,web_search",
        ]
        if self.config.model:
            command.append(f"--model={self.config.model}")
        return command
