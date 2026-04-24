"""Тесты get_directors_history (S3): возвращает только текущего руководителя + warning."""

from __future__ import annotations

import httpx
import pytest
import respx

from atomno_mcp_fns_check.context import ServiceContext
from atomno_mcp_fns_check.db.cache import SQLiteCache
from atomno_mcp_fns_check.errors import ValidationError
from atomno_mcp_fns_check.sources.efrsb import EfrsbClient
from atomno_mcp_fns_check.sources.egrul import EGRUL_BASE_URL, EgrulClient
from atomno_mcp_fns_check.tools.directors_history import get_directors_history

TOKEN = "tk-h"


def _mock(m: respx.MockRouter, payload: dict) -> None:
    m.post(f"{EGRUL_BASE_URL}/").mock(return_value=httpx.Response(200, json={"t": TOKEN}))
    m.get(f"{EGRUL_BASE_URL}/search-result/{TOKEN}").mock(
        return_value=httpx.Response(200, json=payload)
    )


@pytest.fixture
async def ctx(tmp_path):
    cache = SQLiteCache(tmp_path / "cache.sqlite", default_ttl_hours=24)
    egrul = EgrulClient(backoff_base=0.0)
    efrsb = EfrsbClient()
    c = ServiceContext.for_testing(egrul=egrul, efrsb=efrsb, cache=cache)
    await c.__aenter__()
    yield c
    await c.__aexit__(None, None, None)


class TestDirectorsHistory:
    async def test_invalid_inn(self, ctx):
        with pytest.raises(ValidationError):
            await get_directors_history(ctx, "1111111111")

    async def test_returns_current_director(self, ctx):
        payload = {
            "rows": [
                {
                    "i": "7707083893",
                    "o": "1027700132195",
                    "n": "ПАО СБЕРБАНК",
                    "g": "Греф Г. О.",
                    "gp": "Президент",
                    "k": "ul",
                }
            ]
        }
        with respx.mock() as m:
            _mock(m, payload)
            report = await get_directors_history(ctx, "7707083893")

        assert report.inn == "7707083893"
        assert len(report.directors_history) == 1
        cur = report.directors_history[0]
        assert cur.full_name == "Греф Г. О."
        assert cur.is_current is True
        assert report.total_director_changes == 1
        assert report.founders_history == []
        assert report.data_completeness_warning is not None
        assert "S5" in report.data_completeness_warning

    async def test_no_director_data(self, ctx):
        payload = {
            "rows": [
                {
                    "i": "7707083893",
                    "o": "1027700132195",
                    "n": "Без руководителя в карточке",
                    "k": "ul",
                }
            ]
        }
        with respx.mock() as m:
            _mock(m, payload)
            report = await get_directors_history(ctx, "7707083893")

        assert report.directors_history == []
        assert report.total_director_changes == 0
        assert report.data_completeness_warning is not None
