"""End-to-end тесты check_inn / check_ogrn / get_okveds через ServiceContext.

Проверяем сквозной путь: тул → cache miss → EGRUL HTTP → парсинг → cache put →
повторный вызов = cache hit (без HTTP).
"""

from __future__ import annotations

import httpx
import pytest
import respx

from atomno_mcp_fns_check.context import ServiceContext
from atomno_mcp_fns_check.db.cache import SQLiteCache
from atomno_mcp_fns_check.errors import NotFoundError, ValidationError
from atomno_mcp_fns_check.sources.efrsb import EfrsbClient
from atomno_mcp_fns_check.sources.egrul import EGRUL_BASE_URL, EgrulClient
from atomno_mcp_fns_check.tools.check import check_inn, check_ogrn, get_okveds

TOKEN = "tok"


def _mock_egrul(m: respx.MockRouter, payload: dict, *, count: int = 1) -> respx.Route:
    m.post(f"{EGRUL_BASE_URL}/").mock(return_value=httpx.Response(200, json={"t": TOKEN}))
    return m.get(f"{EGRUL_BASE_URL}/search-result/{TOKEN}").mock(
        return_value=httpx.Response(200, json=payload)
    )


@pytest.fixture
async def ctx(tmp_path) -> ServiceContext:
    cache = SQLiteCache(tmp_path / "cache.sqlite", default_ttl_hours=24)
    egrul = EgrulClient(backoff_base=0.0)
    efrsb = EfrsbClient()
    c = ServiceContext.for_testing(egrul=egrul, efrsb=efrsb, cache=cache)
    await c.__aenter__()
    yield c
    await c.__aexit__(None, None, None)


SBER = {
    "rows": [
        {
            "i": "7707083893",
            "o": "1027700132195",
            "p": "773601001",
            "n": "ПАО СБЕРБАНК",
            "c": "СБЕРБАНК",
            "a": "117997, Г.Москва, УЛ. ВАВИЛОВА, Д. 19",
            "e": "64.19",
            "eName": "Денежное посредничество прочее",
            "g": "Греф Г. О.",
            "gp": "Президент",
            "r": "1991-06-20",
            "k": "ul",
        }
    ]
}


class TestCheckInn:
    async def test_happy_path_and_cache_hit(self, ctx: ServiceContext):
        with respx.mock() as m:
            route = _mock_egrul(m, SBER)
            card1 = await check_inn(ctx, "7707083893")
            assert card1.inn == "7707083893"
            assert card1.name.full == "ПАО СБЕРБАНК"
            assert card1.okved_main is not None
            assert card1.okved_main.code == "64.19"

            initial_calls = route.call_count

            card2 = await check_inn(ctx, "7707083893")
            assert card2.inn == "7707083893"
            assert route.call_count == initial_calls

    async def test_invalid_inn(self, ctx: ServiceContext):
        with pytest.raises(ValidationError):
            await check_inn(ctx, "1111111111")

    async def test_not_found(self, ctx: ServiceContext):
        with respx.mock() as m:
            _mock_egrul(m, {"rows": []})
            with pytest.raises(NotFoundError):
                await check_inn(ctx, "7707083893")


class TestCheckOgrn:
    async def test_happy(self, ctx: ServiceContext):
        with respx.mock() as m:
            _mock_egrul(m, SBER)
            card = await check_ogrn(ctx, "1027700132195")
            assert card.inn == "7707083893"
            assert card.ogrn == "1027700132195"

    async def test_invalid_ogrn(self, ctx: ServiceContext):
        with pytest.raises(ValidationError):
            await check_ogrn(ctx, "1234567890123")


class TestGetOkveds:
    async def test_happy_with_inn(self, ctx: ServiceContext):
        with respx.mock() as m:
            _mock_egrul(m, SBER)
            report = await get_okveds(ctx, inn="7707083893")
        assert report.main_okved is not None
        assert report.main_okved.code == "64.19"
        assert report.main_okved.is_licensable is True
        assert report.total_count == 1

    async def test_requires_inn_or_ogrn(self, ctx: ServiceContext):
        with pytest.raises(ValidationError):
            await get_okveds(ctx)

    async def test_uses_cache_after_check_inn(self, ctx: ServiceContext):
        with respx.mock() as m:
            route = _mock_egrul(m, SBER)
            await check_inn(ctx, "7707083893")
            calls_after_first = route.call_count
            report = await get_okveds(ctx, inn="7707083893")
            assert route.call_count == calls_after_first
            assert report.main_okved is not None
