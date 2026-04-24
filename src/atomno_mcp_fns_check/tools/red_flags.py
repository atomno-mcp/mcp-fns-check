"""check_for_red_flags — агрегатор риск-флагов по контрагенту.

S4: реализованы все 8 проверок из SPEC §3.5:

  Базовые (S3):
    * `mass_address`            — адрес из реестра массовой регистрации ФНС;
    * `mass_director`           — руководитель с ≥ 10 действующих юр.лиц;
    * `disqualified_director`   — руководитель в реестре дисквалифицированных;
    * `bankruptcy_records`      — активные дела в ЕФРСБ.

  Расширенные (S4, включаются `include_extended=True`):
    * `tax_debts`               — индикатор задолженности по налогам
                                  (Прозрачный бизнес ФНС);
    * `no_reporting`            — отсутствие налоговой отчётности
                                  более года (Прозрачный бизнес ФНС);
    * `enforcement_proceedings` — открытые исполнительные производства
                                  в Банке данных ФССП;
    * `active_lawsuits`         — активные арбитражные дела (КАД), в которых
                                  контрагент — ответчик. Опц.фильтр по сумме
                                  иска через `lawsuits_threshold_rub`.

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
from ..errors import McpFnsError, ValidationError
from ..schemas import (
    CheckError,
    FlagCode,
    FlagDetail,
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
    "no_reporting": {"low": 10, "medium": 20, "high": 30},
    "enforcement_proceedings": {"low": 5, "medium": 15, "high": 25},
    "active_lawsuits": {"low": 5, "medium": 15, "high": 25},
}


@dataclass(slots=True)
class _CheckResult:
    code: FlagCode
    flag: FlagDetail | None
    skipped_reason: str | None
    error: CheckError | None


# --- 4 проверки ---


async def check_mass_address(ctx: ServiceContext, hit: EgrulSearchHit) -> _CheckResult:
    code: FlagCode = "mass_address"
    if ctx.registries is None:
        return _CheckResult(code, None, "Реестр массовых адресов не подключён.", None)
    if not hit.address:
        return _CheckResult(code, None, "Адрес контрагента отсутствует в карточке ЕГРЮЛ.", None)

    match = await ctx.registries.lookup_mass_address(hit.address)
    if match is None:
        return _CheckResult(code, None, None, None)

    count = match.registered_entities_count
    level: RiskLevel = "high" if count >= 30 else "medium" if count >= 5 else "low"
    return _CheckResult(
        code,
        FlagDetail(
            code=code,
            level=level,
            message_ru=(
                f"Адрес контрагента включён ФНС в реестр массовой регистрации: "
                f"по этому адресу зарегистрировано {count} юр.лиц."
            ),
            details={
                "address_raw": match.address_raw,
                "registered_entities_count": count,
                "fns_inclusion_date": match.fns_inclusion_date,
            },
        ),
        None,
        None,
    )


async def check_mass_director(ctx: ServiceContext, hit: EgrulSearchHit) -> _CheckResult:
    code: FlagCode = "mass_director"
    if ctx.registries is None:
        return _CheckResult(code, None, "Реестр массовых руководителей не подключён.", None)

    director_inn: str | None = None
    raw = hit.raw or {}
    for key in ("director_inn", "directorInn", "managerInn", "g_inn", "gi"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            director_inn = value.strip()
            break

    director_name = (hit.director_name or "").strip()

    if director_inn:
        match = await ctx.registries.lookup_mass_director(director_inn)
        if match is None:
            return _CheckResult(code, None, None, None)
        count = match.companies_count
        level: RiskLevel = "high" if count >= 10 else "medium" if count >= 5 else "low"
        return _CheckResult(
            code,
            FlagDetail(
                code=code,
                level=level,
                message_ru=(
                    f"Руководитель {match.full_name or '(без ФИО)'} включён в реестр "
                    f"массовых руководителей ФНС: {count} действующих компаний."
                ),
                details={
                    "director_inn_masked": _mask_personal_inn(director_inn),
                    "companies_count": count,
                    "match_precision": "exact_inn",
                    "fns_inclusion_date": match.fns_inclusion_date,
                },
            ),
            None,
            None,
        )

    if director_name:
        match = await ctx.registries.lookup_mass_director(full_name=director_name)
        if match is None:
            return _CheckResult(code, None, None, None)
        count = match.companies_count
        level = "high" if count >= 10 else "medium" if count >= 5 else "low"
        return _CheckResult(
            code,
            FlagDetail(
                code=code,
                level=level,
                message_ru=(
                    f"Совпадение по ФИО руководителя ({director_name}) с реестром "
                    f"массовых руководителей ФНС: {count} компаний. "
                    f"Совпадение приблизительное (без ИНН), требует ручной верификации."
                ),
                details={
                    "director_name": director_name,
                    "companies_count": count,
                    "match_precision": "approximate_name_only",
                },
            ),
            None,
            None,
        )

    return _CheckResult(
        code,
        None,
        "ИНН и ФИО руководителя отсутствуют в карточке ЕГРЮЛ.",
        None,
    )


async def check_disqualified_director(
    ctx: ServiceContext, hit: EgrulSearchHit
) -> _CheckResult:
    code: FlagCode = "disqualified_director"
    if ctx.registries is None:
        return _CheckResult(code, None, "Реестр дисквалифицированных не подключён.", None)

    director_inn: str | None = None
    raw = hit.raw or {}
    for key in ("director_inn", "directorInn", "managerInn", "g_inn", "gi"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            director_inn = value.strip()
            break

    director_name = (hit.director_name or "").strip() or None

    if not director_inn and not director_name:
        return _CheckResult(
            code,
            None,
            "ИНН и ФИО руководителя отсутствуют в карточке ЕГРЮЛ.",
            None,
        )

    matches = await ctx.registries.lookup_disqualified(
        inn=director_inn,
        full_name=director_name,
    )
    if not matches:
        return _CheckResult(code, None, None, None)

    today = datetime.now(tz=timezone.utc).date().isoformat()
    active = [m for m in matches if not m.disqualification_until or m.disqualification_until >= today]

    if not active:
        return _CheckResult(
            code,
            FlagDetail(
                code=code,
                level="medium",
                message_ru=(
                    "У руководителя есть истёкшая дисквалификация в реестре ФНС. "
                    "Срок наказания закончился, но факт нарушения зафиксирован."
                ),
                details={
                    "matches_count": len(matches),
                    "active_disqualifications": 0,
                    "match_precision": "exact_inn" if director_inn else "approximate_name_only",
                },
            ),
            None,
            None,
        )

    return _CheckResult(
        code,
        FlagDetail(
            code=code,
            level="high",
            message_ru=(
                f"Руководитель находится в реестре дисквалифицированных лиц ФНС "
                f"({len(active)} активных дисквалификаций). Заключение договора "
                f"с компанией под его управлением запрещено законом."
            ),
            details={
                "matches_count": len(matches),
                "active_disqualifications": len(active),
                "first_disqualification_date": active[0].disqualification_date,
                "first_disqualification_until": active[0].disqualification_until,
                "match_precision": "exact_inn" if director_inn else "approximate_name_only",
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
    """Проверка наличия задолженности по налогам через Прозрачный бизнес ФНС."""
    code: FlagCode = "tax_debts"
    if ctx.pb_fns is None:
        return _CheckResult(code, None, "Клиент Прозрачного бизнеса не подключён.", None)
    if not hit.inn:
        return _CheckResult(code, None, "ИНН контрагента не определён.", None)

    tags = await ctx.pb_fns.get_tags_by_inn(hit.inn)
    if not tags.has_tax_debt:
        return _CheckResult(code, None, None, None)

    return _CheckResult(
        code,
        FlagDetail(
            code=code,
            level="medium",
            message_ru=(
                "Прозрачный бизнес ФНС сообщает о задолженности контрагента "
                "по уплате налогов и сборов. Точная сумма доступна на pb.nalog.ru."
            ),
            details={
                "source": "pb.nalog.ru",
                "indicator_present": True,
            },
        ),
        None,
        None,
    )


async def check_no_reporting(ctx: ServiceContext, hit: EgrulSearchHit) -> _CheckResult:
    """Проверка непредставления налоговой отчётности > 1 года (Прозрачный бизнес)."""
    code: FlagCode = "no_reporting"
    if ctx.pb_fns is None:
        return _CheckResult(code, None, "Клиент Прозрачного бизнеса не подключён.", None)
    if not hit.inn:
        return _CheckResult(code, None, "ИНН контрагента не определён.", None)

    tags = await ctx.pb_fns.get_tags_by_inn(hit.inn)
    if not tags.has_no_reporting:
        return _CheckResult(code, None, None, None)

    return _CheckResult(
        code,
        FlagDetail(
            code=code,
            level="high",
            message_ru=(
                "По данным Прозрачного бизнеса ФНС контрагент не представляет "
                "налоговую отчётность более одного года. Это серьёзный признак "
                "технической / неработающей компании."
            ),
            details={
                "source": "pb.nalog.ru",
                "indicator_present": True,
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


def _mask_personal_inn(inn: str) -> str:
    inn = inn.strip()
    if len(inn) == 12 and inn.isdigit():
        return f"{inn[:3]}*****{inn[10:]}"
    return inn


def _level_from_score(score: int) -> RiskLevel:
    if score >= 50:
        return "high"
    if score >= 25:
        return "medium"
    return "low"


def _summarise(flags: list[FlagDetail], level: RiskLevel, errors: int, skipped: int) -> str:
    if not flags and errors == 0 and skipped == 0:
        return "Риск-флагов не обнаружено по проверенным источникам."
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
    parts.append(f"Итоговый уровень риска: {_RU_LEVEL[level]}.")
    if errors:
        parts.append(f"Не удалось выполнить проверок: {errors} (см. errors).")
    if skipped:
        parts.append(f"Проверок пропущено: {skipped} (нет данных, см. checks_skipped).")
    return " ".join(parts)


_RU_LEVEL: dict[RiskLevel, str] = {
    "low": "низкий",
    "medium": "средний",
    "high": "высокий",
}


# --- публичная точка входа ---


async def check_for_red_flags(
    ctx: ServiceContext,
    inn: str,
    *,
    include_extended: bool = False,
    lawsuits_threshold_rub: float | None = None,
) -> RedFlagsReport:
    """Полный отчёт по риск-флагам контрагента.

    Аргументы:
        inn: ИНН контрагента (10 или 12 цифр).
        include_extended: добавить 4 расширенные проверки (Прозрачный бизнес,
            ФССП, КАД). По умолчанию False — выполняются только базовые 4
            проверки на основе ЕГРЮЛ + ЕФРСБ + локальных реестров.
        lawsuits_threshold_rub: порог фильтра для активных арбитражных дел
            (рубли). Если не задан — учитываются все дела где контрагент
            ответчик. Полезно для отсечения мелких споров.
    """
    if not is_valid_inn(inn):
        raise ValidationError(f"Невалидный ИНН '{inn}'", details={"input": inn})

    hit, _ = await _fetch_hit(ctx, by="inn", value=inn)

    base_coros = [
        check_mass_address(ctx, hit),
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
                check_no_reporting(ctx, hit),
                check_enforcement_proceedings(ctx, hit),
                check_active_lawsuits(ctx, hit, threshold_rub=lawsuits_threshold_rub),
            ]
        )
        expected_codes.extend(
            [
                "tax_debts",
                "no_reporting",
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
    overall = _level_from_score(score)
    return RedFlagsReport(
        inn=inn,
        overall_risk_level=overall,
        overall_risk_score=score,
        summary_ru=_summarise(flags, overall, len(errors), len(skipped)),
        flags=flags,
        checks_passed=passed,
        checks_skipped=skipped,
        errors=errors,
        checked_at=datetime.now(tz=timezone.utc),
    )
