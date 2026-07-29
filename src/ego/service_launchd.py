from __future__ import annotations

import os
import plistlib
import shutil
import socket
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import ValidationError

from ego.config import AppPaths, EgoConfig
from ego.service import LOOPBACK_HOST
from ego.service_auth import ServiceCredentialStore
from ego.service_contract import AuthenticationChallengeFrame

LAUNCH_AGENT_LABEL = "com.rrmarto.ego.service"
LAUNCH_AGENT_FILENAME = f"{LAUNCH_AGENT_LABEL}.plist"
LAUNCHCTL = "/bin/launchctl"
GUI_PATH_SUFFIXES = (
    ".local/bin",
    ".codex/packages/standalone/current/bin",
)
SYSTEM_PATH_ENTRIES = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)


class ServiceLaunchdError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class HealthState(StrEnum):
    AUTHENTICATED = "authenticated"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    INVALID_PROOF = "invalid_proof"
    INCOMPATIBLE = "incompatible"


@dataclass(frozen=True)
class HealthResult:
    state: HealthState
    detail: str


class LaunchAgentState(StrEnum):
    NOT_INSTALLED = "not_installed"
    INSTALLED_NOT_LOADED = "installed_not_loaded"
    LOADED_UNAVAILABLE = "loaded_unavailable"
    AUTHENTICATED = "authenticated"
    INVALID_PROOF = "invalid_proof"
    INCOMPATIBLE = "incompatible"


@dataclass(frozen=True)
class LaunchAgentInfo:
    plist_path: Path
    executable: Path
    endpoint: str
    stdout_path: Path
    stderr_path: Path
    editable_install: bool


@dataclass(frozen=True)
class LaunchAgentStatus:
    state: LaunchAgentState
    info: LaunchAgentInfo
    detail: str
    executable_stale: bool = False


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
HealthChecker = Callable[
    [str, int, ServiceCredentialStore, float, int],
    HealthResult,
]


def _absolute_without_resolving(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def resolve_install_executable(
    invoked: str | None = None,
    *,
    search_path: str | None = None,
) -> Path:
    raw = invoked or sys.argv[0]
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        if candidate.parent == Path("."):
            discovered = shutil.which(raw, path=search_path)
            if discovered is None:
                raise ServiceLaunchdError(
                    "executable_not_found",
                    f"Ego executable was not found from the current invocation: {raw}",
                )
            candidate = Path(discovered)
        else:
            candidate = Path.cwd() / candidate
    candidate = _absolute_without_resolving(candidate)
    if not candidate.exists() or not candidate.is_file():
        raise ServiceLaunchdError(
            "executable_not_found",
            f"Ego executable does not exist: {candidate}",
        )
    if not os.access(candidate, os.X_OK):
        raise ServiceLaunchdError(
            "executable_not_executable",
            f"Ego executable does not have execute permission: {candidate}",
        )
    return candidate


def gui_path(home: Path) -> str:
    entries = [str(home / suffix) for suffix in GUI_PATH_SUFFIXES]
    entries.extend(SYSTEM_PATH_ENTRIES)
    return ":".join(entries)


def launch_agent_payload(
    *,
    executable: Path,
    home: Path,
    data_dir: Path,
    stdout_path: Path,
    stderr_path: Path,
) -> dict[str, object]:
    return {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [str(executable), "service", "run"],
        "KeepAlive": True,
        "ProcessType": "Background",
        "ThrottleInterval": 10,
        "Umask": "077",
        "EnvironmentVariables": {
            "HOME": str(home),
            "EGO_DATA_DIR": str(data_dir),
            "PYTHONUNBUFFERED": "1",
            "PATH": gui_path(home),
        },
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stderr_path),
    }


def check_service_health(
    host: str,
    port: int,
    credentials: ServiceCredentialStore,
    timeout_seconds: float,
    max_message_bytes: int,
) -> HealthResult:
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds) as connection:
            connection.settimeout(timeout_seconds)
            with connection.makefile("rb") as reader:
                message = reader.readline(max_message_bytes + 1)
    except TimeoutError:
        return HealthResult(HealthState.TIMEOUT, "Timed out waiting for Ego Service.")
    except OSError as error:
        return HealthResult(
            HealthState.UNAVAILABLE,
            f"Ego Service is not accepting connections on {host}:{port}: {error}",
        )
    if not message:
        return HealthResult(
            HealthState.INCOMPATIBLE,
            "The process listening on the service port closed without a server proof.",
        )
    if len(message) > max_message_bytes or not message.endswith(b"\n"):
        return HealthResult(
            HealthState.INCOMPATIBLE,
            "The process listening on the service port returned an invalid challenge.",
        )
    try:
        challenge = AuthenticationChallengeFrame.model_validate_json(message)
    except (ValidationError, ValueError):
        return HealthResult(
            HealthState.INCOMPATIBLE,
            "The process listening on the service port uses an incompatible protocol.",
        )
    token = credentials.get_or_create()
    expected = credentials.server_proof(token, challenge.nonce)
    if not credentials.matches(expected, challenge.proof):
        return HealthResult(
            HealthState.INVALID_PROOF,
            "The process listening on the service port failed Ego's server proof.",
        )
    return HealthResult(
        HealthState.AUTHENTICATED,
        "Ego Service is available and authenticated.",
    )


def run_launchctl(arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [LAUNCHCTL, *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


class ServiceLaunchAgent:
    def __init__(
        self,
        paths: AppPaths,
        config: EgoConfig,
        *,
        executable: Path,
        home: Path | None = None,
        uid: int | None = None,
        command_runner: CommandRunner = run_launchctl,
        health_checker: HealthChecker = check_service_health,
        platform: str | None = None,
        health_timeout_seconds: float = 10,
        health_poll_seconds: float = 0.2,
    ) -> None:
        self.paths = paths
        self.config = config
        self.home = _absolute_without_resolving(home or Path.home())
        self.uid = os.getuid() if uid is None else uid
        self.executable = _absolute_without_resolving(executable)
        self.command_runner = command_runner
        self.health_checker = health_checker
        self.platform = sys.platform if platform is None else platform
        self.health_timeout_seconds = health_timeout_seconds
        self.health_poll_seconds = health_poll_seconds

    @property
    def launch_agents_dir(self) -> Path:
        return self.home / "Library" / "LaunchAgents"

    @property
    def plist_path(self) -> Path:
        return self.launch_agents_dir / LAUNCH_AGENT_FILENAME

    @property
    def data_dir(self) -> Path:
        return _absolute_without_resolving(self.paths.data_dir)

    @property
    def stdout_path(self) -> Path:
        return self.data_dir / "service.stdout.log"

    @property
    def stderr_path(self) -> Path:
        return self.data_dir / "service.stderr.log"

    @property
    def service_target(self) -> str:
        return f"gui/{self.uid}/{LAUNCH_AGENT_LABEL}"

    @property
    def gui_domain(self) -> str:
        return f"gui/{self.uid}"

    def install(self) -> LaunchAgentInfo:
        self._require_supported_platform()
        self._validate_executable(self.executable)
        self._prepare_private_data_dir()
        self._prepare_launch_agents_dir()
        if self.is_loaded():
            self._run_required(
                ["bootout", self.service_target],
                code="launchctl_bootout_failed",
                action="bootout",
            )
        self._write_plist_atomically()
        self._run_required(
            ["enable", self.service_target],
            code="launchctl_enable_failed",
            action="enable",
        )
        self._run_required(
            ["bootstrap", self.gui_domain, str(self.plist_path)],
            code="launchctl_bootstrap_failed",
            action="bootstrap",
        )
        health = self._wait_for_health()
        if health.state is not HealthState.AUTHENTICATED:
            raise ServiceLaunchdError(
                self._health_error_code(health.state),
                f"{health.detail} Inspect {self.stderr_path} and {self.stdout_path}.",
            )
        return self.info(self.executable)

    def status(self) -> LaunchAgentStatus:
        self._require_supported_platform()
        if self.plist_path.is_symlink():
            raise ServiceLaunchdError(
                "unsafe_launchagent_location",
                f"LaunchAgent plist must not be a symlink: {self.plist_path}",
            )
        if not self.plist_path.exists():
            return LaunchAgentStatus(
                LaunchAgentState.NOT_INSTALLED,
                self.info(self.executable),
                "Ego Service LaunchAgent is not installed.",
            )
        self._validate_plist_file()
        recorded_executable = self._read_recorded_executable()
        stale = not recorded_executable.exists() or not os.access(recorded_executable, os.X_OK)
        info = self.info(recorded_executable)
        if not self.is_loaded():
            detail = "LaunchAgent plist is installed, but the job is not loaded."
            if stale:
                detail += " Its recorded Ego executable is missing or not executable."
            return LaunchAgentStatus(
                LaunchAgentState.INSTALLED_NOT_LOADED,
                info,
                detail,
                executable_stale=stale,
            )
        health = self.health_checker(
            LOOPBACK_HOST,
            self.config.service.port,
            ServiceCredentialStore(self.paths),
            self.config.service.request_timeout_seconds,
            self.config.service.max_message_bytes,
        )
        state = {
            HealthState.AUTHENTICATED: LaunchAgentState.AUTHENTICATED,
            HealthState.UNAVAILABLE: LaunchAgentState.LOADED_UNAVAILABLE,
            HealthState.TIMEOUT: LaunchAgentState.LOADED_UNAVAILABLE,
            HealthState.INVALID_PROOF: LaunchAgentState.INVALID_PROOF,
            HealthState.INCOMPATIBLE: LaunchAgentState.INCOMPATIBLE,
        }[health.state]
        detail = health.detail
        if stale:
            detail += " The plist records a missing or non-executable Ego path."
        return LaunchAgentStatus(state, info, detail, executable_stale=stale)

    def uninstall(self) -> bool:
        self._require_supported_platform()
        self._validate_known_location()
        installed = self.plist_path.exists()
        if installed:
            self._validate_plist_file()
        if self.is_loaded():
            self._run_required(
                ["bootout", self.service_target],
                code="launchctl_bootout_failed",
                action="bootout",
            )
        if not installed:
            return False
        self.plist_path.unlink()
        return True

    def is_loaded(self) -> bool:
        result = self.command_runner(["print", self.service_target])
        return result.returncode == 0

    def info(self, executable: Path) -> LaunchAgentInfo:
        return LaunchAgentInfo(
            plist_path=self.plist_path,
            executable=executable,
            endpoint=f"{LOOPBACK_HOST}:{self.config.service.port}",
            stdout_path=self.stdout_path,
            stderr_path=self.stderr_path,
            editable_install=self._is_editable_install(executable),
        )

    def _require_supported_platform(self) -> None:
        if self.platform != "darwin":
            raise ServiceLaunchdError(
                "unsupported_platform",
                "Ego Service LaunchAgent is supported only on macOS.",
            )

    @staticmethod
    def _validate_executable(executable: Path) -> None:
        if not executable.exists() or not executable.is_file():
            raise ServiceLaunchdError(
                "executable_not_found",
                f"Ego executable does not exist: {executable}",
            )
        if not os.access(executable, os.X_OK):
            raise ServiceLaunchdError(
                "executable_not_executable",
                f"Ego executable does not have execute permission: {executable}",
            )

    def _prepare_private_data_dir(self) -> None:
        if self.data_dir.is_symlink():
            raise ServiceLaunchdError(
                "unsafe_data_directory",
                f"Ego data directory must be a real directory, not a symlink: {self.data_dir}",
            )
        self.paths.ensure()
        if not self.data_dir.is_dir():
            raise ServiceLaunchdError(
                "unsafe_data_directory",
                f"Ego data directory must be a real directory, not a symlink: {self.data_dir}",
            )
        self.data_dir.chmod(0o700)

    def _prepare_launch_agents_dir(self) -> None:
        library = self.home / "Library"
        for location in (library, self.launch_agents_dir):
            if location.is_symlink():
                raise ServiceLaunchdError(
                    "unsafe_launchagent_location",
                    f"LaunchAgent location must not be a symlink: {location}",
                )
        self.launch_agents_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._validate_known_location()
        if self.plist_path.exists():
            self._validate_plist_file()

    def _validate_known_location(self) -> None:
        if self.launch_agents_dir.is_symlink() or self.plist_path.is_symlink():
            raise ServiceLaunchdError(
                "unsafe_launchagent_location",
                f"LaunchAgent path must not contain a symlink: {self.plist_path}",
            )
        if self.launch_agents_dir.exists():
            directory_stat = self.launch_agents_dir.stat()
            if not stat.S_ISDIR(directory_stat.st_mode) or directory_stat.st_uid != self.uid:
                raise ServiceLaunchdError(
                    "unsafe_launchagent_location",
                    f"LaunchAgent directory is not owned by the current user: "
                    f"{self.launch_agents_dir}",
                )
            if directory_stat.st_mode & 0o022:
                raise ServiceLaunchdError(
                    "unsafe_launchagent_location",
                    f"LaunchAgent directory is writable by group or others: "
                    f"{self.launch_agents_dir}",
                )

    def _validate_plist_file(self) -> None:
        if self.plist_path.is_symlink():
            raise ServiceLaunchdError(
                "unsafe_launchagent_location",
                f"LaunchAgent plist must not be a symlink: {self.plist_path}",
            )
        plist_stat = self.plist_path.stat()
        if not stat.S_ISREG(plist_stat.st_mode) or plist_stat.st_uid != self.uid:
            raise ServiceLaunchdError(
                "unsafe_launchagent_location",
                f"LaunchAgent plist must be a regular file owned by the current user: "
                f"{self.plist_path}",
            )
        if plist_stat.st_mode & 0o022:
            raise ServiceLaunchdError(
                "unsafe_launchagent_location",
                f"LaunchAgent plist is writable by group or others: {self.plist_path}",
            )

    def _write_plist_atomically(self) -> None:
        if self.plist_path.is_symlink():
            raise ServiceLaunchdError(
                "unsafe_launchagent_location",
                f"LaunchAgent plist must not be a symlink: {self.plist_path}",
            )
        payload = launch_agent_payload(
            executable=self.executable,
            home=self.home,
            data_dir=self.data_dir,
            stdout_path=self.stdout_path,
            stderr_path=self.stderr_path,
        )
        temporary = self.plist_path.with_name(
            f".{self.plist_path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                plistlib.dump(payload, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.plist_path)
            self.plist_path.chmod(0o600)
            directory_descriptor = os.open(self.launch_agents_dir, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError as error:
            raise ServiceLaunchdError(
                "plist_write_failed",
                f"Could not write LaunchAgent plist {self.plist_path}: {error}",
            ) from error
        finally:
            if temporary.exists():
                temporary.unlink()

    def _read_recorded_executable(self) -> Path:
        try:
            with self.plist_path.open("rb") as handle:
                payload = plistlib.load(handle)
            arguments = payload["ProgramArguments"]
            label = payload["Label"]
            if (
                label != LAUNCH_AGENT_LABEL
                or not isinstance(arguments, list)
                or arguments[1:] != ["service", "run"]
                or not isinstance(arguments[0], str)
            ):
                raise ValueError
        except (
            OSError,
            KeyError,
            IndexError,
            TypeError,
            ValueError,
            plistlib.InvalidFileException,
        ):
            raise ServiceLaunchdError(
                "incompatible_plist",
                f"Installed plist is not a compatible Ego LaunchAgent: {self.plist_path}",
            ) from None
        return Path(arguments[0])

    def _run_required(self, arguments: Sequence[str], *, code: str, action: str) -> None:
        result = self.command_runner(arguments)
        if result.returncode == 0:
            return
        detail = (result.stderr or result.stdout).strip()
        suffix = f": {detail}" if detail else ""
        raise ServiceLaunchdError(
            code,
            f"launchctl {action} failed with exit code {result.returncode}{suffix}",
        )

    def _wait_for_health(self) -> HealthResult:
        deadline = time.monotonic() + self.health_timeout_seconds
        last = HealthResult(HealthState.UNAVAILABLE, "Ego Service is not yet available.")
        while True:
            last = self.health_checker(
                LOOPBACK_HOST,
                self.config.service.port,
                ServiceCredentialStore(self.paths),
                min(
                    self.config.service.request_timeout_seconds,
                    max(self.health_timeout_seconds, 0.01),
                ),
                self.config.service.max_message_bytes,
            )
            if last.state in (
                HealthState.AUTHENTICATED,
                HealthState.INVALID_PROOF,
                HealthState.INCOMPATIBLE,
            ):
                return last
            if time.monotonic() >= deadline:
                return last
            time.sleep(self.health_poll_seconds)

    @staticmethod
    def _health_error_code(state: HealthState) -> str:
        return {
            HealthState.UNAVAILABLE: "service_not_listening",
            HealthState.TIMEOUT: "service_health_timeout",
            HealthState.INVALID_PROOF: "invalid_server_proof",
            HealthState.INCOMPATIBLE: "incompatible_service",
            HealthState.AUTHENTICATED: "service_healthy",
        }[state]

    @staticmethod
    def _is_editable_install(executable: Path) -> bool:
        parts = executable.parts
        return len(parts) >= 3 and parts[-3:] == (".venv", "bin", "ego")
