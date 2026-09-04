"""Пути по умолчанию: кэш кладём в каталог данных пользователя, не в его проект."""

from __future__ import annotations

import os
from pathlib import Path

CACHE_FILENAME = "atomno_mcp_fns_check_cache.sqlite"


def user_data_dir(*, app: str = "atomno") -> Path:
    """Каталог данных пользователя: LOCALAPPDATA/atomno или ~/.local/share/atomno."""
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    else:
        root = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    dest = root / app
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def default_cache_db_path() -> Path:
    return user_data_dir() / CACHE_FILENAME
