"""Async-клиент egrul.nalog.ru.

Публичный интерфейс ЕГРЮЛ работает через двухшаговый POST-протокол:
    1. POST /             с form-data {query, ...} → возвращает {t: <token>}
    2. POST /search-result/<token>  → возвращает JSON со списком найденных карточек.

Поскольку формат не задокументирован официально, на S1 реализуем минимально
рабочий fetch и нормализованную обёртку `EgrulSearchHit` над одним хитом
из списка `rows`. Полный парсинг и маппинг в `CounterpartyCard` — на стадии S2.

Все запросы идут через httpx с таймаутом и осмысленным User-Agent
(см. SPEC §5.3, throttle ≤ 0.5 req/sec — управляется снаружи на уровне сервера).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..errors import (
    NotFoundError,
    ParseError,
    RateLimitedError,
    SourceUnavailableError,
    ValidationError,
)
from ..validators import is_valid_inn, is_valid_ogrn

EGRUL_BASE_URL = "https://egrul.nalog.ru"
DEFAULT_TIMEOUT = 15.0
DEFAULT_USER_AGENT = "atomno-mcp-fns-check/0.1 (+https://example.com/contact)"


@dataclass(slots=True)
class EgrulSearchHit:
    """Одна запись (юр.лицо или ИП) из ответа ЕГРЮЛ.

    Поля — близкие к сырому JSON, без полной нормализации (она в schemas.py / tools.py).
    """

    inn: str
    ogrn: str
    kpp: str | None
    name_full: str | None
    name_short: str | None
    address: str | None
    okved_code: str | None
    okved_name: str | None
    director_name: str | None
    director_position: str | None
    registration_date: str | None
    liquidation_date: str | None
    is_individual: bool
    raw: dict[str, Any] = field(default_factory=dict)


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _parse_row(row: dict[str, Any]) -> EgrulSearchHit:
    """Нормализовать одну запись из `rows` ответа ЕГРЮЛ.

    Ключи в разных версиях ответа отличаются. Поддерживаем оба основных набора:
    исторический (`n`, `i`, `o`, `k`, `a`, `g`, `r`, `e`...) и более новый (`name`,
    `inn`, ...). Если не нашли — оставляем None.
    """
    is_individual = "ip" in row or row.get("k") == "ip" or "fio" in row

    return EgrulSearchHit(
        inn=_str_or_none(row.get("i") or row.get("inn")) or "",
        ogrn=_str_or_none(row.get("o") or row.get("ogrn")) or "",
        kpp=_str_or_none(row.get("p") or row.get("kpp")),
        name_full=_str_or_none(row.get("n") or row.get("name") or row.get("fio")),
        name_short=_str_or_none(row.get("c") or row.get("name_short")),
        address=_str_or_none(row.get("a") or row.get("address")),
        okved_code=_str_or_none(row.get("e") or row.get("okved_code")),
        okved_name=_str_or_none(row.get("eName") or row.get("okved_name")),
        director_name=_str_or_none(row.get("g") or row.get("director_name")),
        director_position=_str_or_none(row.get("gp") or row.get("director_position")),
        registration_date=_str_or_none(row.get("r") or row.get("registration_date")),
        liquidation_date=_str_or_none(row.get("dl") or row.get("liquidation_date")),
        is_individual=bool(is_individual),
        raw=row,
    )


class EgrulClient:
    """Тонкая async-обёртка над публичным API egrul.nalog.ru.

    Использование:
        async with EgrulClient() as client:
            hits = await client.search_by_inn("7707083893")

    Клиент НЕ управляет rate-limit'ом — это задача вызывающего кода (FastMCP-сервер
    на S2 будет добавлять semaphore + throttle, см. SPEC §5.3).
    """

    def __init__(
        self,
        *,
        base_url: str = EGRUL_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        user_agent: str = DEFAULT_USER_AGENT,
        client: httpx.AsyncClient | None = None,
        max_attempts: int = 3,
        backoff_base: float = 0.5,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._user_agent = user_agent
        self._owns_client = client is None
        self._client = client
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base

    async def __aenter__(self) -> "EgrulClient":
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                headers={"User-Agent": self._user_agent},
            )
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("EgrulClient is not initialised. Use 'async with EgrulClient()'.")
        return self._client

    # --- Публичные методы поиска ---

    async def search_by_inn(self, inn: str) -> list[EgrulSearchHit]:
        if not is_valid_inn(inn):
            raise ValidationError(f"Невалидный ИНН '{inn}'", details={"input": inn})
        return await self._search({"query": inn})

    async def search_by_ogrn(self, ogrn: str) -> list[EgrulSearchHit]:
        if not is_valid_ogrn(ogrn):
            raise ValidationError(f"Невалидный ОГРН '{ogrn}'", details={"input": ogrn})
        return await self._search({"query": ogrn})

    # --- Внутренняя реализация ---

    async def _search(self, form: dict[str, str]) -> list[EgrulSearchHit]:
        token = await self._request_token(form)
        payload = await self._fetch_results(token)

        rows = payload.get("rows")
        if rows is None:
            raise ParseError(
                "Ответ ЕГРЮЛ не содержит поля 'rows'.",
                details={"payload_keys": list(payload.keys())},
            )

        if not rows:
            raise NotFoundError(
                f"Контрагент не найден в ЕГРЮЛ/ЕГРИП по запросу '{form.get('query')}'.",
                details=form,
            )

        try:
            return [_parse_row(r) for r in rows]
        except (KeyError, TypeError, ValueError) as exc:
            raise ParseError(
                f"Не удалось распарсить ответ ЕГРЮЛ: {exc}",
                details={"rows_count": len(rows)},
            ) from exc

    async def _request_token(self, form: dict[str, str]) -> str:
        payload = await self._post_with_retry("/", data=form)
        token = payload.get("t")
        if not token:
            raise ParseError(
                "ЕГРЮЛ не вернул поисковый токен (поле 't' отсутствует).",
                details={"payload_keys": list(payload.keys())},
            )
        return str(token)

    async def _fetch_results(self, token: str) -> dict[str, Any]:
        return await self._get_with_retry(f"/search-result/{token}")

    async def _post_with_retry(self, path: str, *, data: dict[str, str]) -> dict[str, Any]:
        return await self._with_retry("POST", path, data=data)

    async def _get_with_retry(self, path: str) -> dict[str, Any]:
        return await self._with_retry("GET", path)

    async def _with_retry(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self.client.request(method, f"{self._base_url}{path}", data=data)
            except httpx.TimeoutException as exc:
                last_exc = exc
            except httpx.HTTPError as exc:
                last_exc = exc
            else:
                if response.status_code == 429:
                    last_exc = RateLimitedError(
                        "ЕГРЮЛ ответил 429 (rate-limit). Снизьте частоту запросов.",
                        details={"status": 429, "attempt": attempt},
                    )
                elif 500 <= response.status_code < 600:
                    last_exc = SourceUnavailableError(
                        f"ЕГРЮЛ вернул {response.status_code}.",
                        details={"status": response.status_code, "attempt": attempt},
                    )
                elif response.status_code >= 400:
                    raise SourceUnavailableError(
                        f"ЕГРЮЛ ответил {response.status_code} (клиентская ошибка).",
                        details={"status": response.status_code, "body": response.text[:500]},
                    )
                else:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise ParseError(
                            "Не удалось декодировать JSON-ответ ЕГРЮЛ.",
                            details={"body": response.text[:500]},
                        ) from exc

            if attempt < self._max_attempts:
                await asyncio.sleep(self._backoff_base * (2 ** (attempt - 1)))

        if isinstance(last_exc, (RateLimitedError, SourceUnavailableError)):
            raise last_exc
        raise SourceUnavailableError(
            f"ЕГРЮЛ недоступен после {self._max_attempts} попыток.",
            details={"reason": str(last_exc) if last_exc else "unknown"},
        ) from last_exc
