from pathlib import Path

from typer.testing import CliRunner

from ego.cli import app

runner = CliRunner()


def test_help_exposes_public_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("ask", "summon", "doctor", "participants", "runs", "inspect", "decisions"):
        assert command in result.stdout


def test_bare_ego_opens_tui(monkeypatch: object) -> None:
    launched: list[bool] = []
    monkeypatch.setattr("ego.cli.launch_tui", lambda: launched.append(True))  # type: ignore[attr-defined]
    result = runner.invoke(app)

    assert result.exit_code == 0
    assert launched == [True]


def test_empty_history_commands_are_readable(monkeypatch: object, tmp_path: Path) -> None:
    import os

    os.environ["EGO_DATA_DIR"] = str(tmp_path / "ego-data")
    try:
        assert runner.invoke(app, ["runs"]).exit_code == 0
        assert runner.invoke(app, ["decisions"]).exit_code == 0
    finally:
        os.environ.pop("EGO_DATA_DIR", None)
