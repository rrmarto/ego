import asyncio
import json
import stat
from pathlib import Path

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
    Synthesis,
    TurnRequest,
    UsageMetrics,
)
from ego.participants.claude import ClaudeParticipant
from ego.participants.codex import CodexParticipant
from ego.participants.opencode import OpenCodeParticipant
from ego.participants.registry import build_participants
from ego.prompts import build_prompt, validate_response


def test_prompt_requires_falsification_and_explains_citation_scope() -> None:
    prompt = build_prompt(
        TurnRequest(
            run_id="run",
            phase=Phase.INDEPENDENT,
            question="Is this syntax valid?",
            workspace=".",
        )
    )
    normalized = prompt.replace("\n", " ")

    assert "actively try to falsify" in prompt
    assert "runtime" in prompt and "version" in prompt
    assert "does not prove" in normalized
    assert "Model agreement is not independent proof" in normalized


def test_codex_command_uses_only_ego_external_sandbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source_home = tmp_path / "source-codex-home"
    source_home.mkdir()
    auth_file = source_home / "auth.json"
    auth_file.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(source_home))
    participant = CodexParticipant(ParticipantConfig(model="test-model"), EgoConfig())
    request = TurnRequest(
        run_id="run",
        phase=Phase.INDEPENDENT,
        question="Question?",
        workspace=".",
    )
    command = participant.command("/usr/local/bin/codex", Position.model_json_schema(), request)
    assert command[0] == "/usr/bin/env"
    runtime_home = Path(command[1].removeprefix("HOME="))
    assert command[2] == f"CODEX_HOME={runtime_home}"
    assert runtime_home != source_home
    runtime_auth = runtime_home / "auth.json"
    assert runtime_auth.read_text(encoding="utf-8") == "{}"
    assert stat.S_IMODE(runtime_auth.stat().st_mode) == 0o600
    assert command[3:5] == ["/usr/local/bin/codex", "exec"]
    assert "--dangerously-bypass-approvals-and-sandbox" in command
    assert "--sandbox" not in command
    assert participant.requires_external_sandbox
    assert "--ephemeral" in command
    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    assert command[command.index("--config") + 1] == 'model_reasoning_effort="medium"'
    disabled = [command[index + 1] for index, value in enumerate(command) if value == "--disable"]
    assert {"apps", "browser_use", "computer_use", "multi_agent"} <= set(disabled)
    assert command[-1] == "-"
    participant.cleanup_command(command)
    assert not runtime_home.exists()

    with participant.probe_environment() as environment:
        probe_home = Path(environment["CODEX_HOME"])
        assert environment["HOME"] == str(probe_home)
        assert probe_home != source_home
        assert (probe_home / "auth.json").read_text(encoding="utf-8") == "{}"
    assert not probe_home.exists()


def test_opencode_command_uses_default_model_in_an_isolated_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source_home = tmp_path / "source-home"
    source_config = tmp_path / "source-config"
    source_data = tmp_path / "source-data"
    source_state = tmp_path / "source-state"
    (source_config / "opencode").mkdir(parents=True)
    (source_data / "opencode").mkdir(parents=True)
    (source_state / "opencode").mkdir(parents=True)
    source_home.mkdir()
    source_jsonc = json.dumps(
        {
                "model": "custom/default-model",
                "provider": {
                    "custom": {
                        "npm": "@ai-sdk/openai-compatible",
                        "options": {
                            "baseURL": "http://localhost:11434/v1",
                            "apiKey": "{env:CUSTOM_PROVIDER_KEY}",
                        },
                        "models": {"default-model": {"name": "Default"}},
                    }
                },
                "plugin": ["must-not-load"],
                "mcp": {
                    "must-not-start": {
                        "type": "local",
                        "command": ["/usr/bin/touch", "/tmp/must-not-run"],
                    }
                },
                "agent": {"build": {"prompt": "Must not be inherited."}},
        }
    )
    (source_config / "opencode" / "opencode.jsonc").write_text(
        "// OpenCode allows comments and trailing commas.\n" + source_jsonc[:-1] + ",\n}",
        encoding="utf-8",
    )
    (source_data / "opencode" / "auth.json").write_text(
        '{"custom":{"type":"api","key":"secret"}}',
        encoding="utf-8",
    )
    (source_state / "opencode" / "model.json").write_text(
        json.dumps(
            {
                "recent": [{"providerID": "custom", "modelID": "recent-model"}],
                "favorite": [],
                "variant": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(source_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(source_config))
    monkeypatch.setenv("XDG_DATA_HOME", str(source_data))
    monkeypatch.setenv("XDG_STATE_HOME", str(source_state))
    monkeypatch.setenv("CUSTOM_PROVIDER_KEY", "provider-secret")

    participant = OpenCodeParticipant(ParticipantConfig(), EgoConfig())
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = TurnRequest(
        run_id="run",
        phase=Phase.INDEPENDENT,
        question="Question?",
        workspace=workspace,
    )

    command = participant.command("/usr/local/bin/opencode", {}, request)

    assert command[0] == "/usr/bin/env"
    runtime_home = Path(command[1].removeprefix("HOME="))
    runtime_config_home = Path(command[2].removeprefix("XDG_CONFIG_HOME="))
    runtime_data_home = Path(command[3].removeprefix("XDG_DATA_HOME="))
    runtime_state_home = Path(command[5].removeprefix("XDG_STATE_HOME="))
    runtime_config = json.loads(
        (runtime_config_home / "opencode" / "opencode.json").read_text(encoding="utf-8")
    )
    runtime_auth = runtime_data_home / "opencode" / "auth.json"
    runtime_model_state = runtime_state_home / "opencode" / "model.json"

    assert runtime_home != source_home
    assert runtime_config["model"] == "custom/default-model"
    assert runtime_config["provider"] == {
        "custom": {
            "npm": "@ai-sdk/openai-compatible",
            "options": {
                "baseURL": "http://localhost:11434/v1",
                "apiKey": "{env:CUSTOM_PROVIDER_KEY}",
            },
            "models": {"default-model": {"name": "Default"}},
        }
    }
    assert runtime_config["plugin"] == []
    assert runtime_config["mcp"] == {}
    assert set(runtime_config["agent"]) == {"ego"}
    assert runtime_config["share"] == "disabled"
    assert runtime_config["permission"]["edit"] == "deny"
    assert runtime_config["permission"]["bash"] == "deny"
    assert runtime_config["permission"]["task"] == "deny"
    assert runtime_config["permission"]["read"][str(workspace.resolve()) + "/**"] == "allow"
    assert runtime_config["permission"]["external_directory"][
        str(workspace.resolve()) + "/**"
    ] == "allow"
    assert runtime_auth.read_text(encoding="utf-8") == (
        '{"custom":{"type":"api","key":"secret"}}'
    )
    assert stat.S_IMODE(runtime_auth.stat().st_mode) == 0o600
    assert json.loads(runtime_model_state.read_text(encoding="utf-8"))["recent"][0] == {
        "providerID": "custom",
        "modelID": "recent-model",
    }
    assert "CUSTOM_PROVIDER_KEY" in participant.environment_keys
    assert command[command.index("--dir") + 1] == str(runtime_home / "project")
    assert command[command.index("--format") + 1] == "json"
    assert "--pure" in command
    assert "--auto" not in command
    assert "--model" not in command
    assert participant.requires_external_sandbox
    assert participant.resolved_model == "custom/default-model"

    participant.cleanup_command(command)
    assert not runtime_home.exists()


def test_opencode_explicit_model_remains_an_optional_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    participant = OpenCodeParticipant(
        ParticipantConfig(model="github-copilot/gpt-5.4-mini"),
        EgoConfig(),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    command = participant.command(
        "/usr/local/bin/opencode",
        {},
        TurnRequest(
            run_id="run",
            phase=Phase.REVISION,
            question="Question?",
            workspace=workspace,
        ),
    )
    runtime_config_home = Path(command[2].removeprefix("XDG_CONFIG_HOME="))
    runtime_config = json.loads(
        (runtime_config_home / "opencode" / "opencode.json").read_text(encoding="utf-8")
    )

    assert command[command.index("--model") + 1] == "github-copilot/gpt-5.4-mini"
    assert runtime_config["model"] == "github-copilot/gpt-5.4-mini"
    assert runtime_config["permission"]["read"] == "deny"
    assert runtime_config["permission"]["glob"] == "deny"
    participant.cleanup_command(command)


def test_opencode_extracts_final_json_event_and_reported_tokens() -> None:
    participant = OpenCodeParticipant(ParticipantConfig(), EgoConfig())
    payload = Position(
        recommendation="Keep the boundary.",
        confidence=Confidence.MODERATE,
        confidence_reason="The inspected source supports it.",
    ).model_dump(mode="json")
    output = "\n".join(
        (
            json.dumps(
                {
                    "type": "step_finish",
                    "part": {
                        "tokens": {
                            "input": 120,
                            "output": 30,
                            "reasoning": 10,
                            "cache": {"read": 40, "write": 0},
                            "total": 200,
                        },
                        "cost": 0.123,
                    },
                }
            ),
            json.dumps({"type": "text", "part": {"text": json.dumps(payload)}}),
        )
    )

    assert participant.extract_json(output) == payload
    assert participant.extract_usage(output) == UsageMetrics(
        input_tokens=120,
        output_tokens=30,
        cached_input_tokens=40,
        total_tokens=200,
    )


@pytest.mark.asyncio
async def test_opencode_temporary_home_is_removed_when_turn_is_cancelled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "source-home"))
    participant = OpenCodeParticipant(ParticipantConfig(), EgoConfig())
    runtime_homes: list[Path] = []
    original_command = participant.command

    async def fake_probe() -> ParticipantAvailability:
        return ParticipantAvailability(
            participant_id="opencode",
            status=AvailabilityStatus.AVAILABLE,
            binary="/usr/local/bin/opencode",
        )

    def capture_command(
        binary: str, schema: dict[str, object], request: TurnRequest
    ) -> list[str]:
        command = original_command(binary, schema, request)
        runtime_homes.append(Path(command[1].removeprefix("HOME=")))
        return command

    async def cancelled_run(*args: object, **kwargs: object) -> ProcessResult:
        del args, kwargs
        raise asyncio.CancelledError

    monkeypatch.setattr(participant, "probe", fake_probe)
    monkeypatch.setattr(participant, "command", capture_command)
    monkeypatch.setattr("ego.participants.base.run_read_only", cancelled_run)

    with pytest.raises(asyncio.CancelledError):
        await participant.respond(
            TurnRequest(
                run_id="cancelled",
                phase=Phase.INDEPENDENT,
                question="Read only.",
                workspace=tmp_path,
            )
        )

    assert runtime_homes
    assert all(not path.exists() for path in runtime_homes)


def test_existing_config_without_opencode_still_builds_every_participant() -> None:
    config = EgoConfig(participants={"codex": ParticipantConfig(enabled=False)})

    participants = build_participants(config)

    assert set(participants) == {"codex", "claude", "gemini", "copilot", "opencode"}
    assert not participants["codex"].config.enabled  # type: ignore[attr-defined]
    assert participants["opencode"].config.enabled  # type: ignore[attr-defined]


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


def test_codex_extracts_reported_turn_usage() -> None:
    participant = CodexParticipant(ParticipantConfig(), EgoConfig())
    output = "\n".join(
        (
            json.dumps({"type": "thread.started"}),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 21_104,
                        "cached_input_tokens": 0,
                        "output_tokens": 3_575,
                    },
                }
            ),
        )
    )

    assert participant.extract_usage(output) == UsageMetrics(
        input_tokens=21_104,
        output_tokens=3_575,
        cached_input_tokens=0,
        total_tokens=24_679,
    )


def test_claude_extracts_cached_tokens_and_reported_cost() -> None:
    participant = ClaudeParticipant(ParticipantConfig(), EgoConfig())
    output = json.dumps(
        {
            "usage": {
                "input_tokens": 8,
                "cache_creation_input_tokens": 37_505,
                "cache_read_input_tokens": 97_406,
                "output_tokens": 20_396,
            },
            "total_cost_usd": 0.5717558,
        }
    )

    assert participant.extract_usage(output) == UsageMetrics(
        input_tokens=134_919,
        output_tokens=20_396,
        cached_input_tokens=97_406,
        total_tokens=155_315,
        cost_usd=0.5717558,
    )


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


def test_placeholder_synthesis_is_rejected() -> None:
    request = _synthesis_request()
    placeholder = Synthesis(
        recommendation="Test",
        supporting_argument_ids=["a"],
        alternatives=["a"],
        disagreements=["a"],
        assumptions=["a"],
        risks=["a"],
        confidence=Confidence.MODERATE,
        confidence_reason="test",
        material_conflicts=["a"],
    )

    with pytest.raises(ValueError) as error:
        validate_response(request, placeholder)
    validation_error = str(error.value)
    assert "not a placeholder" in validation_error
    assert "substantive confidence reason" in validation_error
    assert "unknown argument ids: a" in validation_error


def test_synthesis_rejects_unknown_argument_ids() -> None:
    synthesis = Synthesis(
        recommendation="Keep the external boundary and document its limits.",
        supporting_argument_ids=["invented-id"],
        confidence=Confidence.MODERATE,
        confidence_reason="The cited boundary is the strongest available argument.",
    )

    with pytest.raises(ValueError, match="unknown argument ids: invented-id"):
        validate_response(_synthesis_request(), synthesis)


def test_reconciliation_requires_explicit_equivalence_decision() -> None:
    request = TurnRequest(
        run_id="run",
        phase=Phase.RECONCILIATION,
        question="Are these positions equivalent?",
        workspace=".",
    )
    synthesis = Synthesis(
        recommendation="The recommendations remain materially different.",
        confidence=Confidence.MODERATE,
        confidence_reason="Their proposed boundaries have materially different scope.",
    )

    with pytest.raises(ValueError, match="explicit equivalence decision"):
        validate_response(request, synthesis)


def _synthesis_request() -> TurnRequest:
    return TurnRequest(
        run_id="run",
        phase=Phase.SYNTHESIS,
        question="Which boundary should we use?",
        workspace=".",
        peer_positions={
            "codex": Position(
                recommendation="Keep the external boundary.",
                arguments=[Argument(id="external-boundary", claim="It contains writes.")],
                confidence=Confidence.MODERATE,
                confidence_reason="The repository boundary supports it.",
            )
        },
    )


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
        require_external_sandbox: bool,
        environment_keys: frozenset[str],
    ) -> ProcessResult:
        del workspace, timeout_seconds, output_limit_bytes
        assert not require_external_sandbox
        assert environment_keys == ClaudeParticipant.environment_keys
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


@pytest.mark.asyncio
async def test_codex_temporary_home_is_removed_when_turn_is_cancelled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source_home = tmp_path / "source-codex-home"
    source_home.mkdir()
    (source_home / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(source_home))
    participant = CodexParticipant(ParticipantConfig(), EgoConfig())
    runtime_homes: list[Path] = []
    original_command = participant.command

    async def fake_probe() -> ParticipantAvailability:
        return ParticipantAvailability(
            participant_id="codex",
            status=AvailabilityStatus.AVAILABLE,
            binary="/usr/local/bin/codex",
        )

    def capture_command(
        binary: str, schema: dict[str, object], request: TurnRequest
    ) -> list[str]:
        command = original_command(binary, schema, request)
        runtime_homes.append(Path(command[1].removeprefix("HOME=")))
        return command

    async def cancelled_run(*args: object, **kwargs: object) -> ProcessResult:
        del args, kwargs
        raise asyncio.CancelledError

    monkeypatch.setattr(participant, "probe", fake_probe)
    monkeypatch.setattr(participant, "command", capture_command)
    monkeypatch.setattr("ego.participants.base.run_read_only", cancelled_run)

    with pytest.raises(asyncio.CancelledError):
        await participant.respond(
            TurnRequest(
                run_id="cancelled",
                phase=Phase.INDEPENDENT,
                question="Read only.",
                workspace=tmp_path,
            )
        )

    assert runtime_homes
    assert all(not path.exists() for path in runtime_homes)


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
