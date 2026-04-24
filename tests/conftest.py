"""Общие фикстуры для тестов atomno-mcp-fns-check."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def egrul_search_response() -> dict:
    """Фикстура: типичный ответ /search-result/<token> ЕГРЮЛ для одного юр.лица."""
    with (FIXTURES_DIR / "egrul_sber.json").open(encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def cache_db(tmp_path) -> Path:
    return tmp_path / "test_cache.sqlite"
