"""Async-клиент Единого федерального реестра сведений о банкротстве (ЕФРСБ).

Источник: bankrot.fedresurs.ru. Публичная страница `bankrupts/?searchString={inn}`
формирует HTML с возможностью JSON-эндпоинта `/backend/companies/?searchString={inn}`.
Точная схема ответа не гарантирована (на S2 берём минимальный набор полей и
все парсеры пишем толерантными к отсутствию ключей).

Назначение на S2: проверка наличия активных дел о банкротстве по ИНН для
обогащения `get_legal_status`. Полноценный поиск с пагинацией и историей
дел появится в S4 (как один из 8 риск-флагов в `check_for_red_flags`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from ..errors import (
    ParseError,
    RateLimitedError,
    SourceUnavailableError,
    ValidationError,
)
from ..validators import is_valid_inn

EFRSB_BASE_URL = "https://bankrot.fedresurs.ru"
EFRSB_SEARCH_PATH = "/backend/companies/"
DEFAULT_TIMEOUT = 10.0
DEFAULT_USER_AGENT = "atomno-mcp-fns-check/0.1 (+https://example.com/contact)"


_STAGE_MAP_RU_TO_EN: dict[str, str] = {
    "наблюдение": "observation",
    "финансовое оздоровление": "financial_recovery",
    "внешнее управление": "external_management",
    "конкурсное производство": "bankruptcy_proceedings",
    "мировое соглашение": "settlement",
    "реализация имущества": "bankruptcy_proceedings",
    "реструктуризация долгов": "financial_recovery",
}


@dataclass(slots=True)
class BankruptcyCase:
    """Одно дело о банкротстве по контрагенту."""

    case_number: str | None
    stage_ru: str | None
    stage: str | None
    is_active: bool
    raw: dict[str, Any]


def _normalise_stage(stage_ru: str | None) -> str | None:
    if not stage_ru:
        return None
    key = stage_ru.lower().strip()
    for ru, code in _STAGE_MAP_RU_TO_EN.items():
        if ru in key:
            return code
    return None


def _parse_company(item: dict[str, Any]) -> BankruptcyCase:
    case_number = (
        item.get("caseNumber")
        or item.get("case_number")
        or item.get("number")
    )
    stage_ru = (
        item.get("stageName")
        or item.get("stage")
        or item.get("currentStageName")
    )
    is_active = bool(
        item.get("isActive")
        or item.get("active")
        or (stage_ru and "завершен" not in str(stage_ru).lower())
    )
    return BankruptcyCase(
        case_number=str(case_number) if case_number else None,
        stage_ru=str(stage_ru) if stage_ru else None,
        stage=_normalise_stage(str(stage_ru) if stage_ru else None),
        is_active=is_active,
        raw=item,
    )


class EfrsbClient:
    """Минимальный async-клиент над публичным backend ЕФРСБ.

    Использование:
        async with EfrsbClient() as c:
            cases = await c.search_active_by_inn("7707083893")
    """

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
                    "Accept": "application/json",
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

    async def search_active_by_inn(self, inn: str) -> list[BankruptcyCase]:
        """Найти активные дела о банкротстве по ИНН контрагента.

        Возвращает только активные кейсы. Пустой список означает «банкротных
        дел нет / процедура завершена», что считается good signal в legal_status.
        """
        if not is_valid_inn(inn):
            raise ValidationError(f"Невалидный ИНН '{inn}'", details={"input": inn})

        try:
            response = await self.client.get(
                f"{self._base_url}{EFRSB_SEARCH_PATH}",
                params={"searchString": inn, "limit": 10, "offset": 0},
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

        if response.status_code == 429:
            raise RateLimitedError(
                "ЕФРСБ ответил 429 (rate-limit).",
                details={"status": 429},
            )
        if response.status_code >= 500:
            raise SourceUnavailableError(
                f"ЕФРСБ вернул {response.status_code}.",
                details={"status": response.status_code},
            )
        if response.status_code == 404:
            return []
        if response.status_code >= 400:
            raise SourceUnavailableError(
                f"ЕФРСБ вернул {response.status_code}.",
                details={"status": response.status_code, "body": response.text[:300]},
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise ParseError(
                "Не удалось декодировать JSON-ответ ЕФРСБ.",
                details={"body": response.text[:300]},
            ) from exc

        items: list[dict[str, Any]] = []
        if isinstance(payload, dict):
            items = (
                payload.get("pageData")
                or payload.get("items")
                or payload.get("data")
                or []
            )
        elif isinstance(payload, list):
            items = payload

        cases = [_parse_company(item) for item in items if isinstance(item, dict)]
        return [c for c in cases if c.is_active]
