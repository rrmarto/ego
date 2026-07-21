from pathlib import Path

import pytest

from ego.sandbox import probe_seatbelt, seatbelt_profile, wrap_read_only


def test_profile_denies_workspace_writes(tmp_path: Path) -> None:
    profile = seatbelt_profile(tmp_path)
    assert "(allow default)" in profile
    assert "deny file-write*" in profile
    assert str(tmp_path.resolve()) in profile
    wrapped = wrap_read_only(["/bin/echo", "ok"], tmp_path)
    assert wrapped[0] == "/usr/bin/sandbox-exec"
    assert wrapped[-2:] == ["/bin/echo", "ok"]


@pytest.mark.asyncio
async def test_real_seatbelt_read_write_probe_when_nesting_is_supported() -> None:
    result = await probe_seatbelt()
    if not result.safe and "Operation not permitted" in result.reason:
        pytest.skip("the parent Codex sandbox forbids nested Seatbelt profiles")
    assert result.safe, result.reason
