"""FastMCP entrypoint для atomno-mcp-fns-check.

Регистрирует 7 тулзов (SPEC v0.1 §4.1, актуализировано после
миграции в PRODUCTS/ATOMNO/):

    Главный агрегатор:
        * check_contractor — полная проверка по одному идентификатору
          (ИНН или ОГРН), детерминированный вердикт и рекомендации.

    Гранулярные тулзы (для случаев, когда агент хочет только часть картины):
        * check_inn              — базовая карточка по ИНН.
        * check_ogrn             — базовая карточка по ОГРН/ОГРНИП.
        * get_legal_status       — жизненный статус с обогащением через ЕФРСБ.
        * get_okveds             — ОКВЭД с расшифровкой по справочнику ОКВЭД-2.
        * get_directors_history  — текущий руководитель + история (по мере
                                   подгрузки Open Data ЕГРЮЛ).
        * check_for_red_flags    — 8 проверок риска (4 базовых + 4 расширенных).

Сервис-контекст (`ServiceContext`) создаётся лениво при первом вызове, общий
на жизненный цикл процесса. Закрытие httpx-клиентов и SQLite — через
`atexit`-хук.
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import importlib.resources as importlib_resources
import logging
import os
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from . import __version__
from .context import ServiceContext
from .errors import McpFnsError
from .tools import check_contractor as _check_contractor_impl
from .tools import check_for_red_flags as _check_for_red_flags_impl
from .tools import check_inn as _check_inn_impl
from .tools import check_ogrn as _check_ogrn_impl
from .tools import get_directors_history as _get_directors_history_impl
from .tools import get_legal_status as _get_legal_status_impl
from .tools import get_okveds as _get_okveds_impl

logger = logging.getLogger("atomno_mcp_fns_check")

mcp: FastMCP = FastMCP(
    name="atomno-mcp-fns-check",
    instructions=(
        "Сервер для проверки российских контрагентов через открытые данные ФНС. "
        "Главный тул — check_contractor(identifier): агрегирует карточку ЕГРЮЛ, "
        "жизненный статус с обогащением из ЕФРСБ, 8 проверок риска (включая "
        "массовые адреса/руководители, дисквалификация, банкротство, налоговые "
        "долги, исполнительные производства, арбитражные дела) и возвращает "
        "детерминированный вердикт плюс список рекомендаций. Гранулярные тулзы "
        "(check_inn, check_ogrn, get_legal_status, get_okveds, "
        "get_directors_history, check_for_red_flags) доступны для точечных "
        "запросов. Источники — публичные: egrul.nalog.ru, bankrot.fedresurs.ru, "
        "pb.nalog.ru, fssp.gov.ru, kad.arbitr.ru."
    ),
)

_ctx: ServiceContext | None = None
_ctx_lock = asyncio.Lock()


async def _get_ctx() -> ServiceContext:
    global _ctx
    if _ctx is not None:
        return _ctx
    async with _ctx_lock:
        if _ctx is None:
            ctx = ServiceContext.from_env()
            await ctx.__aenter__()
            await _maybe_seed_registries(ctx)
            _ctx = ctx
            atexit.register(_close_ctx_atexit)
    assert _ctx is not None
    return _ctx


async def _maybe_seed_registries(ctx: ServiceContext) -> None:
    """Если реестры пусты, подгрузить bundled-сидинг.

    На стадии S3 это синтетический мини-набор. На S4 заменится на полноценный
    ETL из data.nalog.ru, который перезаписывает таблицы целиком.
    """
    if ctx.registries is None:
        return
    if await ctx.registries.get_meta("seed_version") is not None:
        return
    try:
        with importlib_resources.as_file(
            importlib_resources.files("atomno_mcp_fns_check.data").joinpath("registries_seed.json")
        ) as seed_path:
            counts = await ctx.registries.load_seed(seed_path)
        logger.info("registries seeded: %s", counts)
    except Exception as exc:  # pragma: no cover - best-effort
        logger.warning("registries seed skipped: %s", exc)


def _close_ctx_atexit() -> None:
    if _ctx is None:
        return
    try:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(_ctx.__aexit__(None, None, None))
        loop.close()
    except Exception:  # pragma: no cover - best-effort cleanup
        pass


def _err(exc: McpFnsError) -> dict[str, Any]:
    return exc.to_dict()


@mcp.tool()
async def ping() -> dict[str, Any]:
    """Диагностический тул: проверяет, что сервер запущен и доступен."""
    return {
        "ok": True,
        "service": "atomno-mcp-fns-check",
        "version": __version__,
        "cache_db": _cache_db_path(),
    }


@mcp.tool()
async def check_contractor(
    identifier: str,
    include_extended_risks: bool = True,
    lawsuits_threshold_rub: float = 1_000_000.0,
) -> dict[str, Any]:
    """Главный тул: полная проверка контрагента по одному идентификатору.

    Принимает ИНН (10 цифр — юр.лицо, 12 — ИП/физлицо) или ОГРН
    (13 цифр — юр.лицо, 15 — ИП). Тип определяется строго по длине и
    контрольной цифре.

    Собирает полную картину из 5 публичных источников ФНС и связанных:
      * egrul.nalog.ru      — базовая карточка (наименование, руководитель,
                              адрес, ОКВЭД, статус).
      * bankrot.fedresurs.ru (ЕФРСБ) — активные дела о банкротстве.
      * pb.nalog.ru (Прозрачный бизнес) — налоговые долги, непредставление
                              отчётности.
      * fssp.gov.ru         — открытые исполнительные производства.
      * kad.arbitr.ru       — активные арбитражные дела как ответчика.
      * Локальные реестры ФНС — массовые адреса, массовые руководители,
                              дисквалифицированные руководители.

    Возвращает агрегированный отчёт:
      * `card`                 — CounterpartyCard с базовыми полями.
      * `legal_status`         — жизненный статус (active/bankruptcy/...).
      * `risks`                — до 8 проверок риска со scoring 0..100.
      * `verdict_action`       — детерминированный вердикт:
          - `safe_to_proceed`                 — препятствий не найдено.
          - `manual_review_required`          — medium-риски или часть
                                                источников не ответила.
          - `high_risk_do_not_proceed`        — высокий риск / банкротство /
                                                дисквалифицированный директор.
          - `impossible_contractor_defunct`   — контрагент ликвидирован или
                                                исключён из ЕГРЮЛ.
      * `verdict_reason_ru`    — краткое обоснование вердикта на русском.
      * `recommendations`      — детерминированный список рекомендаций
                                 (по таблице FlagCode → рекомендация).
      * `sources`              — метаданные каких источников опрошено.
      * `tier`                 — всегда "open" для этого open-source клиента
                                 (значения "free"/"pro" — для hosted-бэка).

    Поведение при сбоях источников:
      * Если ЕГРЮЛ недоступен — поднимается SourceUnavailableError
        (базовая карточка — единственный blocking-источник).
      * Сбои остальных источников (ЕФРСБ/ФССП/КАД/Прозрачный бизнес)
        складываются в `risks.errors[]` и НЕ валят общий отчёт.
      * При часте сбоев вердикт становится `manual_review_required`.

    Args:
        identifier: ИНН (10/12 цифр) или ОГРН/ОГРНИП (13/15 цифр).
            Пример: '7707083893' (Сбербанк) или '1027700132195'.
        include_extended_risks: Запускать ли 4 расширенные проверки
            (Прозрачный бизнес, ФССП, КАД). По умолчанию True.
        lawsuits_threshold_rub: Порог фильтрации арбитражных дел в рублях.
            По умолчанию 1 000 000 ₽ — отсекает мелкие споры.
    """
    try:
        ctx = await _get_ctx()
        report = await _check_contractor_impl(
            ctx,
            identifier,
            include_extended_risks=include_extended_risks,
            lawsuits_threshold_rub=lawsuits_threshold_rub,
        )
        return report.model_dump(mode="json")
    except McpFnsError as exc:
        return _err(exc)


@mcp.tool()
async def check_inn(inn: str, include_extended: bool = False) -> dict[str, Any]:
    """Полная карточка контрагента по ИНН (10 цифр — юр.лицо, 12 — ИП).

    Args:
        inn: ИНН контрагента. Пример: '7707083893' (Сбербанк).
        include_extended: зарезервировано для S2.x (Прозрачный бизнес).
    """
    try:
        ctx = await _get_ctx()
        card = await _check_inn_impl(ctx, inn, include_extended=include_extended)
        return card.model_dump(mode="json")
    except McpFnsError as exc:
        return _err(exc)


@mcp.tool()
async def check_ogrn(ogrn: str, include_extended: bool = False) -> dict[str, Any]:
    """Карточка контрагента по ОГРН (13 цифр) или ОГРНИП (15 цифр)."""
    try:
        ctx = await _get_ctx()
        card = await _check_ogrn_impl(ctx, ogrn, include_extended=include_extended)
        return card.model_dump(mode="json")
    except McpFnsError as exc:
        return _err(exc)


@mcp.tool()
async def get_legal_status(inn: str | None = None, ogrn: str | None = None) -> dict[str, Any]:
    """Текущий жизненный статус: active / liquidating / bankruptcy / liquidated / reorganizing / excluded_inactive.

    Минимум один из `inn` или `ogrn` обязателен.
    """
    try:
        ctx = await _get_ctx()
        report = await _get_legal_status_impl(
            inn=inn, ogrn=ogrn, egrul=ctx.egrul, efrsb=ctx.efrsb
        )
        return report.model_dump(mode="json")
    except McpFnsError as exc:
        return _err(exc)


@mcp.tool()
async def get_okveds(
    inn: str | None = None,
    ogrn: str | None = None,
    include_history: bool = False,
) -> dict[str, Any]:
    """Перечень кодов ОКВЭД основной + дополнительные с расшифровкой по справочнику ОКВЭД-2."""
    try:
        ctx = await _get_ctx()
        report = await _get_okveds_impl(
            ctx, inn=inn, ogrn=ogrn, include_history=include_history
        )
        return report.model_dump(mode="json")
    except McpFnsError as exc:
        return _err(exc)


@mcp.tool()
async def get_directors_history(
    inn: str,
    include_founders: bool = True,
    depth_years: int = 10,
) -> dict[str, Any]:
    """История смены руководителей и учредителей по ИНН.

    На стадии S3 возвращает только текущего руководителя из egrul.nalog.ru
    плюс предупреждение о неполноте данных. Полная история (с датами
    назначения/увольнения) появится на стадии S5 после загрузки Open Data
    slice ЕГРЮЛ.

    Args:
        inn: ИНН контрагента (10 или 12 цифр).
        include_founders: на S3 не используется (ЕГРЮЛ-search не отдаёт учредителей).
        depth_years: на S3 не используется (история пока без дат).
    """
    try:
        ctx = await _get_ctx()
        report = await _get_directors_history_impl(ctx, inn)
        return report.model_dump(mode="json")
    except McpFnsError as exc:
        return _err(exc)


@mcp.tool()
async def check_for_red_flags(
    inn: str,
    include_extended: bool = True,
    lawsuits_threshold_rub: float = 1_000_000.0,
) -> dict[str, Any]:
    """Агрегированный риск-чек по контрагенту (8 проверок, SPEC §3.5).

    Базовые 4 проверки (всегда):
      * `mass_address` — массовый юр.адрес ФНС;
      * `mass_director` — массовый руководитель (≥10 действующих компаний);
      * `disqualified_director` — руководитель в реестре дисквалифицированных;
      * `bankruptcy_records` — активные дела в ЕФРСБ.

    Расширенные 4 проверки (`include_extended=True`):
      * `tax_debts` — индикатор задолженности по налогам (Прозрачный бизнес);
      * `no_reporting` — нет налоговой отчётности > 1 года (Прозрачный бизнес);
      * `enforcement_proceedings` — открытые исп.производства (Банк данных ФССП);
      * `active_lawsuits` — активные арбитражные дела где контрагент ответчик
        (КАД), фильтр по сумме иска через `lawsuits_threshold_rub`.

    Любая внешняя проверка может вернуть ошибку (CAPTCHA, 5xx, timeout) — она
    попадёт в `errors[]` и не сорвёт остальные проверки.

    Args:
        inn: ИНН контрагента (10 или 12 цифр).
        include_extended: запускать ли расширенные 4 проверки. По умолчанию True.
        lawsuits_threshold_rub: порог фильтра арбитражных дел в рублях.
            По умолчанию 1 000 000 ₽ (отсечь мелкие споры).
    """
    try:
        ctx = await _get_ctx()
        report = await _check_for_red_flags_impl(
            ctx,
            inn,
            include_extended=include_extended,
            lawsuits_threshold_rub=lawsuits_threshold_rub,
        )
        return report.model_dump(mode="json")
    except McpFnsError as exc:
        return _err(exc)


def _cache_db_path() -> str:
    return os.environ.get("MCP_FNS_CACHE_DB", str(Path.cwd() / "atomno_mcp_fns_check_cache.sqlite"))


# Список транспортов FastMCP, которые мы допускаем в публичном CLI. Он
# совпадает с `fastmcp.server.server.Transport`-literal; хардкодим здесь,
# чтобы не зависеть от внутренних символов fastmcp (стабильный публичный
# контракт остаётся за `FastMCP.run(transport=...)`).
_SUPPORTED_TRANSPORTS = ("stdio", "http", "sse", "streamable-http")
_DEFAULT_TRANSPORT = "stdio"
_DEFAULT_HTTP_HOST = "127.0.0.1"
_DEFAULT_HTTP_PORT = 8000
_VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="atomno-mcp-fns-check",
        description=(
            "MCP-сервер для проверки российских контрагентов через открытые данные ФНС "
            "(ЕГРЮЛ/ЕГРИП, ЕФРСБ, Прозрачный бизнес, ФССП, КАД). "
            "7 тулзов: check_contractor (главный агрегатор) + 6 granular. "
            "По умолчанию запускается по MCP stdio-транспорту для интеграции с Cursor, "
            "Claude Desktop, Claude Code и другими MCP-клиентами."
        ),
        epilog=(
            "Примеры:\n"
            "  atomno-mcp-fns-check                      # запуск для MCP-клиента через stdio\n"
            "  atomno-mcp-fns-check --transport http --port 8000\n"
            "  atomno-mcp-fns-check --log-level DEBUG\n"
            "\n"
            "Переменные окружения:\n"
            "  MCP_FNS_CACHE_DB   — путь к SQLite-файлу кэша ответов ЕГРЮЛ (TTL 168h).\n"
            "  MCP_FNS_LOG_LEVEL  — уровень логирования (перекрывается флагом --log-level).\n"
            "\n"
            "Документация: https://github.com/atomno-mcp/mcp-fns-check"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        "-V",
        action="version",
        version=f"atomno-mcp-fns-check {__version__}",
        help="показать версию пакета и выйти",
    )
    parser.add_argument(
        "--transport",
        "-t",
        choices=_SUPPORTED_TRANSPORTS,
        default=_DEFAULT_TRANSPORT,
        help=(
            f"MCP-транспорт (по умолчанию: {_DEFAULT_TRANSPORT}). "
            "stdio — для локальных MCP-клиентов; http/sse/streamable-http — для сетевых."
        ),
    )
    parser.add_argument(
        "--host",
        default=_DEFAULT_HTTP_HOST,
        help=(
            f"Хост для http/sse/streamable-http транспортов (по умолчанию: {_DEFAULT_HTTP_HOST}). "
            "Игнорируется для stdio."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=_DEFAULT_HTTP_PORT,
        help=(
            f"Порт для http/sse/streamable-http транспортов (по умолчанию: {_DEFAULT_HTTP_PORT}). "
            "Игнорируется для stdio."
        ),
    )
    parser.add_argument(
        "--log-level",
        "-l",
        choices=_VALID_LOG_LEVELS,
        default=None,
        help=(
            "Уровень логирования; перекрывает переменную MCP_FNS_LOG_LEVEL. "
            "По умолчанию INFO."
        ),
    )
    return parser


def _resolve_log_level(cli_value: str | None) -> str:
    """CLI-флаг имеет приоритет над env; фолбэк — INFO.

    Возвращает строку (одну из _VALID_LOG_LEVELS). Никаких silent-fallback
    на невалидные значения — argparse уже валидирует CLI, env-значение
    нормализуется и матчится строго.
    """
    if cli_value is not None:
        return cli_value
    env_raw = os.environ.get("MCP_FNS_LOG_LEVEL")
    if env_raw is None:
        return "INFO"
    env_norm = env_raw.strip().upper()
    if env_norm in _VALID_LOG_LEVELS:
        return env_norm
    raise ValueError(
        f"MCP_FNS_LOG_LEVEL={env_raw!r} — недопустимое значение. "
        f"Допустимые: {', '.join(_VALID_LOG_LEVELS)}."
    )


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI.

    Args:
        argv: список аргументов (без имени программы). Если None — берётся
            из sys.argv[1:]. Возвращает exit-code для программного
            использования (0 — штатное завершение, 2 — ошибка конфигурации).
    """
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    try:
        log_level = _resolve_log_level(args.log_level)
    except ValueError as exc:
        parser.error(str(exc))
        return 2  # pragma: no cover - parser.error вызывает SystemExit(2)

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info(
        "atomno-mcp-fns-check %s starting (transport=%s, cache=%s)",
        __version__,
        args.transport,
        _cache_db_path(),
    )

    run_kwargs: dict[str, Any] = {"transport": args.transport}
    if args.transport in {"http", "sse", "streamable-http"}:
        run_kwargs["host"] = args.host
        run_kwargs["port"] = args.port
    mcp.run(**run_kwargs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
