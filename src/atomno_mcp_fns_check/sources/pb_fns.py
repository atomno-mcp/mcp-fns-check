"""Async-клиент сервиса «Прозрачный бизнес» ФНС (pb.nalog.ru).

Источник: https://pb.nalog.ru/. Публичный фронт умеет JSON через эндпоинт
`/search-proc.json?query=<inn>&mode=search-ul&page=1&pageSize=10` (для юр.лиц)
и `mode=search-ip` (для ИП). В ответе приходят так называемые «теги»
(индикаторы) — флаги нарушений / особенностей контрагента.

Нас на стадии S4 интересуют два индикатора:

  * `tax_debt`        — «Сведения об имеющейся задолженности по уплате
                        налогов и сборов». Точную сумму открытый
                        интерфейс не возвращает, только сам факт.
  * `no_reporting`    — «Сведения о непредставлении налоговой отчётности
                        более года».

Эндпоинты pb.nalog.ru НЕ задокументированы публично, поэтому клиент
максимально толерантен к смене схемы: смотрит ключи в нескольких
исторических вариантах.

Никаких write-операций. Запросы read-only, кэшируются на уровне ServiceContext.
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

PB_BASE_URL = "https://pb.nalog.ru"
PB_SEARCH_PATH = "/search-proc.json"
DEFAULT_TIMEOUT = 10.0
DEFAULT_USER_AGENT = "atomno-mcp-fns-check/0.4 (+https://example.com/contact)"

_TAX_DEBT_KEYS = {"tax_debt", "taxdebt", "taxarrearsexceeded"}
_NO_REPORTING_KEYS = {
    "no_reporting",
    "noreporting",
    "notaxreportingmorethanyear",
}

# Корневые подстроки для русскоязычных тегов (фронт может выдавать
# индикаторы человеческим текстом).
_TAX_DEBT_RU_PARTS = (("задолжен", "налог"), ("долг", "налог"))
_NO_REPORTING_RU_PARTS = (
    ("непредставлен", "отчётност"),
    ("непредставлен", "отчетност"),
    ("не представл", "отчётност"),
    ("не представл", "отчетност"),
)


@dataclass(slots=True)
class PbFnsTags:
    """Набор «тегов» из карточки Прозрачного бизнеса.

    Поля:
        has_tax_debt         — найден индикатор задолженности по налогам.
        has_no_reporting     — найден индикатор «нет отчётности > 1 года».
        raw_tags             — все теги, как пришли (для отладки и расширения).
    """

    has_tax_debt: bool
    has_no_reporting: bool
    raw_tags: list[str] = field(default_factory=list)
    raw_payload: dict[str, Any] = field(default_factory=dict)


def _extract_tags(payload: dict[str, Any]) -> list[str]:
    """Достать список тегов из ответа pb.nalog.ru.

    Исторически встречаются два варианта:
      * `{"ul": {"data": [{"tags": ["...", "..."]}]}}`
      * `{"data": [{"tags": [...]}, ...]}`
      * `{"items": [{"indicators": [...]}, ...]}`
    """
    candidates: list[Any] = []
    for top in ("ul", "ip"):
        node = payload.get(top)
        if isinstance(node, dict):
            data = node.get("data") or node.get("items")
            if isinstance(data, list):
                candidates.extend(data)
    if "data" in payload and isinstance(payload["data"], list):
        candidates.extend(payload["data"])
    if "items" in payload and isinstance(payload["items"], list):
        candidates.extend(payload["items"])

    tags: list[str] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        for key in ("tags", "indicators", "tagList"):
            v = item.get(key)
            if isinstance(v, list):
                tags.extend(str(t) for t in v if t)
    return tags


def _has_any(
    tags: list[str], wanted: set[str], ru_parts: tuple[tuple[str, ...], ...] = ()
) -> bool:
    """True, если в `tags` есть либо точный технический ключ (`wanted`),
    либо русский тег, в котором встречаются ВСЕ корни одной из пар `ru_parts`."""
    if not tags:
        return False
    norm = [t.lower().strip() for t in tags]
    wanted_norm = {w.lower().strip() for w in wanted}
    for t in norm:
        if t in wanted_norm:
            return True
        for parts in ru_parts:
            if all(p in t for p in parts):
                return True
    return False


class PbFnsClient:
    """Тонкая async-обёртка над «Прозрачным бизнесом»."""

    def __init__(
        self,
        *,
        base_url: str = PB_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        user_agent: str = DEFAULT_USER_AGENT,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._user_agent = user_agent
        self._owns_client = client is None
        self._client = client

    async def __aenter__(self) -> "PbFnsClient":
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
            raise RuntimeError("PbFnsClient is not initialised. Use 'async with PbFnsClient()'.")
        return self._client

    async def get_tags_by_inn(self, inn: str) -> PbFnsTags:
        """Получить набор индикаторов «Прозрачного бизнеса» по ИНН.

        Возвращает пустой `PbFnsTags(False, False, [], {})` если контрагент
        в реестре не найден или у него нет интересных нам флагов.
        Бросает `SourceUnavailableError` / `RateLimitedError` / `ParseError` —
        они обрабатываются агрегатором как «проверка не выполнена».
        """
        if not is_valid_inn(inn):
            raise ValidationError(f"Невалидный ИНН '{inn}'", details={"input": inn})

        mode = "search-ul" if len(inn) == 10 else "search-ip"
        try:
            response = await self.client.get(
                f"{self._base_url}{PB_SEARCH_PATH}",
                params={"query": inn, "mode": mode, "page": 1, "pageSize": 10},
            )
        except httpx.TimeoutException as exc:
            raise SourceUnavailableError(
                "Прозрачный бизнес не ответил вовремя.",
                details={"reason": str(exc)},
            ) from exc
        except httpx.HTTPError as exc:
            raise SourceUnavailableError(
                f"Прозрачный бизнес HTTP ошибка: {exc}",
                details={"reason": str(exc)},
            ) from exc

        if response.status_code == 429:
            raise RateLimitedError(
                "Прозрачный бизнес ответил 429 (rate-limit).",
                details={"status": 429},
            )
        if response.status_code >= 500:
            raise SourceUnavailableError(
                f"Прозрачный бизнес вернул {response.status_code}.",
                details={"status": response.status_code},
            )
        if response.status_code == 404:
            return PbFnsTags(has_tax_debt=False, has_no_reporting=False)
        if response.status_code >= 400:
            raise SourceUnavailableError(
                f"Прозрачный бизнес вернул {response.status_code}.",
                details={"status": response.status_code, "body": response.text[:300]},
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise ParseError(
                "Не удалось декодировать JSON-ответ Прозрачного бизнеса.",
                details={"body": response.text[:300]},
            ) from exc

        if not isinstance(payload, dict):
            return PbFnsTags(has_tax_debt=False, has_no_reporting=False, raw_payload={})

        tags = _extract_tags(payload)
        return PbFnsTags(
            has_tax_debt=_has_any(tags, _TAX_DEBT_KEYS, _TAX_DEBT_RU_PARTS),
            has_no_reporting=_has_any(tags, _NO_REPORTING_KEYS, _NO_REPORTING_RU_PARTS),
            raw_tags=tags,
            raw_payload=payload,
        )
