import sys
from pathlib import Path

import pytest

from ego.runner import (
    OutputLimitExceeded,
    ProcessFailure,
    ProcessTimeout,
    reduced_environment,
    run_read_only,
)


def test_reduced_environment_only_includes_selected_provider_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-secret")
    monkeypatch.setenv("GH_TOKEN", "github-secret")

    environment = reduced_environment(frozenset({"ANTHROPIC_API_KEY"}))

    assert environment["ANTHROPIC_API_KEY"] == "anthropic-secret"
    assert "GEMINI_API_KEY" not in environment
    assert "GH_TOKEN" not in environment


@pytest.mark.asyncio
async def test_runner_timeout_without_real_sandbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "ego.runner.wrap_read_only",
        lambda command, workspace, *, protect_user_data: command,
    )
    with pytest.raises(ProcessTimeout):
        await run_read_only(
            [sys.executable, "-c", "import time; time.sleep(2)"],
            workspace=tmp_path,
            stdin="",
            timeout_seconds=0.05,
            output_limit_bytes=100,
        )


@pytest.mark.asyncio
async def test_runner_output_limit_without_real_sandbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "ego.runner.wrap_read_only",
        lambda command, workspace, *, protect_user_data: command,
    )
    with pytest.raises(OutputLimitExceeded):
        await run_read_only(
            [sys.executable, "-c", "print('x' * 200)"],
            workspace=tmp_path,
            stdin="",
            timeout_seconds=2,
            output_limit_bytes=100,
        )


@pytest.mark.asyncio
async def test_external_sandbox_participant_cannot_run_without_seatbelt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "ego.runner.wrap_read_only",
        lambda command, workspace, *, protect_user_data: command,
    )

    with pytest.raises(ProcessFailure, match="requires Ego's external Seatbelt"):
        await run_read_only(
            [sys.executable, "-c", "print('must not run')"],
            workspace=tmp_path,
            stdin="",
            timeout_seconds=2,
            output_limit_bytes=100,
            require_external_sandbox=True,
        )
