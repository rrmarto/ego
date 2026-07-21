from pathlib import Path

from ego.shell import InteractiveShell, ShellActions


class ActionLog:
    def __init__(self) -> None:
        self.deliberations: list[tuple[str, Path, list[str] | None, str]] = []
        self.calls: list[tuple[str, str | None]] = []

    def actions(self) -> ShellActions:
        return ShellActions(
            deliberate=lambda question, workspace, participants, mode: self.deliberations.append(
                (question, workspace, participants, mode)
            ),
            doctor=lambda: self.calls.append(("doctor", None)),
            runs=lambda: self.calls.append(("runs", None)),
            decisions=lambda: self.calls.append(("decisions", None)),
            inspect=lambda value: self.calls.append(("inspect", value)),
            show=lambda value: self.calls.append(("show", value)),
        )


def run_shell(tmp_path: Path, commands: list[str]) -> tuple[ActionLog, list[str]]:
    command_iterator = iter(commands)
    output: list[str] = []
    log = ActionLog()
    shell = InteractiveShell(
        version="test",
        workspace=tmp_path,
        actions=log.actions(),
        read=lambda _prompt: next(command_iterator),
        write=output.append,
    )
    shell.run()
    return log, output


def test_plain_question_uses_all_participants(tmp_path: Path) -> None:
    log, _ = run_shell(tmp_path, ["Should we keep this boundary?", "/exit"])

    assert log.deliberations == [
        ("Should we keep this boundary?", tmp_path.resolve(), None, "standard")
    ]


def test_session_keeps_workspace_mode_and_selected_participants(tmp_path: Path) -> None:
    target = tmp_path / "project"
    target.mkdir()
    log, _ = run_shell(
        tmp_path,
        [
            "/cd project",
            "/mode discussion",
            "/summon codex claude -- Choose the module boundary",
            "/exit",
        ],
    )

    assert log.deliberations == [
        (
            "Choose the module boundary",
            target.resolve(),
            ["codex", "claude"],
            "discussion",
        )
    ]


def test_internal_history_commands_are_dispatched(tmp_path: Path) -> None:
    log, _ = run_shell(
        tmp_path,
        ["/doctor", "/runs", "/decisions", "/inspect run-1", "/show decision-1", "/exit"],
    )

    assert log.calls == [
        ("doctor", None),
        ("runs", None),
        ("decisions", None),
        ("inspect", "run-1"),
        ("show", "decision-1"),
    ]

