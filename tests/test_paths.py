"""Путь кэша по умолчанию — каталог данных пользователя, не текущая папка."""

from __future__ import annotations

from pathlib import Path

from atomno_mcp_fns_check.paths import (
    CACHE_FILENAME,
    default_cache_db_path,
    resolve_cache_db_path,
    user_data_dir,
)
from atomno_mcp_fns_check.server import _cache_db_path


def test_default_cache_uses_user_data_dir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.delenv("MCP_FNS_CACHE_DB", raising=False)

    dest = default_cache_db_path()
    assert dest == tmp_path / "atomno" / CACHE_FILENAME
    assert dest.parent.is_dir()
    assert dest != Path.cwd() / CACHE_FILENAME
    assert _cache_db_path() == str(dest)


def test_env_overrides_default(monkeypatch, tmp_path: Path) -> None:
    custom = tmp_path / "custom.sqlite"
    monkeypatch.setenv("MCP_FNS_CACHE_DB", str(custom))
    # Боевой сервер задаёт путь явно; домашний каталог при этом недоступен.
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "no-home-write"))
    (tmp_path / "no-home-write").mkdir()
    assert resolve_cache_db_path() == str(custom)
    assert _cache_db_path() == str(custom)
    assert not (tmp_path / "no-home-write" / ".local").exists()


def test_user_data_dir_creates_folder(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert user_data_dir().is_dir()
