"""Async-клиент Единого федерального реестра сведений о банкротстве (ЕФРСБ).

Источник: bankrot.fedresurs.ru.

Публичные backend-эндпоинты SPA-сайта:
    * `/backend/companies/?searchString={inn}` — юрлица и ИП по ИНН.
    * `/backend/bankrupts/?searchString={fio}&birthDate={YYYY-MM-DD}` — физлица.

HTML-fallback: `/bankrupts/?searchString=...` — если JSON недоступен (antibot).

Точная схема ответа не гарантирована — все парсеры толерантны к отсутствию ключей.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

import httpx

from ..errors import (
    ParseError,
    RateLimitedError,
    SourceUnavailableError,
    ValidationError,
)
from ..person import normalize_fio, normalize_snils, parse_birth_date
from ..validators import is_valid_inn

EFRSB_BASE_URL = "https://bankrot.fedresurs.ru"
EFRSB_COMPANIES_PATH = "/backend/companies/"
EFRSB_BANKRUPTS_PATH = "/backend/bankrupts/"
EFRSB_BANKRUPTS_HTML_PATH = "/bankrupts/"
# Backward-compatible alias for tests/imports.
EFRSB_SEARCH_PATH = EFRSB_COMPANIES_PATH
DEFAULT_TIMEOUT = 10.0
DEFAULT_USER_AGENT = "atomno-mcp-fns-check/0.1 (+https://example.com/contact)"

MatchConfidence = Literal["exact", "possible", "unknown"]

_STAGE_MAP_RU_TO_EN: dict[str, str] = {
    "наблюдение": "observation",
    "финансовое оздоровление": "financial_recovery",
    "внешнее управление": "external_management",
    "конкурсное производство": "bankruptcy_proceedings",
    "мировое соглашение": "settlement",
    "реализация имущества": "bankruptcy_proceedings",
    "реструктуризация долгов": "financial_recovery",
}

_CASE_NUMBER_RE = re.compile(r"А\d{2}-\d+/\d{4}", re.IGNORECASE)


@dataclass(slots=True)
class BankruptcyCase:
    """Одно дело о банкротстве по контрагенту."""

    case_number: str | None
    stage_ru: str | None
    stage: str | None
    is_active: bool
    raw: dict[str, Any]


@dataclass(slots=True)
class IndividualBankruptcyCase:
    """Дело о банкротстве физического лица."""

    case_number: str | None
    stage_ru: str | None
    stage: str | None
    is_active: bool
    debtor_fio: str | None
    birth_date: date | None
    inn: str | None
    snils: str | None
    region: str | None
    match_confidence: MatchConfidence
    raw: dict[str, Any]


def _normalise_stage(stage_ru: str | None) -> str | None:
    if not stage_ru:
        return None
    key = stage_ru.lower().strip()
    for ru, code in _STAGE_MAP_RU_TO_EN.items():
        if ru in key:
            return code
    return None


def _coalesce_str(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _parse_birth_from_item(item: dict[str, Any]) -> date | None:
    raw = _coalesce_str(
        item.get("birthDate"),
        item.get("birthdate"),
        item.get("dateOfBirth"),
        item.get("BankruptBirthdate"),
    )
    if not raw:
        return None
    try:
        return parse_birth_date(raw[:10] if "T" in raw else raw)
    except ValidationError:
        return None


def _compose_fio(item: dict[str, Any]) -> str | None:
    direct = _coalesce_str(item.get("fio"), item.get("name"), item.get("fullName"))
    if direct:
        return direct
    last = _coalesce_str(item.get("lastName"), item.get("LastName"))
    first = _coalesce_str(item.get("firstName"), item.get("FirstName"))
    middle = _coalesce_str(item.get("middleName"), item.get("MiddleName"))
    parts = [p for p in (last, first, middle) if p]
    return " ".join(parts) if parts else None


def _is_active_stage(stage_ru: str | None, explicit: Any) -> bool:
    if explicit is not None:
        return bool(explicit)
    if stage_ru and "завершен" in stage_ru.lower():
        return False
    return bool(stage_ru)


def _parse_company(item: dict[str, Any]) -> BankruptcyCase:
    case_number = _coalesce_str(
        item.get("caseNumber"),
        item.get("case_number"),
        item.get("number"),
    )
    stage_ru = _coalesce_str(
        item.get("stageName"),
        item.get("stage"),
        item.get("currentStageName"),
        item.get("procedureType"),
    )
    is_active = _is_active_stage(
        stage_ru,
        item.get("isActive", item.get("active")),
    )
    return BankruptcyCase(
        case_number=case_number,
        stage_ru=stage_ru,
        stage=_normalise_stage(stage_ru),
        is_active=is_active,
        raw=item,
    )


def _parse_individual(
    item: dict[str, Any],
    *,
    query_birth_date: date | None = None,
    query_snils: str | None = None,
) -> IndividualBankruptcyCase:
    case_number = _coalesce_str(
        item.get("caseNumber"),
        item.get("case_number"),
        item.get("number"),
        item.get("caseNum"),
    )
    stage_ru = _coalesce_str(
        item.get("stageName"),
        item.get("stage"),
        item.get("currentStageName"),
        item.get("procedureType"),
        item.get("status"),
    )
    is_active = _is_active_stage(
        stage_ru,
        item.get("isActive", item.get("active")),
    )
    debtor_fio = _compose_fio(item)
    birth_date = _parse_birth_from_item(item)
    inn = _coalesce_str(item.get("inn"), item.get("INN"))
    snils_raw = _coalesce_str(item.get("snils"), item.get("SNILS"))
    snils = None
    if snils_raw:
        try:
            snils = normalize_snils(snils_raw)
        except ValidationError:
            snils = "".join(ch for ch in snils_raw if ch.isdigit()) or None
    region = _coalesce_str(item.get("regionName"), item.get("region"), item.get("address"))

    match_confidence: MatchConfidence = "unknown"
    if query_birth_date and birth_date == query_birth_date:
        match_confidence = "exact"
    elif query_snils and snils and query_snils == snils:
        match_confidence = "exact"
    elif debtor_fio:
        match_confidence = "possible"

    return IndividualBankruptcyCase(
        case_number=case_number,
        stage_ru=stage_ru,
        stage=_normalise_stage(stage_ru),
        is_active=is_active,
        debtor_fio=debtor_fio,
        birth_date=birth_date,
        inn=inn,
        snils=snils,
        region=region,
        match_confidence=match_confidence,
        raw=item,
    )


def _extract_items(payload: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        items = (
            payload.get("pageData")
            or payload.get("items")
            or payload.get("data")
            or payload.get("foundRecords")
            or []
        )
    elif isinstance(payload, list):
        items = payload
    return [item for item in items if isinstance(item, dict)]


def _extract_page_data_from_html(html: str) -> list[dict[str, Any]]:
    marker = '"pageData"'
    idx = html.find(marker)
    if idx == -1:
        return _extract_page_data_heuristic(html)

    colon = html.find(":", idx)
    if colon == -1:
        return _extract_page_data_heuristic(html)

    start = html.find("[", colon)
    if start == -1:
        start = html.find("{", colon)
    if start == -1:
        return _extract_page_data_heuristic(html)

    opener = html[start]
    closer = "]" if opener == "[" else "}"
    depth = 0
    end = start
    for pos in range(start, len(html)):
        ch = html[pos]
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                end = pos + 1
                break

    try:
        parsed = json.loads(html[start:end])
    except json.JSONDecodeError:
        return _extract_page_data_heuristic(html)

    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    if isinstance(parsed, dict):
        return _extract_items(parsed)
    return []


def _extract_page_data_heuristic(html: str) -> list[dict[str, Any]]:
    """Минимальный fallback: номера дел + текст стадии из HTML."""
    cases = sorted(set(_CASE_NUMBER_RE.findall(html)))
    if not cases:
        return []
    items: list[dict[str, Any]] = []
    for case_number in cases:
        stage_ru = None
        window = html[html.find(case_number) : html.find(case_number) + 400]
        for stage_key in _STAGE_MAP_RU_TO_EN:
            if stage_key in window.lower():
                stage_ru = stage_key.title()
                break
        items.append(
            {
                "caseNumber": case_number,
                "stageName": stage_ru,
                "isActive": True,
            }
        )
    return items


class EfrsbClient:
    """Минимальный async-клиент над публичным backend ЕФРСБ."""

    def __init__(
        self,
        *,
        base_url: str = EFRSB_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        user_agent: str = DEFAULT_USER_AGENT,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._user_agent = user_agent
        self._owns_client = client is None
        self._client = client

    async def __aenter__(self) -> "EfrsbClient":
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                headers={
                    "User-Agent": self._user_agent,
                    "Accept": "application/json, text/html;q=0.9,*/*;q=0.8",
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
            raise RuntimeError("EfrsbClient is not initialised. Use 'async with EfrsbClient()'.")
        return self._client

    async def _request(
        self,
        path: str,
        *,
        params: dict[str, Any],
        accept_json: bool = True,
    ) -> httpx.Response:
        try:
            headers = {"Accept": "application/json"} if accept_json else {}
            return await self.client.get(
                f"{self._base_url}{path}",
                params=params,
                headers=headers,
            )
        except httpx.TimeoutException as exc:
            raise SourceUnavailableError(
                "ЕФРСБ не ответил вовремя.",
                details={"reason": str(exc)},
            ) from exc
        except httpx.HTTPError as exc:
            raise SourceUnavailableError(
                f"ЕФРСБ HTTP ошибка: {exc}",
                details={"reason": str(exc)},
            ) from exc

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code == 429:
            raise RateLimitedError(
                "ЕФРСБ ответил 429 (rate-limit).",
                details={"status": 429},
            )
        if response.status_code == 403:
            raise SourceUnavailableError(
                "ЕФРСБ заблокировал запрос (403). Скорее всего сработала antibot-защита.",
                details={"status": 403},
            )
        if response.status_code >= 500:
            raise SourceUnavailableError(
                f"ЕФРСБ вернул {response.status_code}.",
                details={"status": response.status_code},
            )

    def _parse_json_response(self, response: httpx.Response) -> Any:
        ctype = response.headers.get("content-type", "").lower()
        if "json" not in ctype and response.text.lstrip().startswith("<"):
            raise ParseError(
                "ЕФРСБ вернул HTML вместо JSON — вероятно antibot-страница.",
                details={"content_type": ctype, "body": response.text[:300]},
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ParseError(
                "Не удалось декодировать JSON-ответ ЕФРСБ.",
                details={"body": response.text[:300]},
            ) from exc

    async def search_active_by_inn(self, inn: str) -> list[BankruptcyCase]:
        """Найти активные дела о банкротстве по ИНН контрагента."""
        if not is_valid_inn(inn):
            raise ValidationError(f"Невалидный ИНН '{inn}'", details={"input": inn})

        response = await self._request(
            EFRSB_COMPANIES_PATH,
            params={"searchString": inn, "limit": 10, "offset": 0},
        )
        if response.status_code == 404:
            return []
        self._raise_for_status(response)
        if response.status_code >= 400:
            raise SourceUnavailableError(
                f"ЕФРСБ вернул {response.status_code}.",
                details={"status": response.status_code, "body": response.text[:300]},
            )

        payload = self._parse_json_response(response)
        cases = [_parse_company(item) for item in _extract_items(payload)]
        return [case for case in cases if case.is_active]

    async def search_active_by_person(
        self,
        fio: str,
        birth_date: str | date,
        *,
        snils: str | None = None,
    ) -> tuple[list[IndividualBankruptcyCase], Literal["efrsb_json", "efrsb_html"]]:
        """Найти активные дела о банкротстве физлица по ФИО и дате рождения."""
        normalized_fio = normalize_fio(fio)
        parsed_birth = parse_birth_date(birth_date)
        normalized_snils = normalize_snils(snils)

        params = {
            "searchString": normalized_fio,
            "birthDate": parsed_birth.isoformat(),
            "limit": 15,
            "offset": 0,
        }

        response = await self._request(EFRSB_BANKRUPTS_PATH, params=params)
        source: Literal["efrsb_json", "efrsb_html"] = "efrsb_json"
        items: list[dict[str, Any]] = []

        if response.status_code == 404:
            items = []
        elif response.status_code >= 400:
            self._raise_for_status(response)
        else:
            try:
                payload = self._parse_json_response(response)
                items = _extract_items(payload)
            except ParseError:
                items = []

        if not items:
            html_items = await self._search_person_html_fallback(
                normalized_fio,
                parsed_birth,
            )
            if html_items:
                items = html_items
                source = "efrsb_html"

        cases = [
            _parse_individual(
                item,
                query_birth_date=parsed_birth,
                query_snils=normalized_snils,
            )
            for item in items
        ]

        if normalized_snils or parsed_birth:
            filtered: list[IndividualBankruptcyCase] = []
            for case in cases:
                if normalized_snils and case.snils and case.snils != normalized_snils:
                    continue
                if (
                    parsed_birth
                    and case.birth_date
                    and case.birth_date != parsed_birth
                ):
                    continue
                filtered.append(case)
            if filtered:
                cases = filtered

        active = [case for case in cases if case.is_active]
        return active, source

    async def _search_person_html_fallback(
        self,
        fio: str,
        birth_date: date,
    ) -> list[dict[str, Any]]:
        response = await self._request(
            EFRSB_BANKRUPTS_HTML_PATH,
            params={
                "searchString": fio,
                "birthDate": birth_date.isoformat(),
            },
            accept_json=False,
        )
        if response.status_code == 404:
            return []
        self._raise_for_status(response)
        if response.status_code >= 400:
            raise SourceUnavailableError(
                f"ЕФРСБ HTML fallback вернул {response.status_code}.",
                details={"status": response.status_code},
            )
        return _extract_page_data_from_html(response.text)
