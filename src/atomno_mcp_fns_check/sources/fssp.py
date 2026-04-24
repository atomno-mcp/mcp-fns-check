"""Async-клиент Банка данных исполнительных производств ФССП.

Источник: https://fssp.gov.ru/. У публичного фронта есть JSON-эндпоинт
`/_search?type=4&...` (type=4 — поиск по юр.лицу). Эндпоинт защищён
капчей в браузерном UX, но GET без cookies-сессии часто отвечает с
HTTP 200 и `data.captcha=true` либо `status=captcha_required`.

Клиент НЕ умеет решать капчу. При обнаружении флага капчи бросается
`SourceUnavailableError("captcha required")`, агрегатор красиво
помещает это в `errors[]` с понятным русским сообщением.

Поля исп. производства, которые нас интересуют:
  * сумма требования (debt_amount_rub),
  * статус (open / closed),
  * номер ИП.

Никаких write-операций.
"""

from __future__ import annotations

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

FSSP_BASE_URL = "https://fssp.gov.ru"
FSSP_SEARCH_PATH = "/_search"
DEFAULT_TIMEOUT = 12.0
DEFAULT_USER_AGENT = "atomno-mcp-fns-check/0.4 (+https://example.com/contact)"


@dataclass(slots=True)
class EnforcementProceeding:
    """Одно открытое исполнительное производство по контрагенту."""

    case_number: str | None
    debt_amount_rub: float | None
    status: str | None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        if not self.status:
            return True
        s = self.status.lower()
        return "оконч" not in s and "прекр" not in s and "заверш" not in s


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        v = v.replace("\xa0", " ").replace(",", ".").strip()
        if not v:
            return None
        # Иногда сумма приходит как "1 234 567.89".
        v = v.replace(" ", "")
        try:
            return float(v)
        except ValueError:
            return None
    return None


def _parse_proceeding(item: dict[str, Any]) -> EnforcementProceeding:
    case = (
        item.get("ip_number")
        or item.get("ipNumber")
        or item.get("number")
        or item.get("ip")
    )
    debt = _to_float(
        item.get("debt_amount")
        or item.get("debtAmount")
        or item.get("sum")
        or item.get("subjAmount")
    )
    status = (
        item.get("status")
        or item.get("ip_status")
        or item.get("ipStatus")
    )
    return EnforcementProceeding(
        case_number=str(case) if case else None,
        debt_amount_rub=debt,
        status=str(status) if status else None,
        raw=item,
    )


class FsspClient:
    """Минимальный async-клиент Банка данных ФССП.

    Использование:
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
            raise RuntimeError("FsspClient is not initialised. Use 'async with FsspClient()'.")
        return self._client

    async def search_proceedings_by_inn(self, inn: str) -> list[EnforcementProceeding]:
        """Найти открытые исполнительные производства по ИНН должника-юрлица.

        Возвращает только открытые (`is_open == True`) производства.
        Если CAPTCHA требуется — `SourceUnavailableError`. Агрегатор
        обернёт его в `errors[]` без падения отчёта целиком.
        """
        if not is_valid_inn(inn):
            raise ValidationError(f"Невалидный ИНН '{inn}'", details={"input": inn})

        try:
            response = await self.client.get(
                f"{self._base_url}{FSSP_SEARCH_PATH}",
                params={"type": 4, "variant": 1, "is": inn},
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
            raise RateLimitedError("ФССП ответил 429 (rate-limit).", details={"status": 429})
        if response.status_code >= 500:
            raise SourceUnavailableError(
                f"ФССП вернул {response.status_code}.",
                details={"status": response.status_code},
            )
        if response.status_code == 404:
            return []
        if response.status_code >= 400:
            raise SourceUnavailableError(
                f"ФССП вернул {response.status_code}.",
                details={"status": response.status_code, "body": response.text[:300]},
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise ParseError(
                "Не удалось декодировать JSON-ответ ФССП.",
                details={"body": response.text[:300]},
            ) from exc

        if isinstance(payload, dict):
            captcha_flag = (
                payload.get("captcha")
                or payload.get("captcha_required")
                or payload.get("status") == "captcha_required"
            )
            if captcha_flag:
                raise SourceUnavailableError(
                    "ФССП требует ввода CAPTCHA — автоматическая проверка невозможна. "
                    "Проверьте контрагента вручную на fssp.gov.ru/iss/ip.",
                    details={"reason": "captcha_required"},
                )

        items: list[dict[str, Any]] = []
        if isinstance(payload, dict):
            response_block = payload.get("response") or payload.get("data") or {}
            if isinstance(response_block, dict):
                items = (
                    response_block.get("result")
                    or response_block.get("items")
                    or response_block.get("rows")
                    or []
                )
            elif isinstance(response_block, list):
                items = response_block
            if not items:
                items = payload.get("items") or payload.get("result") or []
        elif isinstance(payload, list):
            items = payload

        cases = [_parse_proceeding(it) for it in items if isinstance(it, dict)]
        return [c for c in cases if c.is_open]
