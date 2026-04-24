"""Тесты клиента Прозрачного бизнеса (pb.nalog.ru)."""

from __future__ import annotations

import httpx
import pytest
import respx

from atomno_mcp_fns_check.errors import (
    ParseError,
    RateLimitedError,
    SourceUnavailableError,
    ValidationError,
)
from atomno_mcp_fns_check.sources.pb_fns import (
    PB_BASE_URL,
    PB_SEARCH_PATH,
    PbFnsClient,
)

PB_URL = f"{PB_BASE_URL}{PB_SEARCH_PATH}"


@pytest.fixture
async def client():
    async with PbFnsClient(timeout=2.0) as c:
        yield c


class TestPbFns:
    async def test_invalid_inn_validation(self, client):
        with pytest.raises(ValidationError):
            await client.get_tags_by_inn("1234")

    async def test_no_indicators(self, client):
        with respx.mock() as m:
            m.get(PB_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={"ul": {"data": [{"tags": ["clean", "ok"]}]}},
                )
            )
            tags = await client.get_tags_by_inn("7707083893")
        assert tags.has_tax_debt is False
        assert tags.has_no_reporting is False
        assert "clean" in tags.raw_tags

    async def test_tax_debt_indicator(self, client):
        with respx.mock() as m:
            m.get(PB_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "ul": {
                            "data": [
                                {"tags": ["tax_debt", "active"]},
                            ]
                        }
                    },
                )
            )
            tags = await client.get_tags_by_inn("7707083893")
        assert tags.has_tax_debt is True
        assert tags.has_no_reporting is False

    async def test_no_reporting_indicator_russian(self, client):
        with respx.mock() as m:
            m.get(PB_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "data": [
                            {"tags": ["сведения о непредставлении отчётности более года"]},
                        ]
                    },
                )
            )
            tags = await client.get_tags_by_inn("7707083893")
        assert tags.has_no_reporting is True

    async def test_individual_uses_search_ip_mode(self, client):
        with respx.mock() as m:
            route = m.get(PB_URL).mock(
                return_value=httpx.Response(200, json={"ip": {"data": [{"tags": []}]}})
            )
            await client.get_tags_by_inn("500100732259")  # 12-знач. ИНН
            assert route.called
            # mode=search-ip передан в query.
            req_url = str(route.calls[0].request.url)
            assert "mode=search-ip" in req_url

    async def test_404_returns_empty(self, client):
        with respx.mock() as m:
            m.get(PB_URL).mock(return_value=httpx.Response(404))
            tags = await client.get_tags_by_inn("7707083893")
        assert tags.has_tax_debt is False
        assert tags.has_no_reporting is False

    async def test_429_raises_rate_limited(self, client):
        with respx.mock() as m:
            m.get(PB_URL).mock(return_value=httpx.Response(429))
            with pytest.raises(RateLimitedError):
                await client.get_tags_by_inn("7707083893")

    async def test_5xx_raises_unavailable(self, client):
        with respx.mock() as m:
            m.get(PB_URL).mock(return_value=httpx.Response(500))
            with pytest.raises(SourceUnavailableError):
                await client.get_tags_by_inn("7707083893")

    async def test_timeout_raises_unavailable(self, client):
        with respx.mock() as m:
            m.get(PB_URL).mock(side_effect=httpx.TimeoutException("boom"))
            with pytest.raises(SourceUnavailableError):
                await client.get_tags_by_inn("7707083893")

    async def test_invalid_json_raises_parse_error(self, client):
        with respx.mock() as m:
            m.get(PB_URL).mock(
                return_value=httpx.Response(
                    200, content=b"<html>not json</html>", headers={"content-type": "text/html"}
                )
            )
            with pytest.raises(ParseError):
                await client.get_tags_by_inn("7707083893")
