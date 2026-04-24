"""Тесты клиента КАД (kad.arbitr.ru)."""

from __future__ import annotations

import httpx
import pytest
import respx

from atomno_mcp_fns_check.errors import SourceUnavailableError, ValidationError
from atomno_mcp_fns_check.sources.kad import KAD_BASE_URL, KAD_SEARCH_PATH, KadClient

KAD_URL = f"{KAD_BASE_URL}{KAD_SEARCH_PATH}"


@pytest.fixture
async def client():
    async with KadClient(timeout=2.0) as c:
        yield c


def _kad_response(items: list[dict]) -> httpx.Response:
    return httpx.Response(
        200,
        json={"Result": {"Items": items}},
        headers={"content-type": "application/json"},
    )


class TestKad:
    async def test_invalid_inn(self, client):
        with pytest.raises(ValidationError):
            await client.search_active_lawsuits_by_inn("xxx")

    async def test_no_cases(self, client):
        with respx.mock() as m:
            m.post(KAD_URL).mock(return_value=_kad_response([]))
            cases = await client.search_active_lawsuits_by_inn("7707083893")
        assert cases == []

    async def test_open_defendant_cases_parsed(self, client):
        with respx.mock() as m:
            m.post(KAD_URL).mock(
                return_value=_kad_response(
                    [
                        {
                            "CaseNumber": "А40-100500/2025",
                            "Role": "Ответчик",
                            "Amount": "1 500 000,00",
                            "Status": "Принято к производству",
                        },
                        {
                            "CaseNumber": "А40-100501/2025",
                            "Role": "Ответчик",
                            "Amount": "300000",
                            "Status": "Рассматривается",
                        },
                    ]
                )
            )
            cases = await client.search_active_lawsuits_by_inn("7707083893")
        assert len(cases) == 2
        assert cases[0].amount_rub == 1_500_000.0
        assert cases[0].is_defendant
        assert cases[0].is_open

    async def test_filters_out_closed(self, client):
        with respx.mock() as m:
            m.post(KAD_URL).mock(
                return_value=_kad_response(
                    [
                        {
                            "CaseNumber": "А40-1/2020",
                            "Role": "Ответчик",
                            "Status": "Архив",
                        },
                        {
                            "CaseNumber": "А40-2/2020",
                            "Role": "Ответчик",
                            "Status": "Закончено",
                        },
                    ]
                )
            )
            cases = await client.search_active_lawsuits_by_inn("7707083893")
        assert cases == []

    async def test_filters_out_non_defendant(self, client):
        with respx.mock() as m:
            m.post(KAD_URL).mock(
                return_value=_kad_response(
                    [
                        {
                            "CaseNumber": "А40-3/2025",
                            "Role": "Истец",
                            "Status": "Принято",
                        },
                    ]
                )
            )
            cases = await client.search_active_lawsuits_by_inn("7707083893")
        assert cases == []

    async def test_403_blocks_raises_unavailable(self, client):
        with respx.mock() as m:
            m.post(KAD_URL).mock(return_value=httpx.Response(403))
            with pytest.raises(SourceUnavailableError):
                await client.search_active_lawsuits_by_inn("7707083893")

    async def test_html_antibot_raises_unavailable(self, client):
        with respx.mock() as m:
            m.post(KAD_URL).mock(
                return_value=httpx.Response(
                    200,
                    content=b"<html>captcha</html>",
                    headers={"content-type": "text/html"},
                )
            )
            with pytest.raises(SourceUnavailableError):
                await client.search_active_lawsuits_by_inn("7707083893")

    async def test_5xx(self, client):
        with respx.mock() as m:
            m.post(KAD_URL).mock(return_value=httpx.Response(503))
            with pytest.raises(SourceUnavailableError):
                await client.search_active_lawsuits_by_inn("7707083893")
