from pathlib import Path

from typer.testing import CliRunner

from ego.cli import app

runner = CliRunner()


def test_help_exposes_public_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("ask", "summon", "doctor", "participants", "runs", "inspect", "decisions"):
        assert command in result.stdout


def test_bare_ego_opens_interactive_environment() -> None:
    result = runner.invoke(app, input="/exit\n")

    assert result.exit_code == 0
    assert "interactive decision environment" in result.stdout
    assert "Write a decision question" in result.stdout


def test_empty_history_commands_are_readable(monkeypatch: object, tmp_path: Path) -> None:
    import os

    os.environ["EGO_DATA_DIR"] = str(tmp_path / "ego-data")
    try:
        assert runner.invoke(app, ["runs"]).exit_code == 0
        assert runner.invoke(app, ["decisions"]).exit_code == 0
    finally:
        os.environ.pop("EGO_DATA_DIR", None)
