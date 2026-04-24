"""Async-клиент Картотеки арбитражных дел (КАД, kad.arbitr.ru).

Источник: https://kad.arbitr.ru. Публичный фронт делает POST к
`/Kad/SearchInstances` с JSON-телом и ожидает специфические заголовки
(`x-date-format`, `Content-Type: application/json`). При попытке без
правильных headers сервер часто возвращает 200 + HTML с antibot-CAPTCHA
либо 403/406. Клиент детектит это и возвращает `SourceUnavailableError`.

Нас на стадии S4 интересуют только активные дела, в которых контрагент
является ответчиком и сумма иска превышает `min_amount_rub`. Открытые
данные КАД не отдают сумму напрямую — она доступна на странице дела,
поэтому здесь возвращаем сумму как `None` если её нет в payload, а
агрегатор фильтрует по факту наличия активного дела (без суммы).
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

KAD_BASE_URL = "https://kad.arbitr.ru"
KAD_SEARCH_PATH = "/Kad/SearchInstances"
DEFAULT_TIMEOUT = 15.0
DEFAULT_USER_AGENT = "atomno-mcp-fns-check/0.4 (+https://example.com/contact)"

_ROLE_DEFENDANT_RU = ("ответчик", "должник")
_OPEN_STATUS_RU = ("открыто", "рассматривается", "принято", "в производстве")


@dataclass(slots=True)
class KadCase:
    """Одно арбитражное дело."""

    case_number: str | None
    role: str | None
    amount_rub: float | None
    status: str | None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_defendant(self) -> bool:
        if not self.role:
            return False
        r = self.role.lower()
        return any(role in r for role in _ROLE_DEFENDANT_RU)

    @property
    def is_open(self) -> bool:
        if not self.status:
            return True
        s = self.status.lower()
        if any(open_w in s for open_w in _OPEN_STATUS_RU):
            return True
        return "законч" not in s and "оконч" not in s and "архив" not in s


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        v = v.replace("\xa0", "").replace(" ", "").replace(",", ".").strip()
        if not v:
            return None
        try:
            return float(v)
        except ValueError:
            return None
    return None


def _parse_case(item: dict[str, Any]) -> KadCase:
    return KadCase(
        case_number=str(
            item.get("CaseNumber")
            or item.get("caseNumber")
            or item.get("number")
            or ""
        ) or None,
        role=str(item.get("Role") or item.get("role") or "") or None,
        amount_rub=_to_float(
            item.get("Amount")
            or item.get("amount")
            or item.get("Sum")
            or item.get("sum")
        ),
        status=str(item.get("Status") or item.get("status") or "") or None,
        raw=item,
    )


class KadClient:
    """Минимальный async-клиент КАД."""

    def __init__(
        self,
        *,
        base_url: str = KAD_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        user_agent: str = DEFAULT_USER_AGENT,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._user_agent = user_agent
        self._owns_client = client is None
        self._client = client

    async def __aenter__(self) -> "KadClient":
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                headers={
                    "User-Agent": self._user_agent,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "x-date-format": "iso",
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
            raise RuntimeError("KadClient is not initialised. Use 'async with KadClient()'.")
        return self._client

    async def search_active_lawsuits_by_inn(self, inn: str) -> list[KadCase]:
        """Найти активные арбитражные дела, в которых контрагент — ответчик.

        Возвращает только активные дела, где роль контрагента — ответчик/должник.
        """
        if not is_valid_inn(inn):
            raise ValidationError(f"Невалидный ИНН '{inn}'", details={"input": inn})

        body = {
            "Page": 1,
            "Count": 25,
            "CaseType": "G",  # Гражданские (арбитраж).
            "Sides": [{"Inn": inn, "Side": 2}],  # Side=2 — ответчик.
            "WithVKSInstances": False,
        }

        try:
            response = await self.client.post(
                f"{self._base_url}{KAD_SEARCH_PATH}",
                json=body,
            )
        except httpx.TimeoutException as exc:
            raise SourceUnavailableError(
                "КАД не ответил вовремя.",
                details={"reason": str(exc)},
            ) from exc
        except httpx.HTTPError as exc:
            raise SourceUnavailableError(
                f"КАД HTTP ошибка: {exc}",
                details={"reason": str(exc)},
            ) from exc

        if response.status_code == 429:
            raise RateLimitedError("КАД ответил 429 (rate-limit).", details={"status": 429})
        if response.status_code == 403:
            raise SourceUnavailableError(
                "КАД заблокировал запрос (403). Скорее всего сработала antibot-защита.",
                details={"status": 403},
            )
        if response.status_code >= 500:
            raise SourceUnavailableError(
                f"КАД вернул {response.status_code}.",
                details={"status": response.status_code},
            )
        if response.status_code == 404:
            return []
        if response.status_code >= 400:
            raise SourceUnavailableError(
                f"КАД вернул {response.status_code}.",
                details={"status": response.status_code, "body": response.text[:300]},
            )

        ctype = response.headers.get("content-type", "").lower()
        if "json" not in ctype:
            raise SourceUnavailableError(
                "КАД вернул не-JSON ответ — вероятно, antibot-страница.",
                details={"content_type": ctype},
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise ParseError(
                "Не удалось декодировать JSON-ответ КАД.",
                details={"body": response.text[:300]},
            ) from exc

        items: list[dict[str, Any]] = []
        if isinstance(payload, dict):
            result = payload.get("Result") or payload.get("result") or {}
            if isinstance(result, dict):
                items = result.get("Items") or result.get("items") or []
            elif isinstance(result, list):
                items = result
        elif isinstance(payload, list):
            items = payload

        cases = [_parse_case(it) for it in items if isinstance(it, dict)]
        return [c for c in cases if c.is_defendant and c.is_open]
