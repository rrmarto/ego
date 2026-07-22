import json

import pytest

from ego.config import EgoConfig, ParticipantConfig
from ego.models import (
    Argument,
    AvailabilityStatus,
    Confidence,
    ParticipantAvailability,
    Phase,
    Position,
    ProcessResult,
    TurnRequest,
)
from ego.participants.claude import ClaudeParticipant
from ego.participants.codex import CodexParticipant
from ego.prompts import validate_response


def test_codex_command_enforces_native_read_only_flags() -> None:
    participant = CodexParticipant(ParticipantConfig(model="test-model"), EgoConfig())
    request = TurnRequest(
        run_id="run",
        phase=Phase.INDEPENDENT,
        question="Question?",
        workspace=".",
    )
    command = participant.command("/usr/local/bin/codex", Position.model_json_schema(), request)
    assert command[:2] == ["/usr/local/bin/codex", "exec"]
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert "--ephemeral" in command
    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    assert command[command.index("--config") + 1] == 'model_reasoning_effort="medium"'
    disabled = [command[index + 1] for index, value in enumerate(command) if value == "--disable"]
    assert {"apps", "browser_use", "computer_use", "multi_agent"} <= set(disabled)
    assert command[-1] == "-"


def test_claude_tools_are_limited_by_phase() -> None:
    participant = ClaudeParticipant(ParticipantConfig(), EgoConfig())
    independent = TurnRequest(
        run_id="run",
        phase=Phase.INDEPENDENT,
        question="Question?",
        workspace=".",
    )
    revision = independent.model_copy(
        update={
            "phase": Phase.REVISION,
            "own_position": Position(
                recommendation="Keep it.",
                arguments=[Argument(id="a1", claim="Supported")],
                confidence=Confidence.MODERATE,
                confidence_reason="Supported by the current evidence.",
            ),
        }
    )

    independent_command = participant.command("/usr/local/bin/claude", {}, independent)
    revision_command = participant.command("/usr/local/bin/claude", {}, revision)

    assert independent_command[independent_command.index("--tools") + 1] == "Read,Glob,Grep"
    assert revision_command[revision_command.index("--tools") + 1] == ""
    assert revision_command[revision_command.index("--effort") + 1] == "medium"


def test_degraded_unchanged_revision_is_rejected() -> None:
    previous = Position(
        recommendation="Keep Textual.",
        arguments=[Argument(id="event-model", claim="It fits the event model.")],
        confidence=Confidence.MODERATE,
        confidence_reason="The architecture supports this recommendation.",
    )
    request = TurnRequest(
        run_id="run",
        phase=Phase.REVISION,
        question="Which TUI should we use?",
        workspace=".",
        own_position=previous,
    )
    degraded = Position(
        recommendation="Test minimal recommendation to isolate schema issue.",
        arguments=[Argument(id="a1", claim="test")],
        confidence=Confidence.HIGH,
        confidence_reason="test",
        changed_position=False,
        change_reason="test",
    )

    with pytest.raises(ValueError, match="substantive change reason"):
        validate_response(request, degraded)


def test_maintained_revision_preserves_argument_continuity() -> None:
    previous = Position(
        recommendation="Keep Textual.",
        arguments=[Argument(id="event-model", claim="It fits the event model.")],
        confidence=Confidence.MODERATE,
        confidence_reason="The architecture supports this recommendation.",
    )
    request = TurnRequest(
        run_id="run",
        phase=Phase.REVISION,
        question="Which TUI should we use?",
        workspace=".",
        own_position=previous,
    )
    disconnected = Position(
        recommendation="Keep Textual.",
        arguments=[Argument(id="new", claim="A disconnected argument.")],
        confidence=Confidence.MODERATE,
        confidence_reason="The available evidence still supports this recommendation.",
        changed_position=False,
        change_reason="The original position remains stronger after review.",
    )

    with pytest.raises(ValueError, match="preserve at least one prior argument id"):
        validate_response(request, disconnected)


@pytest.mark.asyncio
async def test_degraded_revision_receives_one_corrective_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    participant = ClaudeParticipant(ParticipantConfig(), EgoConfig())
    previous = Position(
        recommendation="Keep Textual.",
        arguments=[Argument(id="event-model", claim="It fits the event model.")],
        confidence=Confidence.MODERATE,
        confidence_reason="The architecture supports this recommendation.",
    )
    request = TurnRequest(
        run_id="run",
        phase=Phase.REVISION,
        question="Which TUI should we use?",
        workspace=".",
        own_position=previous,
    )
    degraded = Position(
        recommendation="Test minimal recommendation to isolate schema issue.",
        arguments=[Argument(id="a1", claim="test")],
        confidence=Confidence.HIGH,
        confidence_reason="test",
        changed_position=False,
        change_reason="test",
    )
    corrected = previous.model_copy(
        update={"change_reason": "The original position remains stronger after review."}
    )
    prompts: list[str] = []

    async def fake_probe() -> ParticipantAvailability:
        return ParticipantAvailability(
            participant_id="claude",
            status=AvailabilityStatus.AVAILABLE,
            binary="/usr/local/bin/claude",
        )

    async def fake_run(
        command: list[str],
        *,
        workspace: object,
        stdin: str,
        timeout_seconds: float,
        output_limit_bytes: int,
    ) -> ProcessResult:
        del workspace, timeout_seconds, output_limit_bytes
        prompts.append(stdin)
        payload = degraded if len(prompts) == 1 else corrected
        return ProcessResult(
            command=command,
            returncode=0,
            stdout=json.dumps({"structured_output": payload.model_dump(mode="json")}),
            stderr="",
            duration_seconds=0.01,
        )

    monkeypatch.setattr(participant, "probe", fake_probe)
    monkeypatch.setattr("ego.participants.base.run_read_only", fake_run)

    result = await participant.respond(request)

    assert result.payload == corrected
    assert len(prompts) == 2
    assert "Previous response validation error" in prompts[1]


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


def test_response_schemas_are_strict_for_every_phase() -> None:
    from ego.prompts import response_schema

    for phase in Phase:
        _assert_strict_objects(response_schema(phase))


def _assert_strict_objects(value: object) -> None:
    if isinstance(value, list):
        for item in value:
            _assert_strict_objects(item)
        return
    if not isinstance(value, dict):
        return

    assert "default" not in value
    properties = value.get("properties")
    if value.get("type") == "object" and isinstance(properties, dict):
        assert value.get("additionalProperties") is False
        assert value.get("required") == list(properties)
    for item in value.values():
        _assert_strict_objects(item)


async def test_missing_binary_is_reported_generically() -> None:
    from ego.models import AvailabilityStatus

    participant = CodexParticipant(
        ParticipantConfig(binary="/definitely/not/installed/codex"), EgoConfig()
    )
    result = await participant.probe()
    assert result.status is AvailabilityStatus.UNAVAILABLE
    assert result.binary is None
