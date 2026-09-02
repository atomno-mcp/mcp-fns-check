"""Тесты клиента Банка данных исполнительных производств ФССП.

Ответы источника — настоящие, сняты живыми запросами 02.08.2026:

  * `fssp_captcha_jsonp.txt`        — `HTTP 200`, JSONP, внутри всплывающее
    окно с кодом с картинки (`captcha-popup`, `ncapcha`). Встроенная картинка
    и ~190 КБ стилей обрезаны, структура и маркеры оставлены как есть.
  * `fssp_internal_error_jsonp.txt` — `HTTP 200`, JSONP, внутри «Внутренняя
    ошибка». Это отказ источника, а не «производств нет».

Старый официальный интерфейс `api-ip.fssp.gov.ru` отвечает `HTTP 410`
HTML-страницей «Доступ запрещён» — проверено тем же прогоном.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from atomno_mcp_fns_check.errors import (
    ParseError,
    RateLimitedError,
    SourceUnavailableError,
    ValidationError,
)
from atomno_mcp_fns_check.sources.fssp import (
    FsspClient,
    parse_result_html,
    unwrap_jsonp,
)

FIXTURES = Path(__file__).parent / "fixtures"
INN_SBER = "7707083893"

CAPTCHA_JSONP = (FIXTURES / "fssp_captcha_jsonp.txt").read_text(encoding="utf-8")
INTERNAL_ERROR_JSONP = (FIXTURES / "fssp_internal_error_jsonp.txt").read_text(encoding="utf-8")

#: Так выглядит таблица результатов в разметке ФССП (структура формы `iss`).
RESULT_TABLE_HTML = (
    '<div class="iss"><table class="results"><tbody>'
    "<tr><th>№ ИП</th><th>Должник</th><th>Сумма</th></tr>"
    "<tr>"
    "<td>12345/24/77001-ИП от 01.02.2024</td>"
    '<td>ООО "РОМАШКА", ИНН 7707083893, г. Москва, ул. Ленина, д. 1</td>'
    "<td>Иные взыскания имущественного характера: 1 234 567,89 руб.</td>"
    "</tr>"
    "</tbody></table></div>"
)
EMPTY_RESULT_HTML = (
    '<div class="iss"><div class="results">'
    '<div class="empty">По вашему запросу ничего не найдено</div>'
    "</div></div>"
)


def _client(handler, **kwargs) -> FsspClient:
    return FsspClient(
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://is-go.fssp.gov.ru",
        ),
        **kwargs,
    )


def _jsonp(html: str) -> str:
    import json

    return "(" + json.dumps({"data": html, "err": "", "e": ""}, ensure_ascii=False) + ");"


#: Должник, в названии которого есть слово «технические» — проверяем, что оно
#: не принимается за сообщение об ошибке источника.
RESULT_TABLE_TRICKY_DEBTOR_HTML = (
    '<div class="iss"><table class="results"><tbody>'
    "<tr><th>№ ИП</th><th>Должник</th><th>Сумма</th></tr>"
    "<tr>"
    "<td>77001/24/12345-ИП от 03.03.2024</td>"
    '<td>ООО "ТЕХНИЧЕСКИЕ СИСТЕМЫ", ИНН 7707083893, г. Москва</td>'
    "<td>Иные взыскания имущественного характера: 250 000,00 руб.</td>"
    "</tr>"
    "</tbody></table></div>"
)


class TestHappyPath:
    """Источник ответил таблицей — производства разбираются."""

    @pytest.mark.asyncio
    async def test_debtor_name_with_error_like_word_is_still_parsed(self):
        """«Технические системы» в названии должника — не отказ источника."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text=_jsonp(RESULT_TABLE_TRICKY_DEBTOR_HTML),
                headers={"content-type": "text/javascript; charset=utf-8"},
            )

        async with _client(handler) as f:
            cases = await f.search_proceedings_by_inn(INN_SBER)

        assert len(cases) == 1
        assert cases[0].case_number == "77001/24/12345-ИП"
        assert cases[0].debt_amount_rub == pytest.approx(250000.0)

    @pytest.mark.asyncio
    async def test_parses_open_proceedings(self):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(
                200,
                text=_jsonp(RESULT_TABLE_HTML),
                headers={"content-type": "text/javascript; charset=utf-8"},
            )

        async with _client(handler) as f:
            cases = await f.search_proceedings_by_inn(INN_SBER)

        assert len(cases) == 1
        assert cases[0].case_number == "12345/24/77001-ИП"
        assert cases[0].debt_amount_rub == pytest.approx(1234567.89)
        assert cases[0].is_open is True
        # Запрос ушёл на актуальный адрес формы, а не на закрытый api-ip.
        assert "/ajax_search" in seen["url"]
        assert "is%5Binn%5D=7707083893" in seen["url"] or "is[inn]=7707083893" in seen["url"]

    @pytest.mark.asyncio
    async def test_empty_result_is_not_an_error(self):
        """«Ничего не найдено» — источник ответил, производств нет."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text=_jsonp(EMPTY_RESULT_HTML),
                headers={"content-type": "text/javascript; charset=utf-8"},
            )

        async with _client(handler) as f:
            assert await f.search_proceedings_by_inn(INN_SBER) == []


class TestSourceRefusals:
    """Отказы источника обязаны оставаться отказами, а не «чисто»."""

    @pytest.mark.asyncio
    async def test_real_captcha_response_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text=CAPTCHA_JSONP,
                headers={"content-type": "text/javascript; charset=utf-8"},
            )

        async with _client(handler) as f:
            with pytest.raises(SourceUnavailableError) as exc:
                await f.search_proceedings_by_inn(INN_SBER)

        assert exc.value.details.get("reason") == "captcha_required"
        assert "кода с картинки" in str(exc.value)

    @pytest.mark.asyncio
    async def test_real_internal_error_response_raises(self):
        """«Внутренняя ошибка» внутри `HTTP 200` — не пустой результат."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text=INTERNAL_ERROR_JSONP,
                headers={"content-type": "text/javascript; charset=utf-8"},
            )

        async with _client(handler) as f:
            with pytest.raises(SourceUnavailableError) as exc:
                await f.search_proceedings_by_inn(INN_SBER)

        assert exc.value.details.get("reason") == "source_internal_error"

    @pytest.mark.asyncio
    async def test_err_field_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"data": "", "err": "Сервис перегружен", "e": ""},
                headers={"content-type": "text/javascript; charset=utf-8"},
            )

        async with _client(handler) as f:
            with pytest.raises(SourceUnavailableError) as exc:
                await f.search_proceedings_by_inn(INN_SBER)

        assert "Сервис перегружен" in str(exc.value)

    @pytest.mark.asyncio
    async def test_gone_410_raises(self):
        """Закрытый официальный интерфейс отвечает именно так."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                410,
                text="<!DOCTYPE html><html><head><title>Доступ запрещен</title></head></html>",
                headers={"content-type": "text/html; charset=utf-8"},
            )

        async with _client(handler) as f:
            with pytest.raises(SourceUnavailableError) as exc:
                await f.search_proceedings_by_inn(INN_SBER)

        assert exc.value.details.get("status") == 410

    @pytest.mark.asyncio
    async def test_503_is_rate_limited(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="")

        async with _client(handler) as f:
            with pytest.raises(RateLimitedError):
                await f.search_proceedings_by_inn(INN_SBER)

    @pytest.mark.asyncio
    async def test_429_is_rate_limited(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, text="")

        async with _client(handler) as f:
            with pytest.raises(RateLimitedError):
                await f.search_proceedings_by_inn(INN_SBER)

    @pytest.mark.asyncio
    async def test_timeout_raises_source_unavailable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        async with _client(handler) as f:
            with pytest.raises(SourceUnavailableError):
                await f.search_proceedings_by_inn(INN_SBER)


class TestUnclearResponses:
    """Непонятный ответ — явная ошибка разбора, а не «производств нет»."""

    @pytest.mark.asyncio
    async def test_plain_html_page_raises_parse_error(self):
        """Ровно тот случай, который ломал прошлую версию клиента."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text="<!DOCTYPE html><html><body>Банк данных исполнительных производств</body></html>",
                headers={"content-type": "text/html; charset=utf-8"},
            )

        async with _client(handler) as f:
            with pytest.raises(ParseError) as exc:
                await f.search_proceedings_by_inn(INN_SBER)

        assert "HTML" in str(exc.value)

    @pytest.mark.asyncio
    async def test_missing_data_field_raises_parse_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"err": "", "e": ""})

        async with _client(handler) as f:
            with pytest.raises(ParseError):
                await f.search_proceedings_by_inn(INN_SBER)

    @pytest.mark.asyncio
    async def test_unknown_html_without_table_raises_parse_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text=_jsonp('<div class="iss">Раздел переехал</div>'),
                headers={"content-type": "text/javascript; charset=utf-8"},
            )

        async with _client(handler) as f:
            with pytest.raises(ParseError):
                await f.search_proceedings_by_inn(INN_SBER)


class TestParsers:
    def test_unwrap_jsonp_handles_real_captcha_body(self):
        payload = unwrap_jsonp(CAPTCHA_JSONP)
        assert "captcha-popup" in payload["data"]
        assert payload["err"] == ""

    def test_unwrap_jsonp_rejects_garbage(self):
        with pytest.raises(ParseError):
            unwrap_jsonp("<html>не JSON</html>")

    def test_parse_result_html_empty_marker(self):
        assert parse_result_html(EMPTY_RESULT_HTML) == []

    def test_parse_result_html_table_without_numbers_raises(self):
        html = "<table><tr><td>какие-то данные</td></tr></table>"
        with pytest.raises(ParseError):
            parse_result_html(html)


class TestValidation:
    @pytest.mark.asyncio
    async def test_invalid_inn_raises_before_request(self):
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("запрос не должен был уйти")

        async with _client(handler) as f:
            with pytest.raises(ValidationError):
                await f.search_proceedings_by_inn("00")
