"""check_contractor — главный агрегирующий тул (SPEC v0.1 §4.1, §6.2).

Собирает полную картину по контрагенту из уже реализованных тулов:

    1. `check_inn` / `check_ogrn` — базовая карточка ЕГРЮЛ (`CounterpartyCard`).
    2. `get_legal_status`        — агрегированный жизненный статус с обогащением
                                   через ЕФРСБ.
    3. `check_for_red_flags`     — до 8 проверок рисков (SPEC §3.5).

Поверх собирается `verdict_action` ∈ {safe_to_proceed, manual_review_required,
high_risk_do_not_proceed, impossible_contractor_defunct} — **детерминированно**
по правилам ниже, без «эвристик» и скрытых подстановок:

  - `impossible_contractor_defunct`:
      status ∈ {liquidated, excluded_inactive} — сделка невозможна.
  - `high_risk_do_not_proceed`:
      status == bankruptcy, ИЛИ risks.overall_risk_level == "high",
      ИЛИ есть flag disqualified_director активный — блокирующее обстоятельство.
  - `manual_review_required`:
      risks.overall_risk_level == "medium", ИЛИ status ∈ {liquidating,
      reorganizing}, ИЛИ risks.errors непустой (часть источников не ответила).
  - `safe_to_proceed`:
      status == "active", risks.overall_risk_level == "low", errors пусты.

Рекомендации (`recommendations`) — детерминированный list[str], сгенерированный
из вердикта + по таблице на каждый FlagCode. Никакой LLM-генерации в open-tier.

Источники, не ответившие на запрос, попадают в `risks.errors[]`, а их названия
логируются в `sources.sources_queried` рядом с ответившими — чтобы клиент видел
полную картину, что именно спросили и что именно пришло.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from ..context import ServiceContext
from ..errors import ValidationError
from ..schemas import (
    ContractorReport,
    ContractorSources,
    ContractorTier,
    CounterpartyCard,
    FlagCode,
    IdentifierType,
    LegalStatusReport,
    RedFlagsReport,
    VerdictAction,
)
from ..validators import is_valid_inn, is_valid_ogrn
from .cards import hit_to_card
from .check import _fetch_hit  # noqa: PLC2701 — переиспользуем внутренний путь кэширования
from .legal_status import get_legal_status
from .red_flags import check_for_red_flags

IdentifierChannel = Literal["inn", "ogrn"]

RECOMMENDATIONS_BY_FLAG: dict[FlagCode, str] = {
    "mass_address": (
        "Юр.адрес включён ФНС в реестр массовой регистрации. Запросите у "
        "контрагента договор аренды / свидетельство о собственности на "
        "помещение и убедитесь в фактическом присутствии до подписания."
    ),
    "mass_director": (
        "Руководитель числится в реестре массовых руководителей ФНС (≥10 "
        "действующих юр.лиц). Для существенной сделки запросите заверенную "
        "копию приказа о назначении и расширенную выписку по руководителю."
    ),
    "disqualified_director": (
        "Руководитель находится в реестре дисквалифицированных лиц ФНС. "
        "Заключение договора с компанией под его управлением запрещено "
        "законом — требуется смена единоличного исполнительного органа."
    ),
    "bankruptcy_records": (
        "В ЕФРСБ есть активные дела о банкротстве контрагента. Любая сделка "
        "должна быть согласована с арбитражным управляющим; платежи вне "
        "процедуры могут быть оспорены."
    ),
    "tax_debts": (
        "Прозрачный бизнес ФНС фиксирует налоговую задолженность. Рекомендуется "
        "запросить справку из ИФНС об отсутствии задолженности (форма КНД "
        "1120101) перед подписанием договора."
    ),
    "no_reporting": (
        "Контрагент не сдаёт налоговую отчётность более года. Сделка с такой "
        "компанией — повышенный риск отказа в вычете НДС (ст. 54.1 НК РФ). "
        "Не рекомендуется принимать как первичного поставщика."
    ),
    "enforcement_proceedings": (
        "У контрагента открыты исполнительные производства в ФССП. Учтите "
        "возможность ареста счетов и долгов; при оплате авансом риск потерять "
        "средства выше среднего."
    ),
    "active_lawsuits": (
        "Контрагент участвует как ответчик в активных арбитражных делах. "
        "Запросите позицию контрагента по самым крупным искам и оцените "
        "влияние возможных взысканий на его платёжеспособность."
    ),
}


def _detect_channel(identifier: str) -> IdentifierChannel:
    """Определить тип идентификатора (ИНН vs ОГРН) по длине и валидности.

    Поднимает `ValidationError` если идентификатор не соответствует ни одному
    из форматов ФНС (10/12 для ИНН, 13/15 для ОГРН с корректной контрольной
    цифрой). Никаких «угадываний» по намекам — только строгая валидация.
    """
    if not isinstance(identifier, str):
        raise ValidationError(
            "Идентификатор контрагента должен быть строкой.",
            details={"input": identifier},
        )
    value = identifier.strip()
    length = len(value)
    if length in (10, 12):
        if not is_valid_inn(value):
            raise ValidationError(
                f"Невалидный ИНН '{value}': не сходится контрольная цифра.",
                details={"input": value, "expected_length": [10, 12]},
            )
        return "inn"
    if length in (13, 15):
        if not is_valid_ogrn(value):
            raise ValidationError(
                f"Невалидный ОГРН '{value}': не сходится контрольная цифра.",
                details={"input": value, "expected_length": [13, 15]},
            )
        return "ogrn"
    raise ValidationError(
        (
            f"Идентификатор '{value}' длиной {length} не является ни ИНН "
            f"(10 или 12 цифр), ни ОГРН/ОГРНИП (13 или 15 цифр)."
        ),
        details={"input": value, "length": length, "expected_length": [10, 12, 13, 15]},
    )


def _derive_verdict(
    status: LegalStatusReport, risks: RedFlagsReport
) -> tuple[VerdictAction, str]:
    """Детерминированный вердикт + краткое обоснование на русском.

    Порядок проверок важен — самая «тяжёлая» причина побеждает первой.
    """
    if status.status in ("liquidated", "excluded_inactive"):
        return (
            "impossible_contractor_defunct",
            f"Контрагент имеет статус «{status.status_label_ru}». "
            "Заключение сделки невозможно.",
        )

    disqualified_active = any(
        f.code == "disqualified_director" and f.level == "high" for f in risks.flags
    )
    if disqualified_active:
        return (
            "high_risk_do_not_proceed",
            "Руководитель контрагента находится в реестре дисквалифицированных "
            "лиц ФНС — заключение договора запрещено законом.",
        )

    if status.status == "bankruptcy":
        stage = status.bankruptcy_stage or "стадия не указана"
        return (
            "high_risk_do_not_proceed",
            (
                f"Контрагент находится в процедуре банкротства ({stage}). "
                "Сделка возможна только через арбитражного управляющего."
            ),
        )

    if risks.overall_risk_level == "high":
        return (
            "high_risk_do_not_proceed",
            (
                f"Агрегированный уровень риска — высокий "
                f"(score {risks.overall_risk_score}/100). "
                "Без устранения причин сделка не рекомендуется."
            ),
        )

    if risks.overall_risk_level == "medium" or status.status in (
        "liquidating",
        "reorganizing",
    ):
        return (
            "manual_review_required",
            (
                f"Требуется ручная проверка: статус «{status.status_label_ru}», "
                f"уровень риска — {risks.overall_risk_level} "
                f"(score {risks.overall_risk_score}/100)."
            ),
        )

    if risks.errors:
        return (
            "manual_review_required",
            (
                f"Часть источников не ответила ({len(risks.errors)} из "
                f"{len(risks.errors) + len(risks.checks_passed) + len(risks.flags) + len(risks.checks_skipped)}). "
                "Ручная верификация неответивших источников обязательна."
            ),
        )

    return (
        "safe_to_proceed",
        (
            f"Статус «{status.status_label_ru}», уровень риска — "
            f"{risks.overall_risk_level} (score {risks.overall_risk_score}/100). "
            "Препятствий к заключению сделки по открытым источникам не найдено."
        ),
    )


def _build_recommendations(
    verdict: VerdictAction,
    status: LegalStatusReport,
    risks: RedFlagsReport,
) -> list[str]:
    """Сформировать список рекомендаций для агента.

    Детерминированный порядок:
      1. Критическая рекомендация по action (если impossible / high_risk).
      2. Рекомендации по каждому сработавшему флагу (в порядке level: high→low).
      3. Если есть errors — явно попросить повторить проверку.
      4. Если safe_to_proceed — финальная подтверждающая рекомендация.
    """
    items: list[str] = []

    if verdict == "impossible_contractor_defunct":
        items.append(
            "Контрагент юридически прекратил существование — сделку заключать "
            "нельзя. Проверьте актуальные реквизиты и найдите действующего "
            "контрагента."
        )
    elif verdict == "high_risk_do_not_proceed":
        items.append(
            "Уровень риска — высокий. Не рекомендуется заключать договор без "
            "устранения причин, перечисленных ниже."
        )
    elif verdict == "manual_review_required":
        items.append(
            "Требуется ручная проверка — сверка с оригинальными документами "
            "контрагента и/или повтор проверки после восстановления источников."
        )

    sorted_flags = sorted(
        risks.flags,
        key=lambda f: {"high": 0, "medium": 1, "low": 2}[f.level],
    )
    for flag in sorted_flags:
        recommendation = RECOMMENDATIONS_BY_FLAG.get(flag.code)
        if recommendation is not None:
            items.append(recommendation)

    if risks.errors:
        failed_codes = ", ".join(e.code for e in risks.errors)
        items.append(
            f"Источники не ответили: {failed_codes}. Повторите проверку "
            "позже или используйте hosted Pro-tier с автоматическими retry."
        )

    if status.warnings:
        items.extend(f"Предупреждение: {w}" for w in status.warnings)

    if verdict == "safe_to_proceed" and not items:
        items.append(
            "По открытым источникам препятствий к заключению сделки не "
            "обнаружено. Соблюдайте стандартные меры должной осмотрительности "
            "(ст. 54.1 НК РФ): копия устава, приказ на руководителя, договор."
        )

    return items


def _collect_sources_meta(
    card: CounterpartyCard,
    status: LegalStatusReport,
    risks: RedFlagsReport,
    *,
    checked_at: datetime,
) -> ContractorSources:
    """Собрать секцию `sources` из отчётов подкомпонентов.

    Единственный факт, который можно достоверно зафиксировать — что источник
    был опрошен в текущей проверке (по наличию его названия в sources_checked
    и по отсутствию/наличию ошибок в risks.errors). Даты extract — это даты
    сегодняшнего запроса (не даты генерации самих реестров ФНС), если не
    приходят явно из источника.
    """
    today = checked_at.date()
    sources_queried: set[str] = set(status.sources_checked)

    egrul_extract_date = card.data_source_meta.egrul_extract_date or today
    if "egrul" in sources_queried:
        egrul_extract_date = today

    efrsb_check_date = None
    if "efrsb" in sources_queried:
        efrsb_check_date = today
        sources_queried.add("efrsb")

    errored_codes = {e.code for e in risks.errors}
    checked_codes = set(risks.checks_passed) | {f.code for f in risks.flags} | set(risks.checks_skipped)

    pb_checked = any(c in checked_codes for c in ("tax_debts", "no_reporting"))
    pb_errored = any(c in errored_codes for c in ("tax_debts", "no_reporting"))
    pb_fns_check_date = today if (pb_checked or pb_errored) else None
    if pb_fns_check_date is not None:
        sources_queried.add("pb_fns")

    fssp_checked = "enforcement_proceedings" in checked_codes
    fssp_errored = "enforcement_proceedings" in errored_codes
    fssp_check_date = today if (fssp_checked or fssp_errored) else None
    if fssp_check_date is not None:
        sources_queried.add("fssp")

    kad_checked = "active_lawsuits" in checked_codes
    kad_errored = "active_lawsuits" in errored_codes
    kad_check_date = today if (kad_checked or kad_errored) else None
    if kad_check_date is not None:
        sources_queried.add("kad")

    registries_codes = {"mass_address", "mass_director", "disqualified_director"}
    registries_touched = bool(registries_codes & (checked_codes | errored_codes))
    registries_check_date = today if registries_touched else None
    if registries_check_date is not None:
        sources_queried.add("registries")

    return ContractorSources(
        egrul_extract_date=egrul_extract_date,
        efrsb_check_date=efrsb_check_date,
        pb_fns_check_date=pb_fns_check_date,
        fssp_check_date=fssp_check_date,
        kad_check_date=kad_check_date,
        registries_check_date=registries_check_date,
        cache_age_hours=card.data_source_meta.cache_age_hours,
        sources_queried=sorted(sources_queried),
    )


async def check_contractor(
    ctx: ServiceContext,
    identifier: str,
    *,
    include_extended_risks: bool = True,
    lawsuits_threshold_rub: float = 1_000_000.0,
    tier: ContractorTier = "open",
) -> ContractorReport:
    """Главный тул: полная проверка контрагента по одному идентификатору.

    Args:
        ctx: Общий ServiceContext с httpx-клиентами и SQLite-кэшем.
        identifier: ИНН (10/12 цифр) или ОГРН/ОГРНИП (13/15 цифр).
            Тип определяется строго по длине и контрольной цифре.
        include_extended_risks: Выполнить ли 4 расширенные проверки риска
            (Прозрачный бизнес, ФССП, КАД). По умолчанию True — в open-tier
            они тоже доступны, просто с более строгими rate-limit провайдеров.
        lawsuits_threshold_rub: Порог фильтрации арбитражных дел в рублях.
            Дела меньше порога не учитываются в риск-скоре. По умолчанию
            1 000 000 ₽ — отсекает мелкие потребительские споры.
        tier: Какой tier записать в отчёт. Client (этот пакет) всегда отдаёт
            "open"; значения "free"/"pro" используются hosted-бэкендом.

    Returns:
        ContractorReport: Агрегированный отчёт со всеми 5 секциями
        (card, legal_status, risks, verdict, recommendations) и метаданными
        источников.

    Raises:
        ValidationError: Если идентификатор не соответствует форматам ИНН/ОГРН.
        NotFoundError: Если ЕГРЮЛ вернул пустой результат по идентификатору.
        SourceUnavailableError: Если EgrulClient не смог получить данные ЕГРЮЛ
            (ЕГРЮЛ — единственный blocking-источник; остальные подмешиваются
            best-effort и их сбои складываются в risks.errors, не валят отчёт).
    """
    channel = _detect_channel(identifier)
    normalised = identifier.strip()

    hit, cache_age = await _fetch_hit(ctx, by=channel, value=normalised)
    card = hit_to_card(hit, cache_age_hours=cache_age)

    inn_for_downstream = hit.inn if hit.inn and is_valid_inn(hit.inn) else None
    if inn_for_downstream is None:
        raise ValidationError(
            (
                "ЕГРЮЛ вернул запись без валидного ИНН — дальнейшие проверки "
                "(ЕФРСБ/ФССП/КАД/Прозрачный бизнес) невозможны по ИНН. "
                "Обратитесь к расширенной выписке ЕГРЮЛ."
            ),
            details={"identifier": normalised, "egrul_inn": hit.inn},
        )

    status = await get_legal_status(
        inn=inn_for_downstream,
        egrul=ctx.egrul,
        efrsb=ctx.efrsb,
    )

    risks = await check_for_red_flags(
        ctx,
        inn_for_downstream,
        include_extended=include_extended_risks,
        lawsuits_threshold_rub=lawsuits_threshold_rub,
    )

    verdict_action, verdict_reason = _derive_verdict(status, risks)
    recommendations = _build_recommendations(verdict_action, status, risks)
    checked_at = datetime.now(tz=timezone.utc)
    sources = _collect_sources_meta(card, status, risks, checked_at=checked_at)

    ogrn_value = hit.ogrn if hit.ogrn else card.ogrn

    return ContractorReport(
        identifier=normalised,
        identifier_type=channel,
        inn=inn_for_downstream,
        ogrn=ogrn_value,
        card=card,
        legal_status=status,
        risks=risks,
        verdict_action=verdict_action,
        verdict_reason_ru=verdict_reason,
        recommendations=recommendations,
        sources=sources,
        tier=tier,
        checked_at=checked_at,
    )


__all__ = [
    "RECOMMENDATIONS_BY_FLAG",
    "check_contractor",
]
