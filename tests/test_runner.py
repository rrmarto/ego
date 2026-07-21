import sys
from pathlib import Path

import pytest

from ego.runner import OutputLimitExceeded, ProcessTimeout, run_read_only


@pytest.mark.asyncio
async def test_runner_timeout_without_real_sandbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("ego.runner.wrap_read_only", lambda command, workspace: command)
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
    monkeypatch.setattr("ego.runner.wrap_read_only", lambda command, workspace: command)
    with pytest.raises(OutputLimitExceeded):
        await run_read_only(
            [sys.executable, "-c", "print('x' * 200)"],
            workspace=tmp_path,
            stdin="",
            timeout_seconds=2,
            output_limit_bytes=100,
        )
