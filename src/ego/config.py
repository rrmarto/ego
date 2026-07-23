from __future__ import annotations

import os
import tomllib
from pathlib import Path

from platformdirs import user_data_path
from pydantic import BaseModel, Field


class ParticipantConfig(BaseModel):
    enabled: bool = True
    binary: str | None = None
    model: str | None = None
    timeout_seconds: float = Field(default=600, gt=0)


class EgoConfig(BaseModel):
    raw_retention_days: int = Field(default=30, ge=0)
    output_limit_bytes: int = Field(default=5 * 1024 * 1024, gt=0)
    participants: dict[str, ParticipantConfig] = Field(
        default_factory=lambda: {
            name: ParticipantConfig()
            for name in ("codex", "claude", "gemini", "copilot", "opencode")
        }
    )


class AppPaths(BaseModel):
    data_dir: Path
    database: Path
    raw_dir: Path
    config_file: Path

    @classmethod
    def resolve(cls) -> AppPaths:
        override = os.environ.get("EGO_DATA_DIR")
        data_dir = Path(override).expanduser() if override else user_data_path("Ego", "Ego")
        return cls(
            data_dir=data_dir,
            database=data_dir / "ego.sqlite3",
            raw_dir=data_dir / "raw",
            config_file=data_dir / "config.toml",
        )

    def ensure(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)


def load_config(paths: AppPaths) -> EgoConfig:
    if not paths.config_file.exists():
        return EgoConfig()
    with paths.config_file.open("rb") as handle:
        return EgoConfig.model_validate(tomllib.load(handle))
