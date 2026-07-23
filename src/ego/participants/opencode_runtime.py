from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path

from ego.models import Phase, TurnRequest

_SAFE_CONFIG_KEYS = frozenset(
    {
        "disabled_providers",
        "enabled_providers",
        "model",
        "provider",
    }
)
_ENV_REFERENCE = re.compile(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}")


class OpenCodeRuntime:
    def __init__(self, model_override: str | None) -> None:
        source_config, self.config_error = self._read_source_config()
        self.safe_source_config = {
            key: value for key, value in source_config.items() if key in _SAFE_CONFIG_KEYS
        }
        self.environment_keys = frozenset(_environment_references(self.safe_source_config))
        self.model_override = model_override
        self.resolved_model = model_override or _configured_model(self.safe_source_config)
        if self.resolved_model is None:
            self.resolved_model = self._recent_model()

    @staticmethod
    def _config_root() -> Path:
        return Path(
            os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
        ).expanduser() / "opencode"

    @staticmethod
    def _data_root() -> Path:
        return Path(
            os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
        ).expanduser() / "opencode"

    @staticmethod
    def _state_root() -> Path:
        return Path(
            os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
        ).expanduser() / "opencode"

    def _read_source_config(self) -> tuple[dict[str, object], str | None]:
        for name in ("opencode.json", "opencode.jsonc"):
            path = self._config_root() / name
            if not path.is_file():
                continue
            try:
                value = json.loads(_normalize_jsonc(path.read_text(encoding="utf-8")))
            except (OSError, ValueError) as error:
                return {}, f"could not read OpenCode configuration {path}: {error}"
            if not isinstance(value, dict):
                return {}, f"OpenCode configuration {path} must contain a JSON object"
            return value, None
        return {}, None

    def _recent_model(self) -> str | None:
        path = self._state_root() / "model.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict) or not isinstance(value.get("recent"), list):
            return None
        for item in value["recent"]:
            if not isinstance(item, dict):
                continue
            provider = item.get("providerID")
            model = item.get("modelID")
            if isinstance(provider, str) and isinstance(model, str):
                return f"{provider}/{model}"
        return None

    def create(
        self, request: TurnRequest | None
    ) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        runtime = tempfile.TemporaryDirectory(prefix="ego-opencode-")
        runtime_home = Path(runtime.name)
        config_home = runtime_home / "config"
        data_home = runtime_home / "data"
        state_home = runtime_home / "state"
        for path in (config_home / "opencode", data_home / "opencode", state_home / "opencode"):
            path.mkdir(parents=True)
        (runtime_home / "project").mkdir()

        config = dict(self.safe_source_config)
        if self.model_override:
            config["model"] = self.model_override
        permissions = _permissions(request)
        config.update(
            {
                "agent": {
                    "ego": {
                        "description": "Read-only decision support for Ego",
                        "mode": "primary",
                        "prompt": (
                            "Inspect only the target workspace named in the user prompt. "
                            "Never edit files, run commands, use the web, or delegate."
                        ),
                        "permission": permissions,
                    }
                },
                "autoupdate": False,
                "default_agent": "ego",
                "instructions": [],
                "mcp": {},
                "permission": permissions,
                "plugin": [],
                "share": "disabled",
            }
        )
        config_path = config_home / "opencode" / "opencode.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        config_path.chmod(stat.S_IRUSR | stat.S_IWUSR)

        self._copy_private_file(
            self._data_root() / "auth.json",
            data_home / "opencode" / "auth.json",
        )
        self._copy_private_file(
            self._state_root() / "model.json",
            state_home / "opencode" / "model.json",
        )
        return runtime, runtime_home

    @staticmethod
    def environment(runtime_home: Path) -> dict[str, str]:
        return {
            "HOME": str(runtime_home),
            "XDG_CONFIG_HOME": str(runtime_home / "config"),
            "XDG_DATA_HOME": str(runtime_home / "data"),
            "XDG_CACHE_HOME": str(runtime_home / "cache"),
            "XDG_STATE_HOME": str(runtime_home / "state"),
            "OPENCODE_DISABLE_AUTOUPDATE": "true",
            "OPENCODE_DISABLE_CLAUDE_CODE": "true",
            "OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER": "true",
        }

    @staticmethod
    def _copy_private_file(source: Path, destination: Path) -> None:
        if not source.is_file():
            return
        shutil.copyfile(source, destination)
        destination.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _permissions(request: TurnRequest | None) -> dict[str, object]:
    permissions: dict[str, object] = {
        "*": "deny",
        "bash": "deny",
        "edit": "deny",
        "external_directory": "deny",
        "glob": "deny",
        "grep": "deny",
        "list": "deny",
        "lsp": "deny",
        "question": "deny",
        "read": "deny",
        "skill": "deny",
        "task": "deny",
        "webfetch": "deny",
        "websearch": "deny",
    }
    if request is None or request.phase not in {Phase.INDEPENDENT, Phase.PEER_REVIEW}:
        return permissions
    workspace = str(Path(request.workspace).resolve())
    workspace_access = {
        "*": "deny",
        workspace: "allow",
        f"{workspace}/**": "allow",
    }
    permissions.update(
        {
            "external_directory": workspace_access,
            "glob": "allow",
            "grep": "allow",
            "list": "allow",
            "read": workspace_access,
        }
    )
    return permissions


def _configured_model(config: dict[str, object]) -> str | None:
    model = config.get("model")
    return model if isinstance(model, str) and model else None


def _environment_references(value: object) -> set[str]:
    if isinstance(value, str):
        return set(_ENV_REFERENCE.findall(value))
    if isinstance(value, list):
        result: set[str] = set()
        for item in value:
            result.update(_environment_references(item))
        return result
    if isinstance(value, dict):
        result = set()
        for item in value.values():
            result.update(_environment_references(item))
        return result
    return set()


def _normalize_jsonc(text: str) -> str:
    without_comments: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        character = text[index]
        if in_string:
            without_comments.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
            without_comments.append(character)
            index += 1
            continue
        if character == "/" and index + 1 < len(text) and text[index + 1] == "/":
            index += 2
            while index < len(text) and text[index] not in "\r\n":
                index += 1
            continue
        if character == "/" and index + 1 < len(text) and text[index + 1] == "*":
            index += 2
            while index + 1 < len(text) and text[index : index + 2] != "*/":
                index += 1
            index += 2
            continue
        without_comments.append(character)
        index += 1
    return _remove_trailing_commas("".join(without_comments))


def _remove_trailing_commas(text: str) -> str:
    result: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        character = text[index]
        if in_string:
            result.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
            result.append(character)
            index += 1
            continue
        if character == ",":
            lookahead = index + 1
            while lookahead < len(text) and text[lookahead].isspace():
                lookahead += 1
            if lookahead < len(text) and text[lookahead] in "}]":
                index += 1
                continue
        result.append(character)
        index += 1
    return "".join(result)
