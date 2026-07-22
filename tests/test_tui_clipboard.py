from __future__ import annotations

import subprocess

import pytest

from ego.tui.clipboard import copy_to_macos_clipboard


class ExistingPbcopy:
    def is_file(self) -> bool:
        return True

    def __str__(self) -> str:
        return "/usr/bin/pbcopy"


def test_macos_clipboard_uses_pbcopy_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], str]] = []

    def fake_run(
        command: list[str], *, input: str, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append((command, input))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("ego.tui.clipboard.sys.platform", "darwin")
    monkeypatch.setattr("ego.tui.clipboard.PBCOPY", ExistingPbcopy())
    monkeypatch.setattr("ego.tui.clipboard.subprocess.run", fake_run)

    assert copy_to_macos_clipboard("selected text") is True
    assert calls == [(["/usr/bin/pbcopy"], "selected text")]


def test_macos_clipboard_reports_pbcopy_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ego.tui.clipboard.sys.platform", "darwin")
    monkeypatch.setattr("ego.tui.clipboard.PBCOPY", ExistingPbcopy())
    monkeypatch.setattr(
        "ego.tui.clipboard.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(["/usr/bin/pbcopy"], 1),
    )

    assert copy_to_macos_clipboard("selected text") is False
