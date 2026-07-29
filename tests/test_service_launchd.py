from __future__ import annotations

import inspect
import io
import os
import plistlib
import stat
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from ego.config import AppPaths, EgoConfig
from ego.service_auth import ServiceCredentialStore
from ego.service_contract import AuthenticationChallengeFrame
from ego.service_launchd import (
    GUI_PATH_SUFFIXES,
    LAUNCH_AGENT_LABEL,
    LAUNCHCTL,
    SYSTEM_PATH_ENTRIES,
    HealthResult,
    HealthState,
    LaunchAgentState,
    ServiceLaunchAgent,
    ServiceLaunchdError,
    check_service_health,
    gui_path,
    launch_agent_payload,
    resolve_install_executable,
    run_launchctl,
)


class FakeLaunchctl:
    def __init__(
        self,
        *,
        loaded: bool = False,
        failures: dict[str, int] | None = None,
    ) -> None:
        self.loaded = loaded
        self.failures = failures or {}
        self.calls: list[list[str]] = []

    def __call__(self, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        call = list(arguments)
        self.calls.append(call)
        action = call[0]
        if action in self.failures:
            return subprocess.CompletedProcess(
                [LAUNCHCTL, *call],
                self.failures[action],
                stdout="",
                stderr=f"synthetic {action} failure",
            )
        if action == "print":
            code = 0 if self.loaded else 113
        elif action == "bootout":
            code = 0 if self.loaded else 3
            if code == 0:
                self.loaded = False
        elif action == "bootstrap":
            code = 0
            self.loaded = True
        else:
            code = 0
        return subprocess.CompletedProcess([LAUNCHCTL, *call], code, stdout="", stderr="")


def authenticated_health(
    host: str,
    port: int,
    credentials: ServiceCredentialStore,
    timeout_seconds: float,
    max_message_bytes: int,
) -> HealthResult:
    del host, port, credentials, timeout_seconds, max_message_bytes
    return HealthResult(HealthState.AUTHENTICATED, "synthetic Ego proof accepted")


def health(state: HealthState) -> Any:
    def checker(
        host: str,
        port: int,
        credentials: ServiceCredentialStore,
        timeout_seconds: float,
        max_message_bytes: int,
    ) -> HealthResult:
        del host, port, credentials, timeout_seconds, max_message_bytes
        return HealthResult(state, f"synthetic {state.value}")

    return checker


@pytest.fixture
def executable(tmp_path: Path) -> Path:
    path = tmp_path / "bin" / "ego"
    path.parent.mkdir()
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o700)
    return path


@pytest.fixture
def launch_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    return home


def make_paths(tmp_path: Path) -> AppPaths:
    data = tmp_path / "data"
    return AppPaths(
        data_dir=data,
        database=data / "ego.sqlite3",
        raw_dir=data / "raw",
        config_file=data / "config.toml",
    )


def make_agent(
    tmp_path: Path,
    executable: Path,
    launch_home: Path,
    runner: FakeLaunchctl,
    *,
    health_checker: Any = authenticated_health,
) -> ServiceLaunchAgent:
    return ServiceLaunchAgent(
        make_paths(tmp_path),
        EgoConfig(),
        executable=executable,
        home=launch_home,
        uid=os.getuid(),
        command_runner=runner,
        health_checker=health_checker,
        platform="darwin",
        health_timeout_seconds=0,
        health_poll_seconds=0,
    )


def read_plist(agent: ServiceLaunchAgent) -> dict[str, object]:
    with agent.plist_path.open("rb") as handle:
        return plistlib.load(handle)


def test_plist_generation_is_exact_and_has_stable_label(
    executable: Path, launch_home: Path, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    payload = launch_agent_payload(
        executable=executable,
        home=launch_home,
        data_dir=data_dir,
        stdout_path=data_dir / "service.stdout.log",
        stderr_path=data_dir / "service.stderr.log",
    )

    assert payload == {
        "Label": "com.rrmarto.ego.service",
        "ProgramArguments": [str(executable), "service", "run"],
        "KeepAlive": True,
        "ProcessType": "Background",
        "ThrottleInterval": 10,
        "Umask": "077",
        "EnvironmentVariables": {
            "HOME": str(launch_home),
            "EGO_DATA_DIR": str(data_dir),
            "PYTHONUNBUFFERED": "1",
            "PATH": gui_path(launch_home),
        },
        "StandardOutPath": str(data_dir / "service.stdout.log"),
        "StandardErrorPath": str(data_dir / "service.stderr.log"),
    }
    assert payload["Label"] == LAUNCH_AGENT_LABEL
    assert "RunAtLoad" not in payload


def test_program_arguments_are_closed_without_shell_or_configurable_argv(
    executable: Path, launch_home: Path, tmp_path: Path
) -> None:
    payload = launch_agent_payload(
        executable=executable,
        home=launch_home,
        data_dir=tmp_path / "data",
        stdout_path=tmp_path / "stdout",
        stderr_path=tmp_path / "stderr",
    )

    assert payload["ProgramArguments"] == [str(executable), "service", "run"]
    encoded = plistlib.dumps(payload).decode()
    assert "/bin/sh" not in encoded
    assert "bash" not in encoded
    assert "argv" not in encoded
    assert "command" not in encoded


def test_launchctl_uses_direct_argument_array(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(
        arguments: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append((arguments, kwargs))
        return subprocess.CompletedProcess(arguments, 0, stdout="", stderr="")

    monkeypatch.setattr("ego.service_launchd.subprocess.run", fake_run)

    run_launchctl(["print", f"gui/501/{LAUNCH_AGENT_LABEL}"])

    assert calls == [
        (
            [LAUNCHCTL, "print", f"gui/501/{LAUNCH_AGENT_LABEL}"],
            {"check": False, "capture_output": True, "text": True},
        )
    ]


def test_gui_path_is_explicit_and_bounded(launch_home: Path) -> None:
    assert gui_path(launch_home).split(":") == [
        *(str(launch_home / suffix) for suffix in GUI_PATH_SUFFIXES),
        *SYSTEM_PATH_ENTRIES,
    ]


def test_global_symlink_path_is_not_canonicalized(tmp_path: Path) -> None:
    target = tmp_path / "tools" / "ego"
    target.parent.mkdir()
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    target.chmod(0o700)
    stable = tmp_path / ".local" / "bin" / "ego"
    stable.parent.mkdir(parents=True)
    stable.symlink_to(target)

    assert resolve_install_executable(str(stable)) == stable
    assert resolve_install_executable("ego", search_path=str(stable.parent)) == stable


def test_editable_venv_executable_is_supported(tmp_path: Path, launch_home: Path) -> None:
    editable = tmp_path / ".venv" / "bin" / "ego"
    editable.parent.mkdir(parents=True)
    editable.write_text("#!/bin/sh\n", encoding="utf-8")
    editable.chmod(0o700)
    agent = make_agent(tmp_path, editable, launch_home, FakeLaunchctl())

    assert agent.install().editable_install
    assert read_plist(agent)["ProgramArguments"] == [str(editable), "service", "run"]


def test_missing_executable_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ServiceLaunchdError, match="does not exist") as captured:
        resolve_install_executable(str(tmp_path / "missing-ego"))
    assert captured.value.code == "executable_not_found"


def test_non_executable_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "ego"
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(ServiceLaunchdError, match="execute permission") as captured:
        resolve_install_executable(str(path))
    assert captured.value.code == "executable_not_executable"


def test_launchagents_directory_symlink_is_rejected(
    tmp_path: Path, executable: Path, launch_home: Path
) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir()
    (launch_home / "Library").mkdir()
    (launch_home / "Library" / "LaunchAgents").symlink_to(target)
    agent = make_agent(tmp_path, executable, launch_home, FakeLaunchctl())

    with pytest.raises(ServiceLaunchdError) as captured:
        agent.install()
    assert captured.value.code == "unsafe_launchagent_location"


def test_launchagent_plist_symlink_is_rejected(
    tmp_path: Path, executable: Path, launch_home: Path
) -> None:
    launch_agents = launch_home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, mode=0o700)
    target = tmp_path / "foreign.plist"
    target.write_text("foreign", encoding="utf-8")
    (launch_agents / f"{LAUNCH_AGENT_LABEL}.plist").symlink_to(target)
    agent = make_agent(tmp_path, executable, launch_home, FakeLaunchctl())

    with pytest.raises(ServiceLaunchdError) as captured:
        agent.install()
    assert captured.value.code == "unsafe_launchagent_location"
    assert target.read_text(encoding="utf-8") == "foreign"


def test_group_writable_plist_is_rejected(
    tmp_path: Path, executable: Path, launch_home: Path
) -> None:
    runner = FakeLaunchctl()
    agent = make_agent(tmp_path, executable, launch_home, runner)
    agent.install()
    agent.plist_path.chmod(0o620)

    with pytest.raises(ServiceLaunchdError) as captured:
        agent.status()
    assert captured.value.code == "unsafe_launchagent_location"


def test_plist_write_uses_atomic_replace_and_private_permissions(
    tmp_path: Path,
    executable: Path,
    launch_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = FakeLaunchctl()
    agent = make_agent(tmp_path, executable, launch_home, runner)
    replacements: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def recording_replace(source: str | Path, target: str | Path) -> None:
        replacements.append((Path(source), Path(target)))
        real_replace(source, target)

    monkeypatch.setattr("ego.service_launchd.os.replace", recording_replace)
    agent.install()

    assert replacements == [(replacements[0][0], agent.plist_path)]
    assert replacements[0][0].parent == agent.plist_path.parent
    assert stat.S_IMODE(agent.plist_path.stat().st_mode) == 0o600
    assert not list(agent.plist_path.parent.glob("*.tmp"))


def test_new_install_enables_then_bootstraps(
    tmp_path: Path, executable: Path, launch_home: Path
) -> None:
    runner = FakeLaunchctl()
    agent = make_agent(tmp_path, executable, launch_home, runner)

    info = agent.install()

    assert info.executable == executable
    assert runner.calls == [
        ["print", agent.service_target],
        ["enable", agent.service_target],
        ["bootstrap", agent.gui_domain, str(agent.plist_path)],
    ]


def test_idempotent_install_boots_out_before_replacing_and_bootstrapping(
    tmp_path: Path, executable: Path, launch_home: Path
) -> None:
    runner = FakeLaunchctl()
    agent = make_agent(tmp_path, executable, launch_home, runner)
    agent.install()
    runner.calls.clear()

    agent.install()

    assert [call[0] for call in runner.calls] == ["print", "bootout", "enable", "bootstrap"]


def test_install_updates_registered_executable(
    tmp_path: Path, executable: Path, launch_home: Path
) -> None:
    runner = FakeLaunchctl()
    first = make_agent(tmp_path, executable, launch_home, runner)
    first.install()
    replacement = tmp_path / "replacement" / "ego"
    replacement.parent.mkdir()
    replacement.write_text("#!/bin/sh\n", encoding="utf-8")
    replacement.chmod(0o700)
    second = make_agent(tmp_path, replacement, launch_home, runner)
    runner.calls.clear()

    second.install()

    assert [call[0] for call in runner.calls] == ["print", "bootout", "enable", "bootstrap"]
    assert read_plist(second)["ProgramArguments"] == [str(replacement), "service", "run"]


def test_install_always_recovers_disabled_service_with_enable(
    tmp_path: Path, executable: Path, launch_home: Path
) -> None:
    runner = FakeLaunchctl()
    agent = make_agent(tmp_path, executable, launch_home, runner)

    agent.install()

    assert ["enable", agent.service_target] in runner.calls


@pytest.mark.parametrize(
    ("action", "code"),
    [
        ("bootstrap", "launchctl_bootstrap_failed"),
        ("enable", "launchctl_enable_failed"),
    ],
)
def test_install_reports_actionable_launchctl_failure(
    action: str,
    code: str,
    tmp_path: Path,
    executable: Path,
    launch_home: Path,
) -> None:
    agent = make_agent(
        tmp_path,
        executable,
        launch_home,
        FakeLaunchctl(failures={action: 5}),
    )

    with pytest.raises(ServiceLaunchdError, match=f"synthetic {action} failure") as captured:
        agent.install()
    assert captured.value.code == code


def test_loaded_update_reports_bootout_failure(
    tmp_path: Path, executable: Path, launch_home: Path
) -> None:
    agent = make_agent(
        tmp_path,
        executable,
        launch_home,
        FakeLaunchctl(loaded=True, failures={"bootout": 5}),
    )

    with pytest.raises(ServiceLaunchdError) as captured:
        agent.install()
    assert captured.value.code == "launchctl_bootout_failed"


def test_status_not_installed(
    tmp_path: Path, executable: Path, launch_home: Path
) -> None:
    agent = make_agent(tmp_path, executable, launch_home, FakeLaunchctl())

    assert agent.status().state is LaunchAgentState.NOT_INSTALLED


def test_status_installed_but_not_loaded(
    tmp_path: Path, executable: Path, launch_home: Path
) -> None:
    runner = FakeLaunchctl()
    agent = make_agent(tmp_path, executable, launch_home, runner)
    agent.install()
    runner.loaded = False

    assert agent.status().state is LaunchAgentState.INSTALLED_NOT_LOADED


@pytest.mark.parametrize(
    ("health_state", "launch_state"),
    [
        (HealthState.UNAVAILABLE, LaunchAgentState.LOADED_UNAVAILABLE),
        (HealthState.TIMEOUT, LaunchAgentState.LOADED_UNAVAILABLE),
        (HealthState.AUTHENTICATED, LaunchAgentState.AUTHENTICATED),
        (HealthState.INVALID_PROOF, LaunchAgentState.INVALID_PROOF),
        (HealthState.INCOMPATIBLE, LaunchAgentState.INCOMPATIBLE),
    ],
)
def test_loaded_status_distinguishes_health_outcomes(
    health_state: HealthState,
    launch_state: LaunchAgentState,
    tmp_path: Path,
    executable: Path,
    launch_home: Path,
) -> None:
    runner = FakeLaunchctl()
    agent = make_agent(tmp_path, executable, launch_home, runner)
    agent.install()
    checked = make_agent(
        tmp_path,
        executable,
        launch_home,
        runner,
        health_checker=health(health_state),
    )

    assert checked.status().state is launch_state


def test_status_detects_stale_editable_path(
    tmp_path: Path, executable: Path, launch_home: Path
) -> None:
    runner = FakeLaunchctl()
    agent = make_agent(tmp_path, executable, launch_home, runner)
    agent.install()
    executable.unlink()

    status = agent.status()

    assert status.executable_stale
    assert "missing or non-executable" in status.detail


def test_install_health_failure_points_to_logs(
    tmp_path: Path, executable: Path, launch_home: Path
) -> None:
    agent = make_agent(
        tmp_path,
        executable,
        launch_home,
        FakeLaunchctl(),
        health_checker=health(HealthState.INVALID_PROOF),
    )

    with pytest.raises(ServiceLaunchdError) as captured:
        agent.install()
    assert captured.value.code == "invalid_server_proof"
    assert str(agent.stdout_path) in str(captured.value)
    assert str(agent.stderr_path) in str(captured.value)


def test_install_health_timeout_is_actionable(
    tmp_path: Path, executable: Path, launch_home: Path
) -> None:
    agent = make_agent(
        tmp_path,
        executable,
        launch_home,
        FakeLaunchctl(),
        health_checker=health(HealthState.TIMEOUT),
    )

    with pytest.raises(ServiceLaunchdError) as captured:
        agent.install()
    assert captured.value.code == "service_health_timeout"


def test_plist_write_failure_is_actionable(
    tmp_path: Path,
    executable: Path,
    launch_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = make_agent(tmp_path, executable, launch_home, FakeLaunchctl())
    real_open = os.open

    def failing_open(path: str | Path, flags: int, mode: int = 0o777) -> int:
        if str(path).endswith(".tmp"):
            raise PermissionError("synthetic plist denial")
        return real_open(path, flags, mode)

    monkeypatch.setattr("ego.service_launchd.os.open", failing_open)

    with pytest.raises(ServiceLaunchdError, match="synthetic plist denial") as captured:
        agent.install()
    assert captured.value.code == "plist_write_failed"


def test_health_checker_accepts_existing_server_proof(
    app_paths: AppPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials = ServiceCredentialStore(app_paths)
    token = credentials.get_or_create()
    nonce = credentials.new_nonce()
    challenge = AuthenticationChallengeFrame(
        nonce=nonce,
        proof=credentials.server_proof(token, nonce),
    )

    class Connection:
        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def settimeout(self, timeout: float) -> None:
            del timeout

        def makefile(self, mode: str) -> io.BytesIO:
            assert mode == "rb"
            return io.BytesIO(challenge.model_dump_json().encode() + b"\n")

    monkeypatch.setattr(
        "ego.service_launchd.socket.create_connection",
        lambda *args, **kw: Connection(),
    )

    result = check_service_health("127.0.0.1", 37645, credentials, 1, 4096)

    assert result.state is HealthState.AUTHENTICATED


def test_health_checker_rejects_incorrect_server_proof(
    app_paths: AppPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials = ServiceCredentialStore(app_paths)
    credentials.get_or_create()
    challenge = AuthenticationChallengeFrame(nonce="n" * 43, proof="0" * 64)

    class Connection:
        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def settimeout(self, timeout: float) -> None:
            del timeout

        def makefile(self, mode: str) -> io.BytesIO:
            assert mode == "rb"
            return io.BytesIO(challenge.model_dump_json().encode() + b"\n")

    monkeypatch.setattr(
        "ego.service_launchd.socket.create_connection",
        lambda *args, **kw: Connection(),
    )

    result = check_service_health("127.0.0.1", 37645, credentials, 1, 4096)

    assert result.state is HealthState.INVALID_PROOF


def test_health_checker_rejects_incompatible_protocol(
    app_paths: AppPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Connection:
        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def settimeout(self, timeout: float) -> None:
            del timeout

        def makefile(self, mode: str) -> io.BytesIO:
            assert mode == "rb"
            return io.BytesIO(b'{"kind":"not-ego","token":"must-not-leak"}\n')

    monkeypatch.setattr(
        "ego.service_launchd.socket.create_connection",
        lambda *args, **kw: Connection(),
    )

    result = check_service_health(
        "127.0.0.1",
        37645,
        ServiceCredentialStore(app_paths),
        1,
        4096,
    )

    assert result.state is HealthState.INCOMPATIBLE
    assert "token" not in result.detail


def test_health_checker_reports_timeout(
    app_paths: AppPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    def timed_out(*args: object, **kwargs: object) -> None:
        raise TimeoutError

    monkeypatch.setattr("ego.service_launchd.socket.create_connection", timed_out)

    result = check_service_health(
        "127.0.0.1",
        37645,
        ServiceCredentialStore(app_paths),
        0.01,
        4096,
    )

    assert result.state is HealthState.TIMEOUT


def test_uninstall_is_idempotent_and_preserves_credential(
    tmp_path: Path, executable: Path, launch_home: Path
) -> None:
    runner = FakeLaunchctl()
    agent = make_agent(tmp_path, executable, launch_home, runner)
    credential = ServiceCredentialStore(agent.paths).get_or_create()
    agent.install()
    runner.calls.clear()

    assert agent.uninstall()
    assert not agent.uninstall()
    assert [call[0] for call in runner.calls] == ["print", "bootout", "print"]
    assert ServiceCredentialStore(agent.paths).get_or_create() == credential
    assert not agent.plist_path.exists()


def test_plist_and_log_configuration_never_contain_token(
    tmp_path: Path, executable: Path, launch_home: Path
) -> None:
    agent = make_agent(tmp_path, executable, launch_home, FakeLaunchctl())
    token = ServiceCredentialStore(agent.paths).get_or_create()
    agent.install()

    assert token not in agent.plist_path.read_text(encoding="utf-8")
    assert token not in str(read_plist(agent))
    assert read_plist(agent)["StandardOutPath"] != read_plist(agent)["StandardErrorPath"]


def test_launchagent_operations_do_not_build_participants(
    tmp_path: Path,
    executable: Path,
    launch_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ego.participants.build_participants",
        lambda *args, **kwargs: pytest.fail("LaunchAgent must not build participants"),
    )
    agent = make_agent(tmp_path, executable, launch_home, FakeLaunchctl())

    agent.install()
    assert agent.status().state is LaunchAgentState.AUTHENTICATED
    assert agent.uninstall()


def test_launchagent_api_has_no_command_or_argv_parameters() -> None:
    assert list(inspect.signature(ServiceLaunchAgent.install).parameters) == ["self"]
    assert list(inspect.signature(ServiceLaunchAgent.status).parameters) == ["self"]
    assert list(inspect.signature(ServiceLaunchAgent.uninstall).parameters) == ["self"]


def test_unsupported_platform_is_actionable(
    tmp_path: Path, executable: Path, launch_home: Path
) -> None:
    agent = ServiceLaunchAgent(
        make_paths(tmp_path),
        EgoConfig(),
        executable=executable,
        home=launch_home,
        command_runner=FakeLaunchctl(),
        platform="linux",
    )

    with pytest.raises(ServiceLaunchdError) as captured:
        agent.install()
    assert captured.value.code == "unsupported_platform"
