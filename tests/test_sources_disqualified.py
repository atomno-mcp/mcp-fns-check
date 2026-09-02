"""Тесты клиента реестра дисквалифицированных лиц ФНС.

Ответы источника — настоящие, сняты живыми запросами 02.08.2026:

  * `disqualified_hit.json`  — ООО «ГЕРААРТ» (ИНН 2368016435), в управлении
    дисквалифицированное лицо: `rowCount: 1`;
  * `disqualified_miss.json` — ПАО СБЕРБАНК (ИНН 7707083893): `rowCount: 0`.

Отдельно проверяется ловушка источника: если сервер проигнорировал условие
поиска и вернул весь реестр (`rowCount ≈ 8563`), это ошибка разбора, а не
совпадение. Иначе любой контрагент получал бы ложное «дисквалифицирован».
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import httpx
import pytest

from atomno_mcp_fns_check.errors import (
    ParseError,
    RateLimitedError,
    SourceUnavailableError,
    ValidationError,
)
from atomno_mcp_fns_check.sources.disqualified import DisqualifiedClient

FIXTURES = Path(__file__).parent / "fixtures"
INN_WITH_DISQ = "2368016435"
INN_CLEAN = "7707083893"

HIT = json.loads((FIXTURES / "disqualified_hit.json").read_text(encoding="utf-8"))
MISS = json.loads((FIXTURES / "disqualified_miss.json").read_text(encoding="utf-8"))

JSON_HEADERS = {"content-type": "application/json; charset=utf-8"}


def _client(handler) -> DisqualifiedClient:
    return DisqualifiedClient(
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://service.nalog.ru",
        )
    )


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_company_with_disqualified_director(self):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["form"] = dict(httpx.QueryParams(request.content.decode()))
            return httpx.Response(200, json=HIT, headers=JSON_HEADERS)

        async with _client(handler) as d:
            records = await d.search_by_company_inn(INN_WITH_DISQ)

        assert len(records) == 1
        rec = records[0]
        assert rec.full_name == "АБАЗЯН МАРИНА АНДРЕЕВНА"
        assert rec.organization_inn == INN_WITH_DISQ
        assert rec.position == "ГЕНЕРАЛЬНЫЙ ДИРЕКТОР"
        assert rec.reason == "Ч.5 СТ. 14.25 КОАП РФ"
        assert rec.disqualification_from.startswith("12.08.2025")
        assert rec.disqualification_until.startswith("11.08.2026")
        # Запрос ушёл единственным полем, которое источник реально применяет.
        assert seen["form"]["query"] == INN_WITH_DISQ
        assert "fam" not in seen["form"]

    @pytest.mark.asyncio
    async def test_clean_company_returns_empty(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=MISS, headers=JSON_HEADERS)

        async with _client(handler) as d:
            assert await d.search_by_company_inn(INN_CLEAN) == []

    @pytest.mark.asyncio
    async def test_expired_disqualification_is_not_active(self):
        payload = copy.deepcopy(HIT)
        payload["data"][0]["ДатаНачДискв"] = "01.01.2019 00:00:00"
        payload["data"][0]["ДатаКонДискв"] = "01.01.2020 00:00:00"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload, headers=JSON_HEADERS)

        async with _client(handler) as d:
            records = await d.search_by_company_inn(INN_WITH_DISQ)

        assert len(records) == 1
        assert records[0].is_active is False

    @pytest.mark.asyncio
    async def test_unparsable_end_date_counts_as_active(self):
        """Не понимаем дату — считаем действующей: занизить риск опаснее."""
        payload = copy.deepcopy(HIT)
        payload["data"][0]["ДатаКонДискв"] = None

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload, headers=JSON_HEADERS)

        async with _client(handler) as d:
            records = await d.search_by_company_inn(INN_WITH_DISQ)

        assert records[0].is_active is True


class TestFilterNotApplied:
    """Главная ловушка источника: фильтр проигнорирован — это не совпадение."""

    @pytest.mark.asyncio
    async def test_full_registry_response_raises_parse_error(self):
        payload = copy.deepcopy(HIT)
        payload["rowCount"] = 8563  # весь реестр
        second = copy.deepcopy(payload["data"][0])
        second["ROW_NUM"] = 2
        second["ИННОрг"] = "9999999999"
        second["ФИО"] = "ПЕТРОВ ПЁТР ПЕТРОВИЧ"
        payload["data"].append(second)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload, headers=JSON_HEADERS)

        async with _client(handler) as d:
            with pytest.raises(ParseError) as exc:
                await d.search_by_company_inn(INN_CLEAN)

        assert exc.value.details.get("reason") == "filter_not_applied"

    @pytest.mark.asyncio
    async def test_row_not_matching_query_is_dropped(self):
        """Строка не про наш ИНН — не считаем её совпадением."""
        payload = copy.deepcopy(MISS)
        row = copy.deepcopy(HIT["data"][0])
        payload["data"] = [row]
        payload["rowCount"] = 1

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload, headers=JSON_HEADERS)

        async with _client(handler) as d:
            assert await d.search_by_company_inn(INN_CLEAN) == []


class TestErrorResponses:
    @pytest.mark.asyncio
    async def test_rate_limit_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, text="")

        async with _client(handler) as d:
            with pytest.raises(RateLimitedError):
                await d.search_by_company_inn(INN_CLEAN)

    @pytest.mark.asyncio
    async def test_server_error_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="Internal Server Error")

        async with _client(handler) as d:
            with pytest.raises(SourceUnavailableError):
                await d.search_by_company_inn(INN_CLEAN)

    @pytest.mark.asyncio
    async def test_timeout_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        async with _client(handler) as d:
            with pytest.raises(SourceUnavailableError):
                await d.search_by_company_inn(INN_CLEAN)

    @pytest.mark.asyncio
    async def test_captcha_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"captchaRequired": True}, headers=JSON_HEADERS
            )

        async with _client(handler) as d:
            with pytest.raises(SourceUnavailableError) as exc:
                await d.search_by_company_inn(INN_CLEAN)

        assert exc.value.details.get("reason") == "captcha_required"


class TestUnclearResponses:
    @pytest.mark.asyncio
    async def test_html_instead_of_json_raises_parse_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text="<html><body>Технические работы</body></html>",
                headers={"content-type": "text/html; charset=utf-8"},
            )

        async with _client(handler) as d:
            with pytest.raises(ParseError):
                await d.search_by_company_inn(INN_CLEAN)

    @pytest.mark.asyncio
    async def test_data_of_wrong_type_raises_parse_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"rowCount": 1, "data": {"ФИО": "..."}}, headers=JSON_HEADERS
            )

        async with _client(handler) as d:
            with pytest.raises(ParseError):
                await d.search_by_company_inn(INN_CLEAN)


class TestValidation:
    @pytest.mark.asyncio
    async def test_invalid_inn_raises_before_request(self):
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("запрос не должен был уйти")

        async with _client(handler) as d:
            with pytest.raises(ValidationError):
                await d.search_by_company_inn("12")

    @pytest.mark.asyncio
    async def test_empty_name_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("запрос не должен был уйти")

        async with _client(handler) as d:
            with pytest.raises(ValidationError):
                await d.search_by_person_name("   ")
