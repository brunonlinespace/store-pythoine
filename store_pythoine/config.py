from __future__ import annotations

import os
from pathlib import Path

from . import APP_ID, PUBLISHER


def _xdg_dir(env_name: str, fallback: Path) -> Path:
    value = os.environ.get(env_name, "").strip()
    return Path(value).expanduser() if value else fallback


def cache_dir() -> Path:
    return _xdg_dir("XDG_CACHE_HOME", Path.home() / ".cache") / PUBLISHER / APP_ID


def data_dir() -> Path:
    return _xdg_dir("XDG_DATA_HOME", Path.home() / ".local" / "share") / PUBLISHER / APP_ID


def legacy_cache_dir() -> Path:
    return _xdg_dir("XDG_CACHE_HOME", Path.home() / ".cache") / PUBLISHER / "get-pythoinated"
