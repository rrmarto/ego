import json

from ego.config import EgoConfig, ParticipantConfig
from ego.models import Confidence, Phase, Position
from ego.participants.codex import CodexParticipant


def test_codex_command_enforces_native_read_only_flags() -> None:
    participant = CodexParticipant(ParticipantConfig(model="test-model"), EgoConfig())
    command = participant.command("/usr/local/bin/codex", Position.model_json_schema())
    assert command[:2] == ["/usr/local/bin/codex", "exec"]
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert "--ephemeral" in command
    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    disabled = [command[index + 1] for index, value in enumerate(command) if value == "--disable"]
    assert {"apps", "browser_use", "computer_use", "multi_agent"} <= set(disabled)
    assert command[-1] == "-"


def test_codex_jsonl_agent_message_is_extracted() -> None:
    participant = CodexParticipant(ParticipantConfig(), EgoConfig())
    payload = Position(
        recommendation="A",
        confidence=Confidence.LOW,
        confidence_reason="One source",
    ).model_dump(mode="json")
    output = "\n".join(
        [
            json.dumps({"type": "thread.started"}),
            json.dumps({"item": {"type": "agent_message", "text": json.dumps(payload)}}),
        ]
    )
    assert participant.extract_json(output) == payload


def test_response_models_are_phase_specific() -> None:
    from ego.prompts import response_model

    assert response_model(Phase.INDEPENDENT) is Position
    assert response_model(Phase.REVISION) is Position


async def test_missing_binary_is_reported_generically() -> None:
    from ego.models import AvailabilityStatus

    participant = CodexParticipant(
        ParticipantConfig(binary="/definitely/not/installed/codex"), EgoConfig()
    )
    result = await participant.probe()
    assert result.status is AvailabilityStatus.UNAVAILABLE
    assert result.binary is None
