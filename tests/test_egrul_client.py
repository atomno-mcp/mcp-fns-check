"""Тесты EgrulClient через respx (мокинг httpx).

В тестах НЕ выполняются реальные сетевые запросы к egrul.nalog.ru — только
локальные фикстуры. Соответствует security-checklist группы AGENTS (sandbox-first).
"""

from __future__ import annotations

import httpx
import pytest
import respx

from atomno_mcp_fns_check.errors import (
    NotFoundError,
    ParseError,
    RateLimitedError,
    SourceUnavailableError,
    ValidationError,
)
from atomno_mcp_fns_check.sources.egrul import EGRUL_BASE_URL, EgrulClient

TOKEN = "abc-token-123"


def _mock_search_flow(respx_mock: respx.MockRouter, response_payload: dict) -> None:
    respx_mock.post(f"{EGRUL_BASE_URL}/").mock(
        return_value=httpx.Response(200, json={"t": TOKEN})
    )
    respx_mock.get(f"{EGRUL_BASE_URL}/search-result/{TOKEN}").mock(
        return_value=httpx.Response(200, json=response_payload)
    )


class TestEgrulClient:
    async def test_search_by_inn_happy_path(self, egrul_search_response: dict) -> None:
        with respx.mock(assert_all_called=True) as respx_mock:
            _mock_search_flow(respx_mock, egrul_search_response)

            async with EgrulClient(backoff_base=0.0) as client:
                hits = await client.search_by_inn("7707083893")

        assert len(hits) == 1
        hit = hits[0]
        assert hit.inn == "7707083893"
        assert hit.ogrn == "1027700132195"
        assert hit.kpp == "773601001"
        assert hit.name_full and "СБЕРБАНК" in hit.name_full
        assert hit.okved_code == "64.19"
        assert hit.director_name == "Греф Герман Оскарович"
        assert hit.is_individual is False

    async def test_search_by_ogrn(self, egrul_search_response: dict) -> None:
        with respx.mock(assert_all_called=True) as respx_mock:
            _mock_search_flow(respx_mock, egrul_search_response)

            async with EgrulClient(backoff_base=0.0) as client:
                hits = await client.search_by_ogrn("1027700132195")

        assert len(hits) == 1
        assert hits[0].ogrn == "1027700132195"

    async def test_search_invalid_inn_raises_before_http(self) -> None:
        async with EgrulClient(backoff_base=0.0) as client:
            with pytest.raises(ValidationError):
                await client.search_by_inn("1111111111")

    async def test_search_invalid_ogrn(self) -> None:
        async with EgrulClient(backoff_base=0.0) as client:
            with pytest.raises(ValidationError):
                await client.search_by_ogrn("123")

    async def test_search_not_found_when_rows_empty(self) -> None:
        with respx.mock() as respx_mock:
            _mock_search_flow(respx_mock, {"rows": []})

            async with EgrulClient(backoff_base=0.0) as client:
                with pytest.raises(NotFoundError):
                    await client.search_by_inn("7707083893")

    async def test_parse_error_when_no_rows_field(self) -> None:
        with respx.mock() as respx_mock:
            _mock_search_flow(respx_mock, {"oops": "wrong"})

            async with EgrulClient(backoff_base=0.0) as client:
                with pytest.raises(ParseError):
                    await client.search_by_inn("7707083893")

    async def test_parse_error_when_no_token(self) -> None:
        with respx.mock() as respx_mock:
            respx_mock.post(f"{EGRUL_BASE_URL}/").mock(
                return_value=httpx.Response(200, json={"no_token": True})
            )
            async with EgrulClient(backoff_base=0.0) as client:
                with pytest.raises(ParseError):
                    await client.search_by_inn("7707083893")

    async def test_5xx_retries_then_succeeds(self, egrul_search_response: dict) -> None:
        with respx.mock() as respx_mock:
            respx_mock.post(f"{EGRUL_BASE_URL}/").mock(
                side_effect=[
                    httpx.Response(503),
                    httpx.Response(200, json={"t": TOKEN}),
                ]
            )
            respx_mock.get(f"{EGRUL_BASE_URL}/search-result/{TOKEN}").mock(
                return_value=httpx.Response(200, json=egrul_search_response)
            )

            async with EgrulClient(backoff_base=0.0, max_attempts=3) as client:
                hits = await client.search_by_inn("7707083893")
                assert len(hits) == 1

    async def test_5xx_exhausts_attempts(self) -> None:
        with respx.mock() as respx_mock:
            respx_mock.post(f"{EGRUL_BASE_URL}/").mock(return_value=httpx.Response(500))

            async with EgrulClient(backoff_base=0.0, max_attempts=2) as client:
                with pytest.raises(SourceUnavailableError):
                    await client.search_by_inn("7707083893")

    async def test_429_rate_limited(self) -> None:
        with respx.mock() as respx_mock:
            respx_mock.post(f"{EGRUL_BASE_URL}/").mock(return_value=httpx.Response(429))

            async with EgrulClient(backoff_base=0.0, max_attempts=2) as client:
                with pytest.raises(RateLimitedError):
                    await client.search_by_inn("7707083893")

    async def test_4xx_no_retry(self) -> None:
        with respx.mock() as respx_mock:
            respx_mock.post(f"{EGRUL_BASE_URL}/").mock(return_value=httpx.Response(404))

            async with EgrulClient(backoff_base=0.0, max_attempts=3) as client:
                with pytest.raises(SourceUnavailableError):
                    await client.search_by_inn("7707083893")

    async def test_timeout(self) -> None:
        with respx.mock() as respx_mock:
            respx_mock.post(f"{EGRUL_BASE_URL}/").mock(side_effect=httpx.TimeoutException("timeout"))

            async with EgrulClient(backoff_base=0.0, max_attempts=2) as client:
                with pytest.raises(SourceUnavailableError):
                    await client.search_by_inn("7707083893")

    async def test_invalid_json_body(self) -> None:
        with respx.mock() as respx_mock:
            respx_mock.post(f"{EGRUL_BASE_URL}/").mock(
                return_value=httpx.Response(200, content=b"<html>not json</html>")
            )
            async with EgrulClient(backoff_base=0.0) as client:
                with pytest.raises(ParseError):
                    await client.search_by_inn("7707083893")
