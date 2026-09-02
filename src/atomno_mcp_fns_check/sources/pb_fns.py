"""Async-клиент сервиса «Прозрачный бизнес» ФНС (pb.nalog.ru).

Источник: https://pb.nalog.ru/. Публичный фронт работает **в два шага**:

  1. `POST /search-proc.json` с полем `queryUl` (юр.лицо) либо `queryIp` (ИП)
     и `mode=search-ul` / `mode=search-ip`. Ответ — «пропуск»::

         {"id":"68943a07-f6bf-47a7-9ec7-f4b14c2151ea","captchaRequired":false}

  2. `POST /search-proc.json` с `id=<пропуск>&method=get-response`. Первые
     обращения возвращают литерал `null` (запрос ещё выполняется), поэтому
     шаг повторяется с паузой, пока не придёт объект::

         {"ul":{"data":[{"namec":"ООО \\"ЦЕФЕЙ\\"","inn":"5506193814", ...}],
                "rowCount":1.0,"hasMore":false}}

Историческая ошибка, которую этот модуль закрывает: запрос уходил с полем
`query=` (одношагово), источник отвечал `HTTP 400` с явным указанием
правильного имени поля, а клиент трактовал это как «индикаторов нарушений
нет» — то есть молча выдавал «чисто» вместо «проверка не выполнена».

**Важное ограничение источника.** В карточке `search-proc.json` НЕТ ни
признака задолженности по налогам, ни признака непредставления отчётности.
Проверено на организации, которая присутствует в наборе открытых данных ФНС
`debtam` с недоимкой: карточка отдаёт только регистрационные сведения
(`namec`, `inn`, `ogrn`, `sulst_name_ex`, ОКВЭД, регион). Поэтому клиент
**не** пытается вывести эти признаки из карточки — для них есть отдельный
источник (`debtam`, таблица `tax_debts` локальных реестров). См.
`C:/Users/Velcat/.cursor/rules/service-response-honesty.mdc`.

Никаких write-операций. Капча не обходится: если источник отвечает
`captchaRequired: true`, клиент честно поднимает `SourceUnavailableError`.
"""

from __future__ import annotations

import asyncio
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
DEFAULT_USER_AGENT = "atomno-mcp-fns-check/0.5 (+https://atomno-mcp.ru)"

#: Сколько раз опрашивать второй шаг, пока источник считает результат.
DEFAULT_POLL_ATTEMPTS = 8
#: Пауза между опросами второго шага, секунды.
DEFAULT_POLL_DELAY = 0.7

#: Признаки, которых в ответе pb.nalog.ru НЕТ. Держим списком, чтобы
#: вызывающий код мог сослаться на него в честном сообщении об ошибке.
PB_UNSUPPORTED_INDICATORS: tuple[str, ...] = ("tax_debts", "no_reporting")


@dataclass(slots=True)
class PbCompanyCard:
    """Карточка организации из «Прозрачного бизнеса».

    Только те поля, которые источник реально отдаёт. Ничего не достраивается
    и не подставляется «по умолчанию»: отсутствующее поле остаётся `None`.
    """

    inn: str | None
    ogrn: str | None
    name_short: str | None
    name_full: str | None
    status_code: str | None
    status_ru: str | None
    region: str | None
    okved: str | None
    okved_name: str | None
    registered_at: str | None
    liquidation_flag: str | None
    invalid_info_flag: str | None
    raw: dict[str, Any] = field(default_factory=dict)


def _first_row(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Достать первую строку результата из ответа второго шага."""
    for top in ("ul", "ip"):
        node = payload.get(top)
        if not isinstance(node, dict):
            continue
        rows = node.get("data")
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            return rows[0]
    return None


def _to_card(row: dict[str, Any]) -> PbCompanyCard:
    return PbCompanyCard(
        inn=row.get("inn"),
        ogrn=row.get("ogrn"),
        name_short=row.get("namec"),
        name_full=row.get("namep"),
        status_code=row.get("sulst_ex"),
        status_ru=row.get("sulst_name_ex"),
        region=row.get("regionname"),
        okved=row.get("okved2") or row.get("okved2main"),
        okved_name=row.get("okved2name") or row.get("okved2mainname"),
        registered_at=row.get("dtreg") or row.get("dtogrn"),
        liquidation_flag=row.get("pr_liq"),
        invalid_info_flag=row.get("invalid"),
        raw=row,
    )


class PbFnsClient:
    """Тонкая async-обёртка над «Прозрачным бизнесом» (два шага + опрос)."""

    def __init__(
        self,
        *,
        base_url: str = PB_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        user_agent: str = DEFAULT_USER_AGENT,
        client: httpx.AsyncClient | None = None,
        poll_attempts: int = DEFAULT_POLL_ATTEMPTS,
        poll_delay: float = DEFAULT_POLL_DELAY,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._user_agent = user_agent
        self._owns_client = client is None
        self._client = client
        self._poll_attempts = max(1, poll_attempts)
        self._poll_delay = max(0.0, poll_delay)

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

    async def _post(self, data: dict[str, str]) -> httpx.Response:
        try:
            response = await self.client.post(
                f"{self._base_url}{PB_SEARCH_PATH}",
                data=data,
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
                "Прозрачный бизнес ответил 429 (превышен лимит запросов).",
                details={"status": 429},
            )
        if response.status_code >= 500:
            raise SourceUnavailableError(
                f"Прозрачный бизнес вернул {response.status_code}.",
                details={"status": response.status_code},
            )
        if response.status_code >= 400:
            # 400 здесь — почти всегда «не то имя поля»; источник называет
            # правильное прямо в теле. Не прячем это под «ничего не найдено».
            raise SourceUnavailableError(
                f"Прозрачный бизнес вернул {response.status_code}.",
                details={"status": response.status_code, "body": response.text[:300]},
            )
        return response

    async def _request_token(self, inn: str) -> str:
        """Шаг 1: получить «пропуск» на выполнение поиска."""
        is_legal_entity = len(inn) == 10
        mode = "search-ul" if is_legal_entity else "search-ip"
        query_field = "queryUl" if is_legal_entity else "queryIp"

        response = await self._post(
            {
                "page": "1",
                "pageSize": "10",
                query_field: inn,
                "mode": mode,
            }
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ParseError(
                "Прозрачный бизнес вернул не-JSON на шаге выдачи пропуска.",
                details={"body": response.text[:300], "step": "search-proc"},
            ) from exc

        if not isinstance(payload, dict):
            raise ParseError(
                "Прозрачный бизнес вернул неожиданную структуру на шаге выдачи пропуска.",
                details={"body": response.text[:300], "step": "search-proc"},
            )

        if payload.get("captchaRequired"):
            raise SourceUnavailableError(
                "Прозрачный бизнес требует ввод кода с картинки — автоматическая "
                "проверка невозможна. Проверьте контрагента вручную на pb.nalog.ru.",
                details={"reason": "captcha_required", "step": "search-proc"},
            )

        token = payload.get("id")
        if not token or not isinstance(token, str):
            raise ParseError(
                "Прозрачный бизнес не выдал идентификатор запроса (поле 'id').",
                details={"payload_keys": sorted(payload.keys()), "step": "search-proc"},
            )
        return token

    async def _fetch_result(self, token: str) -> dict[str, Any]:
        """Шаг 2: забрать результат по пропуску, опрашивая источник."""
        last_body = ""
        for attempt in range(self._poll_attempts):
            response = await self._post({"id": token, "method": "get-response"})
            last_body = response.text[:300]
            try:
                payload = response.json()
            except ValueError as exc:
                raise ParseError(
                    "Прозрачный бизнес вернул не-JSON на шаге получения результата.",
                    details={"body": last_body, "step": "get-response"},
                ) from exc
            if isinstance(payload, dict):
                if payload.get("captchaRequired"):
                    raise SourceUnavailableError(
                        "Прозрачный бизнес требует ввод кода с картинки — "
                        "автоматическая проверка невозможна.",
                        details={"reason": "captcha_required", "step": "get-response"},
                    )
                return payload
            # `null` — результат ещё не готов, ждём и повторяем.
            if attempt + 1 < self._poll_attempts and self._poll_delay:
                await asyncio.sleep(self._poll_delay)

        raise SourceUnavailableError(
            (
                f"Прозрачный бизнес не отдал результат за {self._poll_attempts} "
                "обращений — запрос завис на стороне источника."
            ),
            details={"reason": "empty_response", "last_body": last_body},
        )

    async def get_company_card(self, inn: str) -> PbCompanyCard | None:
        """Карточка организации из «Прозрачного бизнеса» по ИНН.

        Возвращает `None`, если источник отработал, но записей по ИНН нет.
        Поднимает `SourceUnavailableError` / `RateLimitedError` / `ParseError`,
        если ответ получить не удалось — вызывающий код обязан отличать это
        от «проверили, ничего не нашли».
        """
        if not is_valid_inn(inn):
            raise ValidationError(f"Невалидный ИНН '{inn}'", details={"input": inn})

        token = await self._request_token(inn)
        payload = await self._fetch_result(token)
        row = _first_row(payload)
        if row is None:
            return None
        return _to_card(row)
