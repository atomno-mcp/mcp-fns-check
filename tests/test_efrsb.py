"""Тесты EfrsbClient через respx."""

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
from atomno_mcp_fns_check.sources.efrsb import EFRSB_BASE_URL, EFRSB_SEARCH_PATH, EfrsbClient

URL = f"{EFRSB_BASE_URL}{EFRSB_SEARCH_PATH}"


class TestEfrsbClient:
    async def test_invalid_inn_raises(self):
        async with EfrsbClient() as c:
            with pytest.raises(ValidationError):
                await c.search_active_by_inn("1111111111")

    async def test_no_active_cases(self):
        with respx.mock() as m:
            m.get(URL).mock(return_value=httpx.Response(200, json={"pageData": []}))
            async with EfrsbClient() as c:
                cases = await c.search_active_by_inn("7707083893")
        assert cases == []

    async def test_active_case_found(self):
        payload = {
            "pageData": [
                {
                    "caseNumber": "А40-12345/2024",
                    "stageName": "Конкурсное производство",
                    "isActive": True,
                }
            ]
        }
        with respx.mock() as m:
            m.get(URL).mock(return_value=httpx.Response(200, json=payload))
            async with EfrsbClient() as c:
                cases = await c.search_active_by_inn("7707083893")
        assert len(cases) == 1
        assert cases[0].case_number == "А40-12345/2024"
        assert cases[0].stage == "bankruptcy_proceedings"
        assert cases[0].is_active is True

    async def test_inactive_case_filtered(self):
        payload = {
            "pageData": [
                {"caseNumber": "X", "stageName": "Производство завершено", "isActive": False}
            ]
        }
        with respx.mock() as m:
            m.get(URL).mock(return_value=httpx.Response(200, json=payload))
            async with EfrsbClient() as c:
                cases = await c.search_active_by_inn("7707083893")
        assert cases == []

    async def test_404_returns_empty(self):
        with respx.mock() as m:
            m.get(URL).mock(return_value=httpx.Response(404))
            async with EfrsbClient() as c:
                cases = await c.search_active_by_inn("7707083893")
        assert cases == []

    async def test_429_raises_rate_limited(self):
        with respx.mock() as m:
            m.get(URL).mock(return_value=httpx.Response(429))
            async with EfrsbClient() as c:
                with pytest.raises(RateLimitedError):
                    await c.search_active_by_inn("7707083893")

    async def test_500_raises_source_unavailable(self):
        with respx.mock() as m:
            m.get(URL).mock(return_value=httpx.Response(503))
            async with EfrsbClient() as c:
                with pytest.raises(SourceUnavailableError):
                    await c.search_active_by_inn("7707083893")

    async def test_timeout(self):
        with respx.mock() as m:
            m.get(URL).mock(side_effect=httpx.TimeoutException("t"))
            async with EfrsbClient() as c:
                with pytest.raises(SourceUnavailableError):
                    await c.search_active_by_inn("7707083893")

    async def test_invalid_json(self):
        with respx.mock() as m:
            m.get(URL).mock(return_value=httpx.Response(200, content=b"<html/>"))
            async with EfrsbClient() as c:
                with pytest.raises(ParseError):
                    await c.search_active_by_inn("7707083893")

    async def test_payload_as_list(self):
        with respx.mock() as m:
            m.get(URL).mock(
                return_value=httpx.Response(
                    200,
                    json=[{"caseNumber": "X", "stageName": "Наблюдение"}],
                )
            )
            async with EfrsbClient() as c:
                cases = await c.search_active_by_inn("7707083893")
        assert len(cases) == 1
        assert cases[0].stage == "observation"
