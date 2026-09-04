"""check_for_red_flags — агрегатор риск-флагов по контрагенту.

Проверок семь:

  Базовые (S3):
    * `mass_address`            — адрес из реестра массовой регистрации ФНС;
    * `mass_director`           — руководитель с ≥ 10 действующих юр.лиц;
    * `disqualified_director`   — руководитель в реестре дисквалифицированных;
    * `bankruptcy_records`      — активные дела в ЕФРСБ.

  Расширенные (S4, включаются `include_extended=True`):
    * `tax_debts`               — задолженность по налогам из открытых данных
                                  ФНС (набор «debtam»);
    * `enforcement_proceedings` — открытые исполнительные производства
                                  в Банке данных ФССП;
    * `active_lawsuits`         — активные арбитражные дела (КАД), в которых
                                  контрагент — ответчик. Опц.фильтр по сумме
                                  иска через `lawsuits_threshold_rub`.

Восьмой проверки — непредставление налоговой отчётности — в наборе нет:
публичного источника этих сведений не существует (проверено 02.08.2026;
«Прозрачный бизнес» такой признак не отдаёт, отдельного набора открытых данных
ФНС нет, веб-раздел закрыт кодом с картинки). Вечно молчащая проверка делала
вердикт «можно работать» недостижимым для любой, даже идеально чистой компании,
поэтому она убрана из списка, а не подменена выдуманным «отчётность сдаётся».

Агрегатор:
  1. Запускает все включённые проверки параллельно через
     `asyncio.gather(return_exceptions=True)`.
  2. Каждая проверка возвращает `FlagDetail | None | <exception>`. Падения
     отдельных проверок не валят отчёт — они складываются в `errors[]`.
  3. Считается weighted score (0..100), уровень: low / medium / high.
  4. Формируется русское резюме `summary_ru` для удобного показа агенту.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

from ..context import ServiceContext
from ..db.registries import RegistryStatus
from ..errors import McpFnsError, ValidationError
from ..schemas import (
    CheckCoverage,
    CheckError,
    FlagCode,
    FlagDetail,
    OverallRiskLevel,
    RedFlagsReport,
    RiskLevel,
)
from ..sources.egrul import EgrulSearchHit
from ..validators import is_valid_inn
from .check import _fetch_hit  # noqa: PLC2701

# Веса риск-флагов в общем score (0..100). Сумма всех «high» больше 100 —
# намеренно: при наборе нескольких high автоматом выходим в потолок 100.
FLAG_WEIGHTS: dict[FlagCode, dict[RiskLevel, int]] = {
    "mass_address": {"low": 5, "medium": 15, "high": 25},
    "mass_director": {"low": 5, "medium": 15, "high": 25},
    "disqualified_director": {"low": 0, "medium": 20, "high": 30},
    "bankruptcy_records": {"low": 10, "medium": 25, "high": 40},
    "tax_debts": {"low": 5, "medium": 15, "high": 25},
    "enforcement_proceedings": {"low": 5, "medium": 15, "high": 25},
    "active_lawsuits": {"low": 5, "medium": 15, "high": 25},
}


@dataclass(slots=True)
class _CheckResult:
    code: FlagCode
    flag: FlagDetail | None
    skipped_reason: str | None
    error: CheckError | None


def _registry_empty_error(code: FlagCode, registry_label_ru: str) -> CheckError:
    """Реестр подключён, но локальная база пуста — данные ФНС ещё не загружены.

    Отличаем от «реестр не подключён» (skipped_reason) и от «проверили —
    совпадений нет» (чистый _CheckResult): здесь мы физически не можем дать
    достоверный ответ, поэтому это ошибка (`errors[]`), а не тихое «риска нет».
    См. `C:/Users/Velcat/.cursor/rules/service-response-honesty.mdc`.
    """
    return CheckError(
        code=code,
        error="registry_empty",
        message_ru=(
            f"Локальная база реестра {registry_label_ru} ФНС на сервере пуста — "
            "загрузка данных ещё не выполнена. Проверка не может дать достоверный "
            "ответ: риск не подтверждён и не исключён."
        ),
    )


def _registry_unavailable_error(code: FlagCode, registry_label_ru: str) -> CheckError:
    """В базе лежит учебный набор, а не официальная выгрузка ФНС.

    Совпадение по учебным строкам нельзя выдавать как риск: это не данные
    налоговой. Тот же канал, что пустой/устаревший реестр — `errors[]`.
    """
    return CheckError(
        code=code,
        error="registry_unavailable",
        message_ru=(
            f"Официальная выгрузка реестра {registry_label_ru} ФНС не загружена. "
            "Проверка не выполнялась: риск не подтверждён и не исключён."
        ),
    )


async def _registry_source_gate(
    ctx: ServiceContext, code: FlagCode, registry: str, label_ru: str
) -> _CheckResult | None:
    """Отказ до поиска, если источника нет. None — можно искать.

    Учебный набор и пустая таблица не должны доходить до lookup: иначе
    совпадение по учебной строке выглядело бы как настоящий риск.
    """
    assert ctx.registries is not None  # noqa: S101 — вызывается только после проверки
    status = await ctx.registries.registry_status(registry)
    if status.is_training_sample:
        return _CheckResult(code, None, None, _registry_unavailable_error(code, label_ru))
    if status.is_empty:
        return _CheckResult(code, None, None, _registry_empty_error(code, label_ru))
    return None


def _registry_stale_error(
    code: FlagCode, registry_label_ru: str, status: RegistryStatus
) -> CheckError:
    """Реестр загружен, но данные ФНС устарели.

    Отрицательный результат поиска по устаревшей выгрузке ничего не доказывает:
    компания могла попасть в реестр уже после даты выгрузки. Поэтому «совпадений
    нет» здесь запрещено — только «не проверено» с указанием даты данных.
    """
    age = f"{status.age_days} дн." if status.age_days is not None else "неизвестно сколько"
    return CheckError(
        code=code,
        error="registry_stale",
        message_ru=(
            f"Данные реестра {registry_label_ru} ФНС устарели: выгрузка от "
            f"{status.data_as_of_ru}, ей уже {age} (допустимо "
            f"{status.stale_after_days} дн.). Отсутствие совпадения ничего не "
            "доказывает — компания могла попасть в реестр позже. Проверка "
            "не выполнена."
        ),
    )


async def _registry_negative_result(
    ctx: ServiceContext, code: FlagCode, registry: str, label_ru: str
) -> _CheckResult:
    """Как трактовать «в реестре не нашли».

    Достоверное «чисто» возможно только если реестр наполнен и свеж. Иначе —
    честная ошибка «не проверено».
    """
    assert ctx.registries is not None  # noqa: S101 — вызывается только после проверки
    status = await ctx.registries.registry_status(registry)
    if status.is_empty:
        return _CheckResult(code, None, None, _registry_empty_error(code, label_ru))
    if status.is_stale:
        return _CheckResult(code, None, None, _registry_stale_error(code, label_ru, status))
    return _CheckResult(code, None, None, None)


async def _registry_data_note(ctx: ServiceContext, registry: str) -> str | None:
    """Пометка «по данным ФНС от <дата>» для положительного совпадения."""
    if ctx.registries is None:
        return None
    status = await ctx.registries.registry_status(registry)
    if status.data_as_of is None:
        return None
    return status.data_as_of_ru


# --- 4 проверки ---


async def check_mass_address(
    ctx: ServiceContext,
    hit: EgrulSearchHit,
    *,
    address: str | None = None,
) -> _CheckResult:
    code: FlagCode = "mass_address"
    if ctx.registries is None:
        return _CheckResult(code, None, "Реестр массовых адресов не подключён.", None)

    gated = await _registry_source_gate(ctx, code, "mass_addresses", "массовых адресов")
    if gated is not None:
        return gated

    lookup_address = (address or hit.address or "").strip()
    if not lookup_address:
        # Поиск ЕГРЮЛ (`egrul.nalog.ru/search-result`) отдаёт только регион,
        # без юр.адреса. Проверить адрес можно, только если его передал
        # вызывающий (например, из договора или платёжных реквизитов).
        return _CheckResult(
            code,
            None,
            (
                "Юридический адрес неизвестен: поиск ЕГРЮЛ его не возвращает, "
                "а в запросе адрес не передан. Проверка по реестру массовых "
                "адресов не выполнялась — передайте адрес контрагента, чтобы "
                "она отработала."
            ),
            None,
        )

    match = await ctx.registries.lookup_mass_address(lookup_address)
    if match is None:
        return await _registry_negative_result(ctx, code, "mass_addresses", "массовых адресов")

    data_as_of = await _registry_data_note(ctx, "mass_addresses")
    count = match.registered_entities_count
    level: RiskLevel = "high" if count >= 30 else "medium" if count >= 5 else "low"
    return _CheckResult(
        code,
        FlagDetail(
            code=code,
            level=level,
            message_ru=(
                f"Адрес контрагента включён ФНС в реестр массовой регистрации: "
                f"по этому адресу зарегистрировано {count} юр.лиц"
                + (f" (по данным ФНС от {data_as_of})" if data_as_of else "")
                + "."
            ),
            details={
                "address_raw": match.address_raw,
                "registered_entities_count": count,
                "fns_inclusion_date": match.fns_inclusion_date,
                "registry_data_as_of": data_as_of,
            },
        ),
        None,
        None,
    )


async def check_mass_director(ctx: ServiceContext, hit: EgrulSearchHit) -> _CheckResult:
    code: FlagCode = "mass_director"
    if ctx.registries is None:
        return _CheckResult(code, None, "Реестр массовых руководителей не подключён.", None)

    gated = await _registry_source_gate(
        ctx, code, "mass_directors", "массовых руководителей и учредителей"
    )
    if gated is not None:
        return gated

    director_inn: str | None = None
    raw = hit.raw or {}
    for key in ("director_inn", "directorInn", "managerInn", "g_inn", "gi"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            director_inn = value.strip()
            break

    director_name = (hit.director_name or "").strip()

    if not director_inn and not director_name:
        return _CheckResult(
            code,
            None,
            "ИНН и ФИО руководителя отсутствуют в карточке ЕГРЮЛ.",
            None,
        )

    if director_inn:
        match = await ctx.registries.lookup_mass_director(director_inn)
        precision = "exact_inn"
    else:
        match = await ctx.registries.lookup_mass_director(full_name=director_name)
        precision = "approximate_name_only"

    if match is None:
        return await _registry_negative_result(
            ctx, code, "mass_directors", "массовых руководителей и учредителей"
        )

    data_as_of = await _registry_data_note(ctx, "mass_directors")
    count = match.companies_count
    level: RiskLevel = "high" if count >= 10 else "medium" if count >= 5 else "low"
    who = f"{match.role_title.capitalize()} {match.full_name or director_name or '(без ФИО)'}"
    approximate = (
        " Совпадение приблизительное (по ФИО, без ИНН), требует ручной верификации."
        if precision == "approximate_name_only"
        else ""
    )
    return _CheckResult(
        code,
        FlagDetail(
            code=code,
            level=level,
            message_ru=(
                f"{who} включён в реестр {match.registry_title_ru} ФНС: "
                f"{count} компаний"
                + (f" (по данным ФНС от {data_as_of})" if data_as_of else "")
                + "."
                + approximate
            ),
            details={
                "director_inn_masked": _mask_personal_inn(director_inn) if director_inn else None,
                "director_name": match.full_name or director_name or None,
                "role": match.role,
                "companies_count": count,
                "match_precision": precision,
                "fns_inclusion_date": match.fns_inclusion_date,
                "registry_data_as_of": data_as_of,
            },
        ),
        None,
        None,
    )


async def check_disqualified_director(
    ctx: ServiceContext, hit: EgrulSearchHit
) -> _CheckResult:
    code: FlagCode = "disqualified_director"
    if ctx.disqualified is None:
        return _CheckResult(
            code, None, "Клиент реестра дисквалифицированных лиц не подключён.", None
        )
    if not hit.inn:
        return _CheckResult(code, None, "ИНН контрагента не определён.", None)

    # Ищем по ИНН организации: реестр ФНС хранит связку «человек ↔ организация»,
    # и именно организация — предмет проверки. Поиск по ФИО без ИНН даёт
    # однофамильцев, поэтому как основной путь не используется.
    records = await ctx.disqualified.search_by_company_inn(hit.inn)
    if not records:
        return _CheckResult(code, None, None, None)

    active = [r for r in records if r.is_active]
    if not active:
        return _CheckResult(
            code,
            FlagDetail(
                code=code,
                level="medium",
                message_ru=(
                    "В управлении контрагента есть лицо с истёкшей дисквалификацией "
                    "(реестр ФНС). Срок наказания закончился, но факт нарушения "
                    "зафиксирован."
                ),
                details={
                    "matches_count": len(records),
                    "active_disqualifications": 0,
                    "match_precision": "exact_company_inn",
                    "source": "service.nalog.ru/disqualified",
                },
            ),
            None,
            None,
        )

    first = active[0]
    return _CheckResult(
        code,
        FlagDetail(
            code=code,
            level="high",
            message_ru=(
                f"В управлении контрагента — дисквалифицированное лицо "
                f"({first.full_name or 'ФИО не указано'}, "
                f"{(first.position or 'должность не указана').lower()}), "
                f"действующих записей: {len(active)}. Заключение договора с "
                f"компанией под его управлением запрещено законом."
            ),
            details={
                "matches_count": len(records),
                "active_disqualifications": len(active),
                "person_name": first.full_name,
                "position": first.position,
                "disqualification_from": first.disqualification_from,
                "disqualification_until": first.disqualification_until,
                "reason": first.reason,
                "match_precision": "exact_company_inn",
                "source": "service.nalog.ru/disqualified",
            },
        ),
        None,
        None,
    )


async def check_bankruptcy_records(
    ctx: ServiceContext, hit: EgrulSearchHit
) -> _CheckResult:
    code: FlagCode = "bankruptcy_records"
    if not hit.inn:
        return _CheckResult(code, None, "ИНН контрагента не определён.", None)

    cases = await ctx.efrsb.search_active_by_inn(hit.inn)
    if not cases:
        return _CheckResult(code, None, None, None)

    bankruptcy_stages = {"bankruptcy_proceedings"}
    has_terminal = any(c.stage in bankruptcy_stages for c in cases)
    level: RiskLevel = "high" if has_terminal else "medium"

    stages = [c.stage_ru for c in cases if c.stage_ru]
    return _CheckResult(
        code,
        FlagDetail(
            code=code,
            level=level,
            message_ru=(
                f"В ЕФРСБ найдено активных дел о банкротстве: {len(cases)}. "
                f"Текущие стадии: {', '.join(stages) if stages else '(не указано)'}."
            ),
            details={
                "cases_count": len(cases),
                "stages": stages,
                "case_numbers": [c.case_number for c in cases if c.case_number],
            },
        ),
        None,
        None,
    )


# --- расширенные проверки (S4) ---


async def check_tax_debts(ctx: ServiceContext, hit: EgrulSearchHit) -> _CheckResult:
    """Задолженность по налогам — из набора открытых данных ФНС `debtam`.

    Раньше признак пытались взять из «Прозрачного бизнеса»; в его ответе такого
    поля нет вообще, поэтому проверка всегда отвечала «долгов нет». Источник
    заменён на `debtam` (таблица `tax_debts` локальных реестров).
    """
    code: FlagCode = "tax_debts"
    if ctx.registries is None:
        return _CheckResult(code, None, "Реестр налоговой задолженности не подключён.", None)
    gated = await _registry_source_gate(
        ctx, code, "tax_debts", "налоговой задолженности (debtam)"
    )
    if gated is not None:
        return gated
    if not hit.inn:
        return _CheckResult(code, None, "ИНН контрагента не определён.", None)

    match = await ctx.registries.lookup_tax_debt(hit.inn)
    if match is None:
        return await _registry_negative_result(
            ctx, code, "tax_debts", "налоговой задолженности (debtam)"
        )

    data_as_of = await _registry_data_note(ctx, "tax_debts")
    total = match.total_debt_rub + match.penalty_rub + match.fine_rub
    if total >= 1_000_000:
        level: RiskLevel = "high"
    elif total >= 100_000:
        level = "medium"
    else:
        level = "low"

    top_taxes = sorted(
        (t for t in match.taxes if isinstance(t, dict)),
        key=lambda t: float(t.get("debt") or 0),
        reverse=True,
    )[:5]
    return _CheckResult(
        code,
        FlagDetail(
            code=code,
            level=level,
            message_ru=(
                f"ФНС публикует задолженность контрагента: недоимка "
                f"{_rub(match.total_debt_rub)}, пени {_rub(match.penalty_rub)}, "
                f"штрафы {_rub(match.fine_rub)}; всего {_rub(total)}"
                + (f" (данные ФНС от {data_as_of})" if data_as_of else "")
                + "."
            ),
            details={
                "source": "ФНС, открытые данные «debtam»",
                "org_name": match.org_name,
                "debt_rub": match.total_debt_rub,
                "penalty_rub": match.penalty_rub,
                "fine_rub": match.fine_rub,
                "total_rub": total,
                "top_taxes": top_taxes,
                "registry_data_as_of": data_as_of,
            },
        ),
        None,
        None,
    )


async def check_enforcement_proceedings(
    ctx: ServiceContext, hit: EgrulSearchHit
) -> _CheckResult:
    """Проверка открытых исполнительных производств в Банке данных ФССП."""
    code: FlagCode = "enforcement_proceedings"
    if ctx.fssp is None:
        return _CheckResult(code, None, "Клиент ФССП не подключён.", None)
    if not hit.inn:
        return _CheckResult(code, None, "ИНН контрагента не определён.", None)

    cases = await ctx.fssp.search_proceedings_by_inn(hit.inn)
    if not cases:
        return _CheckResult(code, None, None, None)

    total_amount = sum((c.debt_amount_rub or 0.0) for c in cases)
    n = len(cases)
    if n >= 5 or total_amount >= 1_000_000:
        level: RiskLevel = "high"
    elif n >= 2 or total_amount >= 100_000:
        level = "medium"
    else:
        level = "low"

    return _CheckResult(
        code,
        FlagDetail(
            code=code,
            level=level,
            message_ru=(
                f"В Банке данных исполнительных производств ФССП найдено "
                f"открытых производств: {n}"
                + (
                    f", совокупная сумма требований ≈ {int(total_amount):,} ₽".replace(",", " ")
                    if total_amount > 0
                    else ""
                )
                + "."
            ),
            details={
                "cases_count": n,
                "total_debt_rub": total_amount or None,
                "case_numbers": [c.case_number for c in cases if c.case_number][:10],
            },
        ),
        None,
        None,
    )


async def check_active_lawsuits(
    ctx: ServiceContext,
    hit: EgrulSearchHit,
    *,
    threshold_rub: float | None = None,
) -> _CheckResult:
    """Проверка активных арбитражных дел (КАД), где контрагент — ответчик.

    Если задан `threshold_rub` — учитываются только дела с суммой ≥ порога
    (дела без явной суммы пропускаются для целей фильтра, но попадают в details).
    """
    code: FlagCode = "active_lawsuits"
    if ctx.kad is None:
        return _CheckResult(code, None, "Клиент КАД не подключён.", None)
    if not hit.inn:
        return _CheckResult(code, None, "ИНН контрагента не определён.", None)

    cases = await ctx.kad.search_active_lawsuits_by_inn(hit.inn)
    if not cases:
        return _CheckResult(code, None, None, None)

    if threshold_rub is not None:
        filtered = [c for c in cases if (c.amount_rub or 0.0) >= threshold_rub]
    else:
        filtered = cases

    if not filtered:
        return _CheckResult(
            code,
            None,
            (
                f"Найдено активных арбитражных дел: {len(cases)}, но ни одно "
                f"не превышает порог {int(threshold_rub or 0):,} ₽.".replace(",", " ")
            ),
            None,
        )

    n = len(filtered)
    total = sum((c.amount_rub or 0.0) for c in filtered)
    if n >= 5 or total >= 5_000_000:
        level: RiskLevel = "high"
    elif n >= 2 or total >= 500_000:
        level = "medium"
    else:
        level = "low"

    return _CheckResult(
        code,
        FlagDetail(
            code=code,
            level=level,
            message_ru=(
                f"В КАД найдено активных арбитражных дел против контрагента "
                f"(в роли ответчика): {n}"
                + (
                    f", суммарные требования ≈ {int(total):,} ₽".replace(",", " ")
                    if total > 0
                    else ""
                )
                + "."
            ),
            details={
                "cases_count": n,
                "total_amount_rub": total or None,
                "threshold_rub": threshold_rub,
                "case_numbers": [c.case_number for c in filtered if c.case_number][:10],
            },
        ),
        None,
        None,
    )


# --- утилиты ---


def _rub(value: float) -> str:
    """Рубли в читаемом виде: 1234567.89 → «1 234 568 ₽»."""
    return f"{int(round(value)):,} ₽".replace(",", " ")


def _mask_personal_inn(inn: str) -> str:
    inn = inn.strip()
    if len(inn) == 12 and inn.isdigit():
        return f"{inn[:3]}*****{inn[10:]}"
    return inn


def _level_from_score(score: int, coverage: CheckCoverage) -> OverallRiskLevel:
    """Итоговый уровень риска с учётом того, все ли проверки отработали.

    «Низкий риск» разрешён только когда ответили ВСЕ проверки. Если хоть одна
    промолчала, уровень — «не определён»: сказать «низкий», не проверив половину
    источников, значит подтолкнуть клиента к сделке на пустоте.
    Найденный риск при этом никуда не девается: high/medium выставляются
    независимо от полноты покрытия.
    """
    if score >= 50:
        return "high"
    if score >= 25:
        return "medium"
    if coverage.is_complete:
        return "low"
    return "unknown"


def _summarise(
    flags: list[FlagDetail], level: OverallRiskLevel, coverage: CheckCoverage
) -> str:
    parts: list[str] = []
    if flags:
        high = sum(1 for f in flags if f.level == "high")
        medium = sum(1 for f in flags if f.level == "medium")
        low = sum(1 for f in flags if f.level == "low")
        descr: list[str] = []
        if high:
            descr.append(f"{high} высоких")
        if medium:
            descr.append(f"{medium} средних")
        if low:
            descr.append(f"{low} низких")
        parts.append(f"Найдено флагов: {', '.join(descr)}.")

    parts.append(
        f"Проверок выполнено: {coverage.answered} из {coverage.total}."
        if coverage.total
        else "Проверки не выполнялись."
    )

    if level == "unknown":
        parts.append(
            "Итоговый уровень риска не определён: часть проверок не отработала, "
            "поэтому «низкий риск» показывать нельзя."
        )
    else:
        parts.append(f"Итоговый уровень риска: {_RU_LEVEL[level]}.")

    if coverage.failed:
        parts.append(f"Не удалось выполнить проверок: {coverage.failed} (см. errors).")
    if coverage.skipped:
        parts.append(
            f"Проверок пропущено: {coverage.skipped} (нет входных данных, см. checks_skipped)."
        )
    if level == "low":
        parts.append("Все источники ответили, риск-флагов нет.")
    return " ".join(parts)


_RU_LEVEL: dict[OverallRiskLevel, str] = {
    "low": "низкий",
    "medium": "средний",
    "high": "высокий",
    "unknown": "не определён",
}


# --- публичная точка входа ---


async def check_for_red_flags(
    ctx: ServiceContext,
    inn: str,
    *,
    include_extended: bool = False,
    lawsuits_threshold_rub: float | None = None,
    address: str | None = None,
) -> RedFlagsReport:
    """Полный отчёт по риск-флагам контрагента.

    Аргументы:
        inn: ИНН контрагента (10 или 12 цифр).
        include_extended: добавить 3 расширенные проверки (налоговые долги из
            открытых данных ФНС, ФССП, КАД). По умолчанию False — выполняются
            только базовые 4 проверки на основе ЕГРЮЛ + ЕФРСБ + локальных
            реестров.
        lawsuits_threshold_rub: порог фильтра для активных арбитражных дел
            (рубли). Если не задан — учитываются все дела где контрагент
            ответчик. Полезно для отсечения мелких споров.
        address: юридический адрес контрагента для проверки по реестру
            массовых адресов. Поиск ЕГРЮЛ адрес не отдаёт, поэтому без
            этого аргумента проверка `mass_address` честно помечается
            невыполненной, а не «чисто».
    """
    if not is_valid_inn(inn):
        raise ValidationError(f"Невалидный ИНН '{inn}'", details={"input": inn})

    hit, _ = await _fetch_hit(ctx, by="inn", value=inn)

    base_coros = [
        check_mass_address(ctx, hit, address=address),
        check_mass_director(ctx, hit),
        check_disqualified_director(ctx, hit),
        check_bankruptcy_records(ctx, hit),
    ]
    expected_codes: list[FlagCode] = [
        "mass_address",
        "mass_director",
        "disqualified_director",
        "bankruptcy_records",
    ]

    if include_extended:
        base_coros.extend(
            [
                check_tax_debts(ctx, hit),
                check_enforcement_proceedings(ctx, hit),
                check_active_lawsuits(ctx, hit, threshold_rub=lawsuits_threshold_rub),
            ]
        )
        expected_codes.extend(
            [
                "tax_debts",
                "enforcement_proceedings",
                "active_lawsuits",
            ]
        )

    raw_results = await asyncio.gather(*base_coros, return_exceptions=True)

    flags: list[FlagDetail] = []
    passed: list[FlagCode] = []
    skipped: list[FlagCode] = []
    errors: list[CheckError] = []
    score = 0

    for code, result in zip(expected_codes, raw_results, strict=True):
        if isinstance(result, BaseException):
            err_msg = str(result)
            if isinstance(result, McpFnsError):
                err_ru = result.message_ru
            else:
                err_ru = f"Не удалось выполнить проверку '{code}': {err_msg}"
            errors.append(CheckError(code=code, error=err_msg, message_ru=err_ru))
            continue
        # _CheckResult
        if result.error is not None:
            errors.append(result.error)
            continue
        if result.flag is not None:
            flags.append(result.flag)
            score += FLAG_WEIGHTS[code][result.flag.level]
            continue
        if result.skipped_reason is not None:
            skipped.append(code)
            continue
        passed.append(code)

    score = min(score, 100)
    coverage = CheckCoverage(
        total=len(expected_codes),
        answered=len(flags) + len(passed),
        flagged=len(flags),
        clean=len(passed),
        skipped=len(skipped),
        failed=len(errors),
    )
    overall = _level_from_score(score, coverage)
    return RedFlagsReport(
        inn=inn,
        overall_risk_level=overall,
        overall_risk_score=score,
        summary_ru=_summarise(flags, overall, coverage),
        flags=flags,
        checks_passed=passed,
        checks_skipped=skipped,
        errors=errors,
        coverage=coverage,
        checked_at=datetime.now(tz=timezone.utc),
    )
