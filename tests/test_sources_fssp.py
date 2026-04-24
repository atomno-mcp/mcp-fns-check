"""Тесты клиента ФССП (fssp.gov.ru/iss/ip)."""

from __future__ import annotations

import httpx
import pytest
import respx

from atomno_mcp_fns_check.errors import (
    RateLimitedError,
    SourceUnavailableError,
    ValidationError,
)
from atomno_mcp_fns_check.sources.fssp import (
    FSSP_BASE_URL,
    FSSP_SEARCH_PATH,
    FsspClient,
)

FSSP_URL = f"{FSSP_BASE_URL}{FSSP_SEARCH_PATH}"


@pytest.fixture
async def client():
    async with FsspClient(timeout=2.0) as c:
        yield c


class TestFssp:
    async def test_invalid_inn(self, client):
        with pytest.raises(ValidationError):
            await client.search_proceedings_by_inn("xxx")

    async def test_no_proceedings(self, client):
        with respx.mock() as m:
            m.get(FSSP_URL).mock(
                return_value=httpx.Response(
                    200, json={"response": {"result": []}}
                )
            )
            cases = await client.search_proceedings_by_inn("7707083893")
        assert cases == []

    async def test_open_proceedings_parsed(self, client):
        with respx.mock() as m:
            m.get(FSSP_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "response": {
                            "result": [
                                {
                                    "ip_number": "12345/24/77001-ИП",
                                    "debt_amount": "150 000,50",
                                    "status": "Возбуждено",
                                },
                                {
                                    "ip_number": "67890/24/77001-ИП",
                                    "debt_amount": 50000,
                                    "status": "В работе",
                                },
                            ]
                        }
                    },
                )
            )
            cases = await client.search_proceedings_by_inn("7707083893")
        assert len(cases) == 2
        assert cases[0].debt_amount_rub == 150000.5
        assert cases[0].is_open is True

    async def test_closed_filtered_out(self, client):
        with respx.mock() as m:
            m.get(FSSP_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "response": {
                            "result": [
                                {
                                    "ip_number": "А",
                                    "debt_amount": 1000,
                                    "status": "Окончено",
                                },
                                {
                                    "ip_number": "Б",
                                    "debt_amount": 1000,
                                    "status": "Прекращено",
                                },
                            ]
                        }
                    },
                )
            )
            cases = await client.search_proceedings_by_inn("7707083893")
        assert cases == []

    async def test_captcha_raises_unavailable(self, client):
        with respx.mock() as m:
            m.get(FSSP_URL).mock(
                return_value=httpx.Response(
                    200, json={"captcha": True, "response": {"result": []}}
                )
            )
            with pytest.raises(SourceUnavailableError) as ei:
                await client.search_proceedings_by_inn("7707083893")
            assert "CAPTCHA" in str(ei.value) or "captcha" in str(ei.value).lower()

    async def test_429(self, client):
        with respx.mock() as m:
            m.get(FSSP_URL).mock(return_value=httpx.Response(429))
            with pytest.raises(RateLimitedError):
                await client.search_proceedings_by_inn("7707083893")

    async def test_5xx(self, client):
        with respx.mock() as m:
            m.get(FSSP_URL).mock(return_value=httpx.Response(503))
            with pytest.raises(SourceUnavailableError):
                await client.search_proceedings_by_inn("7707083893")
