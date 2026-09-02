"""Тесты клиента «Прозрачного бизнеса» (pb.nalog.ru).

Все ответы в тестах — **настоящие**, снятые живыми запросами 02.08.2026
и сохранённые в `tests/fixtures/`:

  * `pb_step1_token.json`        — ответ первого шага (выдача пропуска);
  * `pb_step2_sber.json`         — ответ второго шага по ПАО СБЕРБАНК;
  * `pb_error_wrong_field.json`  — `HTTP 400` на старое (ошибочное) поле `query=`.

Ничего не выдумываем: прошлая версия тестов проверяла структуру, которой
источник никогда не отдавал, и была зелёной на собственной ошибке.
"""

from __future__ import annotations

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
from atomno_mcp_fns_check.sources.pb_fns import (
    PB_UNSUPPORTED_INDICATORS,
    PbFnsClient,
)

FIXTURES = Path(__file__).parent / "fixtures"
INN_SBER = "7707083893"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


STEP1 = _load("pb_step1_token.json")
STEP2_SBER = _load("pb_step2_sber.json")
ERROR_WRONG_FIELD = _load("pb_error_wrong_field.json")


def _client(handler) -> PbFnsClient:
    transport = httpx.MockTransport(handler)
    return PbFnsClient(
        client=httpx.AsyncClient(transport=transport, base_url="https://pb.nalog.ru"),
        poll_delay=0.0,
    )


class TestHappyPath:
    """Успешный сценарий: два шага, карточка организации приходит."""

    @pytest.mark.asyncio
    async def test_two_step_exchange_returns_company_card(self):
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            form = dict(httpx.QueryParams(request.content.decode()))
            seen.append(form)
            if form.get("method") == "get-response":
                return httpx.Response(200, json=STEP2_SBER)
            return httpx.Response(200, json=STEP1)

        async with _client(handler) as pb:
            card = await pb.get_company_card(INN_SBER)

        assert card is not None
        assert card.inn == INN_SBER
        assert card.ogrn == "1027700132195"
        assert card.name_short == "ПАО СБЕРБАНК"
        assert card.status_ru == "Действующая организация"
        assert card.region == "ГОРОД ФЕДЕРАЛЬНОГО ЗНАЧЕНИЯ МОСКВА"
        assert card.okved == "64.19"

        # Первый шаг ушёл с правильным именем поля — ровно та починка,
        # ради которой всё затевалось.
        assert seen[0]["queryUl"] == INN_SBER
        assert seen[0]["mode"] == "search-ul"
        assert "query" not in seen[0]
        # Второй шаг — по пропуску из первого.
        assert seen[1]["id"] == STEP1["id"]
        assert seen[1]["method"] == "get-response"

    @pytest.mark.asyncio
    async def test_individual_entrepreneur_uses_query_ip(self):
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            form = dict(httpx.QueryParams(request.content.decode()))
            seen.append(form)
            if form.get("method") == "get-response":
                return httpx.Response(200, json={"ip": {"data": [], "rowCount": 0.0}})
            return httpx.Response(200, json=STEP1)

        async with _client(handler) as pb:
            await pb.get_company_card("500100732259")

        assert seen[0]["queryIp"] == "500100732259"
        assert seen[0]["mode"] == "search-ip"

    @pytest.mark.asyncio
    async def test_null_on_first_poll_then_result(self):
        """Источник реально отдаёт `null`, пока считает — это не ошибка."""
        calls = {"get_response": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            form = dict(httpx.QueryParams(request.content.decode()))
            if form.get("method") == "get-response":
                calls["get_response"] += 1
                if calls["get_response"] < 3:
                    return httpx.Response(
                        200,
                        content=b"null",
                        headers={"content-type": "application/json"},
                    )
                return httpx.Response(200, json=STEP2_SBER)
            return httpx.Response(200, json=STEP1)

        async with _client(handler) as pb:
            card = await pb.get_company_card(INN_SBER)

        assert card is not None
        assert calls["get_response"] == 3

    @pytest.mark.asyncio
    async def test_card_has_no_tax_or_reporting_indicators(self):
        """Ключевой факт: в карточке НЕТ долгов и несданной отчётности.

        Из-за неверного толкования этого ответа сервис раньше писал
        «нарушений не найдено» там, где источник просто молчит.
        """
        def handler(request: httpx.Request) -> httpx.Response:
            form = dict(httpx.QueryParams(request.content.decode()))
            if form.get("method") == "get-response":
                return httpx.Response(200, json=STEP2_SBER)
            return httpx.Response(200, json=STEP1)

        async with _client(handler) as pb:
            card = await pb.get_company_card(INN_SBER)

        assert card is not None
        keys = set(card.raw)
        forbidden = {"debt", "tax_debt", "hasdebt", "nodecl", "noreport", "reporting"}
        assert not (keys & forbidden), f"источник неожиданно отдал: {keys & forbidden}"
        assert PB_UNSUPPORTED_INDICATORS == ("tax_debts", "no_reporting")


class TestNotFound:
    """Источник ответил, но записей нет — это не ошибка и не «чисто по рискам»."""

    @pytest.mark.asyncio
    async def test_empty_data_returns_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            form = dict(httpx.QueryParams(request.content.decode()))
            if form.get("method") == "get-response":
                return httpx.Response(
                    200,
                    json={"ul": {"data": [], "rowCount": 0.0, "hasMore": False}},
                )
            return httpx.Response(200, json=STEP1)

        async with _client(handler) as pb:
            assert await pb.get_company_card("7710137066") is None


class TestErrorResponses:
    """Ответ источника с ошибкой не должен превращаться в пустой результат."""

    @pytest.mark.asyncio
    async def test_wrong_field_400_raises(self):
        """Настоящий `HTTP 400`, который источник отдавал на старое поле `query=`."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json=ERROR_WRONG_FIELD)

        async with _client(handler) as pb:
            with pytest.raises(SourceUnavailableError) as exc:
                await pb.get_company_card(INN_SBER)

        assert "400" in str(exc.value)
        # Источник прямым текстом называет правильное имя поля.
        assert "queryUl" in ERROR_WRONG_FIELD["ERRORS"]

    @pytest.mark.asyncio
    async def test_rate_limit_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, text="")

        async with _client(handler) as pb:
            with pytest.raises(RateLimitedError):
                await pb.get_company_card(INN_SBER)

    @pytest.mark.asyncio
    async def test_server_error_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(502, text="Bad Gateway")

        async with _client(handler) as pb:
            with pytest.raises(SourceUnavailableError):
                await pb.get_company_card(INN_SBER)

    @pytest.mark.asyncio
    async def test_captcha_required_raises(self):
        """Капчу не обходим — честно сообщаем, что проверка невозможна."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"id": None, "captchaRequired": True})

        async with _client(handler) as pb:
            with pytest.raises(SourceUnavailableError) as exc:
                await pb.get_company_card(INN_SBER)

        assert exc.value.details.get("reason") == "captcha_required"

    @pytest.mark.asyncio
    async def test_timeout_raises_source_unavailable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        async with _client(handler) as pb:
            with pytest.raises(SourceUnavailableError):
                await pb.get_company_card(INN_SBER)


class TestUnclearResponses:
    """Пустой / непонятный ответ — ошибка разбора, а не «ничего не найдено»."""

    @pytest.mark.asyncio
    async def test_html_instead_of_json_raises_parse_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html><body>Технические работы</body></html>")

        async with _client(handler) as pb:
            with pytest.raises(ParseError):
                await pb.get_company_card(INN_SBER)

    @pytest.mark.asyncio
    async def test_missing_token_raises_parse_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"captchaRequired": False})

        async with _client(handler) as pb:
            with pytest.raises(ParseError):
                await pb.get_company_card(INN_SBER)

    @pytest.mark.asyncio
    async def test_endless_null_raises_source_unavailable(self):
        """Если источник вечно отдаёт `null` — это отказ, а не пустой результат."""
        def handler(request: httpx.Request) -> httpx.Response:
            form = dict(httpx.QueryParams(request.content.decode()))
            if form.get("method") == "get-response":
                return httpx.Response(
                        200,
                        content=b"null",
                        headers={"content-type": "application/json"},
                    )
            return httpx.Response(200, json=STEP1)

        client = PbFnsClient(
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(handler), base_url="https://pb.nalog.ru"
            ),
            poll_attempts=3,
            poll_delay=0.0,
        )
        async with client as pb:
            with pytest.raises(SourceUnavailableError) as exc:
                await pb.get_company_card(INN_SBER)

        assert exc.value.details.get("reason") == "empty_response"


class TestValidation:
    @pytest.mark.asyncio
    async def test_invalid_inn_raises_before_request(self):
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("запрос не должен был уйти")

        async with _client(handler) as pb:
            with pytest.raises(ValidationError):
                await pb.get_company_card("123")
