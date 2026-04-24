"""Тул get_legal_status — текущий жизненный статус контрагента.

Алгоритм S2:
    1. Базовый статус из ЕГРЮЛ (через EgrulClient + cards.infer_status_from_hit).
    2. Если контрагент жив (active) — обогащаем через ЕФРСБ:
       найдено активное дело → status=bankruptcy + bankruptcy_stage.
    3. Если ЕФРСБ недоступен или 4xx-ошибка — продолжаем с базовым статусом
       и кладём warning. Никогда не падаем целиком из-за второстепенного источника.

Полная классификация (liquidating / reorganizing / excluded_inactive) появится
на S2.x при подключении расширенных полей выписки ЕГРЮЛ.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..errors import (
    NotFoundError,
    RateLimitedError,
    SourceUnavailableError,
    ValidationError,
)
from ..schemas import LegalStatusReport, status_label_ru
from ..sources.efrsb import EfrsbClient
from ..sources.egrul import EgrulClient
from ..validators import is_valid_inn, is_valid_ogrn
from .cards import infer_status_from_hit, parse_date


async def get_legal_status(
    *,
    inn: str | None = None,
    ogrn: str | None = None,
    egrul: EgrulClient,
    efrsb: EfrsbClient | None = None,
) -> LegalStatusReport:
    """Получить агрегированный статус контрагента.

    Минимум один из `inn` или `ogrn` обязателен. Если оба — приоритет у `inn`.
    """
    if not inn and not ogrn:
        raise ValidationError("Нужно передать хотя бы один из 'inn' или 'ogrn'.")

    if inn is not None and not is_valid_inn(inn):
        raise ValidationError(f"Невалидный ИНН '{inn}'", details={"input": inn})
    if ogrn is not None and not is_valid_ogrn(ogrn):
        raise ValidationError(f"Невалидный ОГРН '{ogrn}'", details={"input": ogrn})

    if inn:
        hits = await egrul.search_by_inn(inn)
    else:
        assert ogrn is not None
        hits = await egrul.search_by_ogrn(ogrn)

    if not hits:
        raise NotFoundError(
            "Контрагент не найден в ЕГРЮЛ/ЕГРИП.",
            details={"inn": inn, "ogrn": ogrn},
        )

    hit = hits[0]
    status = infer_status_from_hit(hit)
    sources_checked: list[str] = ["egrul"]
    warnings: list[str] = []
    bankruptcy_stage: str | None = None
    status_changed_at = parse_date(hit.liquidation_date)

    can_check_bankruptcy = (
        efrsb is not None
        and status == "active"
        and hit.inn
        and is_valid_inn(hit.inn)
        and len(hit.inn) == 10
    )

    if can_check_bankruptcy:
        assert efrsb is not None
        try:
            cases = await efrsb.search_active_by_inn(hit.inn)
            sources_checked.append("efrsb")
            if cases:
                status = "bankruptcy"
                bankruptcy_stage = next((c.stage for c in cases if c.stage), None)
        except (SourceUnavailableError, RateLimitedError) as exc:
            warnings.append(
                f"Источник ЕФРСБ недоступен: {exc.message_ru}. Статус только по ЕГРЮЛ."
            )
        except ValidationError:
            pass

    return LegalStatusReport(
        inn=hit.inn or inn,
        ogrn=hit.ogrn or ogrn,
        status=status,  # type: ignore[arg-type]
        status_label_ru=status_label_ru(status),
        status_changed_at=status_changed_at,
        bankruptcy_stage=bankruptcy_stage,
        liquidation_phase=None,
        exclusion_reason=None,
        sources_checked=sources_checked,
        warnings=warnings,
        checked_at=datetime.now(tz=timezone.utc),
    )
