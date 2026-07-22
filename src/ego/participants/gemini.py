from ego.models import TurnRequest
from ego.participants.base import CliParticipant


class GeminiParticipant(CliParticipant):
    participant_id = "gemini"
    default_binary = "gemini"
    required_help_tokens = ("--approval-mode", "--output-format")

    def command(self, binary: str, schema: dict[str, object], request: TurnRequest) -> list[str]:
        del schema, request
        command = [binary, "--approval-mode", "plan", "--output-format", "json"]
        if self.config.model:
            command.extend(["--model", self.config.model])
        return command
