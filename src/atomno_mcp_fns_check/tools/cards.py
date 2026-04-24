"""Нормализация хитов ЕГРЮЛ в публичную модель CounterpartyCard.

Здесь живёт вся «бизнес-логика» преобразования сырого хита `EgrulSearchHit` в
строгую Pydantic-модель `CounterpartyCard`. На S2 заполняем только те поля,
которые прямо доступны из публичного интерфейса `egrul.nalog.ru`. Поля учредителей,
authorized_capital_rub, msp_status, extended-метрики «Прозрачного бизнеса»
оставляем None / пустыми — они закрываются в S2.x и S4.

Маскирование ИНН руководителя — обязательное требование SPEC §3.1 шаг 6 и
§4.6 (минимизация ПДн).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone

from ..schemas import (
    AddressInfo,
    CounterpartyCard,
    CounterpartyName,
    DataSourceMeta,
    DirectorInfo,
    OkvedInfo,
    RegistrationInfo,
)
from ..sources.egrul import EgrulSearchHit
from ..validators import is_valid_inn

_DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%Y/%m/%d", "%d-%m-%Y")


def parse_date(value: str | None) -> date | None:
    """Толерантный парсер дат, поддерживает форматы ЕГРЮЛ."""
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def mask_inn(inn: str | None) -> str | None:
    """Замаскировать ИНН руководителя в формате 'XXX*****YY' (см. SPEC §3.1).

    Применяется только к 12-значному ИНН физлица. 10-значный ИНН (юр.лицо)
    не маскируется. Возвращает None если на входе None или невалидный ИНН.
    """
    if not inn:
        return None
    inn = inn.strip()
    if not inn.isdigit():
        return None
    if len(inn) != 12:
        return None
    return f"{inn[:3]}*****{inn[10:]}"


def infer_status_from_hit(hit: EgrulSearchHit) -> str:
    """Простое правило статуса из ЕГРЮЛ-хита.

    На S2 различаем active / liquidated по факту наличия `liquidation_date`.
    Различение между liquidating / liquidated / excluded_inactive / reorganizing
    требует обращения к расширенным полям выписки и обогащается на S2.x.
    """
    if hit.liquidation_date and parse_date(hit.liquidation_date):
        return "liquidated"
    return "active"


def _build_okved(hit: EgrulSearchHit) -> OkvedInfo | None:
    if not hit.okved_code:
        return None
    return OkvedInfo(
        code=hit.okved_code,
        name=hit.okved_name,
        section=_okved_section_letter(hit.okved_code),
        is_licensable=False,
    )


_SECTION_BY_TOP_RANGE: tuple[tuple[int, int, str], ...] = (
    (1, 3, "A"),
    (5, 9, "B"),
    (10, 33, "C"),
    (35, 35, "D"),
    (36, 39, "E"),
    (41, 43, "F"),
    (45, 47, "G"),
    (49, 53, "H"),
    (55, 56, "I"),
    (58, 63, "J"),
    (64, 66, "K"),
    (68, 68, "L"),
    (69, 75, "M"),
    (77, 82, "N"),
    (84, 84, "O"),
    (85, 85, "P"),
    (86, 88, "Q"),
    (90, 93, "R"),
    (94, 96, "S"),
    (97, 98, "T"),
    (99, 99, "U"),
)


def _okved_section_letter(code: str) -> str | None:
    """По двузначному префиксу кода ОКВЭД-2 определить букву раздела."""
    m = re.match(r"^(\d{2})", code)
    if not m:
        return None
    top = int(m.group(1))
    for lo, hi, letter in _SECTION_BY_TOP_RANGE:
        if lo <= top <= hi:
            return letter
    return None


def hit_to_card(
    hit: EgrulSearchHit,
    *,
    cache_age_hours: float | None = None,
) -> CounterpartyCard:
    """Преобразовать сырой хит ЕГРЮЛ в строгую модель CounterpartyCard.

    Args:
        hit: нормализованная запись из EgrulClient.search_by_*().
        cache_age_hours: возраст кэшированного payload-а в часах (для метаданных).

    Returns:
        CounterpartyCard со всеми доступными на S2 полями.
    """
    subject_type = "individual" if hit.is_individual else "legal_entity"
    if hit.inn and is_valid_inn(hit.inn):
        if subject_type == "individual" and len(hit.inn) != 12:
            subject_type = "legal_entity"
        elif subject_type == "legal_entity" and len(hit.inn) != 10:
            subject_type = "individual"

    name_full = hit.name_full or "—"

    director: DirectorInfo | None = None
    if hit.director_name or hit.director_position:
        director = DirectorInfo(
            full_name=hit.director_name,
            position=hit.director_position,
            inn_masked=None,
        )

    address: AddressInfo | None = None
    if hit.address:
        address = AddressInfo(
            full=hit.address,
            region_code=_region_code_from_address(hit.address),
            is_mass_address=False,
            address_status="unknown",
        )

    return CounterpartyCard(
        inn=hit.inn,
        ogrn=hit.ogrn or None,
        kpp=hit.kpp,
        type=subject_type,  # type: ignore[arg-type]
        status=infer_status_from_hit(hit),  # type: ignore[arg-type]
        name=CounterpartyName(full=name_full, short=hit.name_short),
        registration=RegistrationInfo(
            date=parse_date(hit.registration_date),
        ),
        address=address,
        director=director,
        founders=[],
        okved_main=_build_okved(hit),
        additional_okveds=[],
        tax_regime="unknown",
        authorized_capital_rub=None,
        msp_status=None,
        extended=None,
        data_source_meta=DataSourceMeta(
            egrul_extract_date=None,
            cache_age_hours=cache_age_hours,
            fetched_at=datetime.now(tz=timezone.utc),
        ),
    )


_REGION_PREFIX_PATTERN = re.compile(r"\b([0-9]{2,3})\s*[,]")


def _region_code_from_address(address: str) -> str | None:
    """Из строки адреса вытащить двузначный код субъекта РФ.

    Пример: '117997, Г.Москва, УЛ. ВАВИЛОВА, ...' → ИНН/индекс не помогает,
    но для большинства строк ЕГРЮЛ код субъекта = первые 2 цифры почтового
    индекса. Если не удалось определить — возвращаем None.
    """
    if not address:
        return None
    m = re.match(r"^\s*(\d{6})\b", address)
    if m:
        return m.group(1)[:2]
    m2 = _REGION_PREFIX_PATTERN.search(address)
    if m2:
        digits = m2.group(1)
        return digits[:2]
    return None
