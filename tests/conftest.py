from __future__ import annotations

from pathlib import Path

import pytest

from ego.config import AppPaths
from ego.storage import Database


@pytest.fixture
def app_paths(tmp_path: Path) -> AppPaths:
    return AppPaths(
        data_dir=tmp_path / "data",
        database=tmp_path / "data" / "ego.sqlite3",
        raw_dir=tmp_path / "data" / "raw",
        config_file=tmp_path / "data" / "config.toml",
    )


@pytest.fixture
def database(app_paths: AppPaths) -> Database:
    return Database(app_paths)
