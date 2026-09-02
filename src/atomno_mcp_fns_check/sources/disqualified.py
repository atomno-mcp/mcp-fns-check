"""Async-клиент реестра дисквалифицированных лиц ФНС (service.nalog.ru).

Источник: https://service.nalog.ru/disqualified.html, поиск —
`POST /disqualified-proc.json`. Кода с картинки не требует, отвечает JSON::

    {"rowCount":1,"pageCount":1,"data":[{"ФИО":"...","НаимОрг":"ООО \\"СГ\\"",
     "ИННОрг":"2368016435","Должность":"ДИРЕКТОР",
     "ДатаНачДискв":"26.12.2025 00:00:00","ДатаКонДискв":"25.12.2026 00:00:00",
     "КвалификацияТекст":"Ч.5 СТ. 14.25 КОАП РФ", ...}]}

**Ловушка источника, из-за которой этот модуль устроен именно так.** Форма на
странице отдаёт поля `fam`, `nam`, `otch`, но сервер их **не применяет**:
запрос с ними возвращает `rowCount: 8563` — весь реестр целиком, первой
строкой всегда одна и та же запись. Клиент, отправляющий `fam/nam/otch` и
считающий «пришла строка — значит совпадение», получает **ложное совпадение по
любому контрагенту**. Реально фильтрует единственное поле — `query`
(свободный текст: ФИО, ИНН организации, название организации).

Поэтому здесь:

  * запрос уходит только с `query`;
  * ответ проверяется на «фильтр не применился» (`rowCount` равен полному
    объёму реестра при непустом запросе) — это `ParseError`, а не совпадение;
  * каждая строка дополнительно сверяется с тем, что искали.

См. `C:/Users/Velcat/.cursor/rules/service-response-honesty.mdc`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import httpx

from ..errors import (
    ParseError,
    RateLimitedError,
    SourceUnavailableError,
    ValidationError,
)
from ..validators import is_valid_inn

DISQ_BASE_URL = "https://service.nalog.ru"
DISQ_SEARCH_PATH = "/disqualified-proc.json"
DISQ_FORM_URL = "https://service.nalog.ru/disqualified.html"
DEFAULT_TIMEOUT = 15.0
DEFAULT_USER_AGENT = "atomno-mcp-fns-check/0.5 (+https://atomno-mcp.ru)"
DEFAULT_PAGE_SIZE = 25

#: Если при непустом запросе источник вернул столько же строк, сколько всего
#: в реестре, значит фильтр не применился и результату верить нельзя.
_UNFILTERED_MARGIN = 100


@dataclass(slots=True)
class DisqualifiedRecord:
    """Запись реестра дисквалифицированных лиц."""

    full_name: str | None
    birth_date: str | None
    organization_name: str | None
    organization_inn: str | None
    position: str | None
    disqualification_from: str | None
    disqualification_until: str | None
    reason: str | None
    record_number: str | None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_active(self) -> bool:
        """Действует ли дисквалификация сегодня.

        Если дату окончания разобрать не удалось, запись считается действующей:
        занизить риск здесь опаснее, чем завысить.
        """
        until = _parse_ru_date(self.disqualification_until)
        if until is None:
            return True
        return until >= date.today()


def _parse_ru_date(value: str | None) -> date | None:
    if not value or not isinstance(value, str):
        return None
    head = value.strip().split(" ")[0]
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(head, fmt).date()
        except ValueError:
            continue
    return None


def _to_record(row: dict[str, Any]) -> DisqualifiedRecord:
    return DisqualifiedRecord(
        full_name=row.get("ФИО"),
        birth_date=row.get("ДатаРожд"),
        organization_name=row.get("НаимОрг"),
        organization_inn=row.get("ИННОрг"),
        position=row.get("Должность"),
        disqualification_from=row.get("ДатаНачДискв"),
        disqualification_until=row.get("ДатаКонДискв"),
        reason=row.get("КвалификацияТекст"),
        record_number=row.get("НомЗап"),
        raw=row,
    )


def _row_matches(row: dict[str, Any], query: str) -> bool:
    """Проверить, что строка действительно относится к тому, что искали.

    Защита от ситуации «источник проигнорировал фильтр и отдал всё подряд».
    """
    needle = _normalise(query)
    if not needle:
        return False
    for key in ("ФИО", "ИННОрг", "НаимОрг", "ОГРНОрг"):
        value = row.get(key)
        if isinstance(value, str) and needle in _normalise(value):
            return True
    return False


def _normalise(value: str) -> str:
    return " ".join(value.replace("Ё", "Е").replace("ё", "е").upper().split())


class DisqualifiedClient:
    """Тонкая async-обёртка над реестром дисквалифицированных лиц ФНС."""

    def __init__(
        self,
        *,
        base_url: str = DISQ_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        user_agent: str = DEFAULT_USER_AGENT,
        client: httpx.AsyncClient | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._user_agent = user_agent
        self._owns_client = client is None
        self._client = client
        self._page_size = page_size

    async def __aenter__(self) -> "DisqualifiedClient":
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                headers={
                    "User-Agent": self._user_agent,
                    "Accept": "application/json",
                    "Referer": DISQ_FORM_URL,
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
            raise RuntimeError(
                "DisqualifiedClient is not initialised. Use 'async with DisqualifiedClient()'."
            )
        return self._client

    async def _search(self, query: str) -> list[DisqualifiedRecord]:
        try:
            response = await self.client.post(
                f"{self._base_url}{DISQ_SEARCH_PATH}",
                data={"query": query, "page": "1", "pageSize": str(self._page_size)},
            )
        except httpx.TimeoutException as exc:
            raise SourceUnavailableError(
                "Реестр дисквалифицированных лиц ФНС не ответил вовремя.",
                details={"reason": str(exc)},
            ) from exc
        except httpx.HTTPError as exc:
            raise SourceUnavailableError(
                f"Реестр дисквалифицированных лиц ФНС: ошибка HTTP {exc}",
                details={"reason": str(exc)},
            ) from exc

        if response.status_code == 429:
            raise RateLimitedError(
                "Реестр дисквалифицированных лиц ФНС ответил 429 (лимит запросов).",
                details={"status": 429},
            )
        if response.status_code >= 400:
            raise SourceUnavailableError(
                f"Реестр дисквалифицированных лиц ФНС вернул {response.status_code}.",
                details={"status": response.status_code, "body": response.text[:300]},
            )

        content_type = response.headers.get("content-type", "")
        if "json" not in content_type.lower():
            raise ParseError(
                "Реестр дисквалифицированных лиц ФНС вернул не JSON — вероятно, "
                "изменился адрес или формат поиска.",
                details={"content_type": content_type, "body": response.text[:300]},
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise ParseError(
                "Реестр дисквалифицированных лиц ФНС вернул неразбираемый ответ.",
                details={"body": response.text[:300]},
            ) from exc

        if not isinstance(payload, dict):
            raise ParseError(
                "Реестр дисквалифицированных лиц ФНС вернул неожиданную структуру.",
                details={"body": response.text[:300]},
            )
        if payload.get("captchaRequired"):
            raise SourceUnavailableError(
                "Реестр дисквалифицированных лиц ФНС требует код с картинки — "
                "автоматическая проверка невозможна.",
                details={"reason": "captcha_required"},
            )

        rows = payload.get("data")
        if rows is None:
            rows = []
        if not isinstance(rows, list):
            raise ParseError(
                "Реестр дисквалифицированных лиц ФНС: поле 'data' не является списком.",
                details={"payload_keys": sorted(payload.keys())},
            )

        row_count = payload.get("rowCount")
        total = _total_records(rows)
        if (
            isinstance(row_count, int)
            and total is not None
            and row_count >= total - _UNFILTERED_MARGIN
            and len(rows) > 1
        ):
            raise ParseError(
                "Реестр дисквалифицированных лиц ФНС проигнорировал условие поиска "
                "и вернул весь реестр целиком. Ответу верить нельзя: проверка "
                "не выполнена.",
                details={
                    "reason": "filter_not_applied",
                    "row_count": row_count,
                    "registry_total": total,
                    "query": query,
                },
            )

        matched = [row for row in rows if isinstance(row, dict) and _row_matches(row, query)]
        return [_to_record(row) for row in matched]

    async def search_by_company_inn(self, inn: str) -> list[DisqualifiedRecord]:
        """Дисквалифицированные лица в управлении организации по её ИНН."""
        if not is_valid_inn(inn):
            raise ValidationError(f"Невалидный ИНН '{inn}'", details={"input": inn})
        return await self._search(inn)

    async def search_by_person_name(self, full_name: str) -> list[DisqualifiedRecord]:
        """Поиск по ФИО руководителя. Совпадение по ФИО — приблизительное."""
        name = (full_name or "").strip()
        if not name:
            raise ValidationError("Пустое ФИО для поиска", details={"input": full_name})
        return await self._search(name)


def _total_records(rows: list[Any]) -> int | None:
    """Полный объём реестра — источник кладёт его в поле `КолЗап` каждой строки."""
    for row in rows:
        if isinstance(row, dict):
            value = row.get("КолЗап")
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.isdigit():
                return int(value)
    return None
