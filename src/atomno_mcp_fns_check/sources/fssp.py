"""Async-клиент Банка данных исполнительных производств ФССП.

Актуальное состояние источника (проверено 02.08.2026 живыми запросами):

  * `https://api-ip.fssp.gov.ru/api/v1.0/...` — официальный API **закрыт**:
    отвечает `HTTP 410` HTML-страницей «Доступ запрещён».
  * `https://fssp.gov.ru/_search` — адрес, который использовался раньше,
    отдаёт обычную HTML-страницу сайта (`HTTP 200`, ~2 КБ разметки).
    Клиент пытался разобрать её как JSON и падал с невнятной ошибкой.
  * Актуальный адрес поиска берётся из формы на `fssp.gov.ru/iss/ip`::

        <form method="get" action="https://is-go.fssp.gov.ru/ajax_search" ...>

    Параметры — «массивные»: `is[variant]=2` (юр.лицо), `is[inn]=<ИНН>`.
  * Ответ приходит как JSON/JSONP (`({"data": "<html>"})`) с
    `content-type: text/javascript`. В поле `data` — **всплывающее окно
    с кодом с картинки**: `id="captcha-popup"`, `<h2>Введите код с
    картинки:</h2>`, `<img src="data:image/png;base64,...">`.
  * Второй вариант ответа (тот же `HTTP 200`) — текст «Внутренняя ошибка»
    внутри `data`. Это тоже отказ источника, а не «производств нет»,
    поэтому обрабатывается как `SourceUnavailableError`.

То есть публичный поиск ФССП закрыт кодом с картинки. Разгадывать код
запрещено (см. постановку задачи и `service-response-honesty.mdc`), поэтому
клиент в этом случае честно поднимает `SourceUnavailableError` с причиной
`captcha_required` — проверка попадает в «не проверено», а не в «чисто».

Разбор ответа сделан устойчивым: если структура снова изменится, клиент
поднимает `ParseError` с фактическим куском тела и content-type, а не
пытается «додумать» результат.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..errors import (
    ParseError,
    RateLimitedError,
    SourceUnavailableError,
    ValidationError,
)
from ..validators import is_valid_inn

FSSP_BASE_URL = "https://is-go.fssp.gov.ru"
FSSP_SEARCH_PATH = "/ajax_search"
#: Страница формы — нужна как Referer, ФССП отвергает запросы без неё.
FSSP_FORM_URL = "https://fssp.gov.ru/iss/ip"
#: Вариант поиска «Юридическое лицо» в форме ФССП (`is[variant]`).
FSSP_VARIANT_LEGAL_ENTITY = 2
#: Вариант поиска «ИНН юридического лица».
FSSP_VARIANT_INN = 5

DEFAULT_TIMEOUT = 12.0
DEFAULT_USER_AGENT = "atomno-mcp-fns-check/0.5 (+https://atomno-mcp.ru)"

_CAPTCHA_MARKERS = (
    "captcha-popup",
    "введите код с картинки",
    "ncapcha",
)
_EMPTY_MARKERS = (
    "ничего не найдено",
    "по вашему запросу ничего",
    "не найдено ни одного",
)
#: ФССП иногда отвечает `HTTP 200` с текстом ошибки внутри `data`.
#: Это НЕ «производств нет» — это отказ источника.
_SOURCE_ERROR_MARKERS = (
    "внутренняя ошибка",
    "ошибка сервера",
    "сервис временно недоступен",
    "техническ",
)
_TAG_RE = re.compile(r"<[^>]+>")
_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.I | re.S)
_TD_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.I | re.S)
_IP_NUMBER_RE = re.compile(r"\d+/\d+/\d+(?:-ИП|/\d+-ИП)?", re.U)
_AMOUNT_RE = re.compile(r"(\d[\d\s\u00a0]*(?:[.,]\d{1,2})?)\s*(?:руб)", re.I | re.U)
#: ФССП оборачивает ответ либо в `({...});`, либо в `имяФункции({...});`
#: — когда запрос ушёл с параметром `callback`.
_JSONP_RE = re.compile(r"^\s*[A-Za-z_$][\w$.]*\s*\(\s*(\{.*\})\s*\)\s*;?\s*$|^\s*\(?\s*(\{.*\})\s*\)?\s*;?\s*$", re.S)


@dataclass(slots=True)
class EnforcementProceeding:
    """Одно исполнительное производство по контрагенту."""

    case_number: str | None
    debt_amount_rub: float | None
    status: str | None
    subject: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        if not self.status:
            return True
        s = self.status.lower()
        return "оконч" not in s and "прекр" not in s and "заверш" not in s


def _strip_tags(html: str) -> str:
    text = _TAG_RE.sub(" ", html)
    text = text.replace("&nbsp;", " ").replace("&quot;", '"').replace("&amp;", "&")
    return re.sub(r"\s+", " ", text).strip()


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        cleaned = v.replace("\xa0", "").replace(" ", "").replace(",", ".").strip()
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def unwrap_jsonp(body: str) -> dict[str, Any]:
    """Развернуть ответ ФССП: чистый JSON либо JSONP-обёртка `({...})`.

    Поднимает `ParseError`, если тело не разворачивается — без «тихих»
    подстановок пустого результата.
    """
    text = body.strip()
    try:
        payload = json.loads(text)
    except ValueError:
        match = _JSONP_RE.match(text)
        if match is None:
            raise ParseError(
                "ФССП вернул тело, которое не является ни JSON, ни JSONP.",
                details={"body": text[:400]},
            ) from None
        inner = match.group(1) or match.group(2)
        try:
            payload = json.loads(inner)
        except ValueError as exc:
            raise ParseError(
                "ФССП вернул повреждённый JSONP.",
                details={"body": text[:400]},
            ) from exc
    if not isinstance(payload, dict):
        raise ParseError(
            "ФССП вернул JSON неожиданного типа (ожидался объект).",
            details={"type": type(payload).__name__, "body": text[:400]},
        )
    return payload


def parse_result_html(html: str) -> list[EnforcementProceeding]:
    """Разобрать таблицу результатов ФССП из HTML-фрагмента.

    Возвращает пустой список **только** если источник явно сообщил
    «ничего не найдено». Если таблицы нет и явного «пусто» нет — это
    `ParseError`: мы не знаем, что пришло, и не имеем права молчать.
    """
    low = html.lower()
    if any(marker in low for marker in _EMPTY_MARKERS):
        return []
    if "<t" not in low:
        raise ParseError(
            "ФССП вернул ответ без таблицы результатов и без явного "
            "«ничего не найдено» — структура ответа изменилась.",
            details={"body": _strip_tags(html)[:400]},
        )

    rows = _TR_RE.findall(html)
    proceedings: list[EnforcementProceeding] = []
    for row_html in rows:
        cells = [_strip_tags(c) for c in _TD_RE.findall(row_html)]
        joined = " | ".join(cells)
        number_match = _IP_NUMBER_RE.search(joined)
        if number_match is None:
            # Строка заголовка или разметочный мусор.
            continue
        amount_match = _AMOUNT_RE.search(joined)
        subject = next((c for c in cells if len(c) > 20 and "ИП" not in c[:6]), None)
        proceedings.append(
            EnforcementProceeding(
                case_number=number_match.group(0),
                debt_amount_rub=_to_float(amount_match.group(1)) if amount_match else None,
                status=None,
                subject=subject,
                raw={"cells": cells},
            )
        )

    if not proceedings:
        raise ParseError(
            "ФССП вернул таблицу, в которой не распознан ни один номер "
            "исполнительного производства — структура ответа изменилась.",
            details={"body": _strip_tags(html)[:400]},
        )
    return proceedings


class FsspClient:
    """Минимальный async-клиент Банка данных ФССП.

    Использование::

        async with FsspClient() as f:
            cases = await f.search_proceedings_by_inn("7707083893")
    """

    def __init__(
        self,
        *,
        base_url: str = FSSP_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        user_agent: str = DEFAULT_USER_AGENT,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._user_agent = user_agent
        self._owns_client = client is None
        self._client = client

    async def __aenter__(self) -> "FsspClient":
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                headers={
                    "User-Agent": self._user_agent,
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": FSSP_FORM_URL,
                },
            )
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("FsspClient is not initialised. Use 'async with FsspClient()'.")
        return self._client

    async def search_proceedings_by_inn(self, inn: str) -> list[EnforcementProceeding]:
        """Найти открытые исполнительные производства по ИНН должника-юрлица.

        Возвращает только открытые (`is_open == True`) производства.
        Пустой список означает «источник ответил, производств нет».
        Если источник требует код с картинки — `SourceUnavailableError`
        с причиной `captcha_required`; агрегатор покажет это как
        «не проверено», а не как «чисто».
        """
        if not is_valid_inn(inn):
            raise ValidationError(f"Невалидный ИНН '{inn}'", details={"input": inn})

        variant = FSSP_VARIANT_LEGAL_ENTITY if len(inn) == 10 else FSSP_VARIANT_INN
        try:
            response = await self.client.get(
                f"{self._base_url}{FSSP_SEARCH_PATH}",
                params={"is[variant]": variant, "is[inn]": inn, "is[extended]": 1},
            )
        except httpx.TimeoutException as exc:
            raise SourceUnavailableError(
                "ФССП не ответил вовремя.",
                details={"reason": str(exc)},
            ) from exc
        except httpx.HTTPError as exc:
            raise SourceUnavailableError(
                f"ФССП HTTP ошибка: {exc}",
                details={"reason": str(exc)},
            ) from exc

        if response.status_code == 429:
            raise RateLimitedError("ФССП ответил 429 (превышен лимит запросов).", details={"status": 429})
        if response.status_code == 503:
            raise RateLimitedError(
                "ФССП ответил 503 — сервис поиска временно ограничивает запросы.",
                details={"status": 503},
            )
        if response.status_code == 410:
            raise SourceUnavailableError(
                "Адрес поиска ФССП закрыт (HTTP 410). Автоматическая проверка "
                "исполнительных производств недоступна.",
                details={"status": 410, "url": str(response.url)},
            )
        if response.status_code >= 500:
            raise SourceUnavailableError(
                f"ФССП вернул {response.status_code}.",
                details={"status": response.status_code},
            )
        if response.status_code >= 400:
            raise SourceUnavailableError(
                f"ФССП вернул {response.status_code}.",
                details={"status": response.status_code, "body": response.text[:300]},
            )

        content_type = response.headers.get("content-type", "")
        body = response.text
        if "html" in content_type.lower() and not body.lstrip().startswith(("{", "(")):
            # Ровно тот случай, который ломал прошлую версию: вместо данных
            # прилетела обычная веб-страница.
            raise ParseError(
                "ФССП отдал HTML-страницу вместо данных поиска — адрес "
                "или формат ответа изменился.",
                details={"content_type": content_type, "body": body[:300]},
            )

        payload = unwrap_jsonp(body)
        html = payload.get("data")
        if not isinstance(html, str):
            raise ParseError(
                "В ответе ФССП нет поля 'data' с результатом поиска.",
                details={"payload_keys": sorted(payload.keys()), "body": body[:300]},
            )

        low = html.lower()
        if any(marker in low for marker in _CAPTCHA_MARKERS):
            raise SourceUnavailableError(
                "ФССП требует ввод кода с картинки — автоматическая проверка "
                "исполнительных производств невозможна. Проверьте контрагента "
                "вручную на fssp.gov.ru/iss/ip.",
                details={"reason": "captcha_required", "url": str(response.url)},
            )

        reported_error = payload.get("err") or payload.get("e")
        if isinstance(reported_error, str) and reported_error.strip():
            raise SourceUnavailableError(
                f"ФССП вернул ошибку: {reported_error.strip()}",
                details={"reason": "source_error", "err": reported_error.strip()},
            )
        # Слова про ошибку ищем только там, где таблицы результатов нет: иначе
        # должник с названием вроде «Технические системы» превратил бы
        # настоящий ответ в отказ источника.
        if "<t" not in low and any(marker in low for marker in _SOURCE_ERROR_MARKERS):
            raise SourceUnavailableError(
                "ФССП ответил внутренней ошибкой вместо результата поиска — "
                "проверить исполнительные производства не удалось.",
                details={
                    "reason": "source_internal_error",
                    "body": _strip_tags(html)[:300],
                },
            )

        cases = parse_result_html(html)
        return [c for c in cases if c.is_open]
