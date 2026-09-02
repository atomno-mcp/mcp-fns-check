"""Тул check_individual_bankruptcy — банкротство физлица по ФИО и дате рождения."""

from __future__ import annotations

from datetime import datetime, timezone

from ..errors import RateLimitedError, SourceUnavailableError
from ..person import hash_person_query, normalize_fio, normalize_snils, parse_birth_date
from ..person_notice import (
    EFRSB_REGISTRY_RU,
    EFRSB_URL,
    HOMONYM_WARNING_RU,
    REGISTRY_DATE_UNPUBLISHED_RU,
    build_notice_ru,
)
from ..schemas import IndividualBankruptcyRecord, IndividualBankruptcyReport
from ..sources.efrsb import EfrsbClient, IndividualBankruptcyCase


def _case_to_record(case: IndividualBankruptcyCase) -> IndividualBankruptcyRecord:
    return IndividualBankruptcyRecord(
        case_number=case.case_number,
        stage_ru=case.stage_ru,
        stage=case.stage,
        is_active=case.is_active,
        debtor_fio=case.debtor_fio,
        birth_date=case.birth_date,
        inn=case.inn,
        snils=case.snils,
        region=case.region,
        match_confidence=case.match_confidence,
    )


async def check_individual_bankruptcy(
    *,
    fio: str,
    birth_date: str,
    snils: str | None = None,
    efrsb: EfrsbClient,
) -> IndividualBankruptcyReport:
    """Проверить наличие активных дел о банкротстве физлица в ЕФРСБ."""
    normalized_fio = normalize_fio(fio)
    parsed_birth = parse_birth_date(birth_date)
    normalized_snils = normalize_snils(snils)
    query_hash = hash_person_query(normalized_fio, parsed_birth, normalized_snils)

    try:
        active_cases, source = await efrsb.search_active_by_person(
            normalized_fio,
            parsed_birth,
            snils=normalized_snils,
        )
    except (RateLimitedError, SourceUnavailableError):
        raise

    records = [_case_to_record(case) for case in active_cases]
    warnings: list[str] = []
    possible_only = records and all(r.match_confidence == "possible" for r in records)
    if possible_only:
        warnings.append(
            "Найдены совпадения только по ФИО без подтверждения даты рождения/СНИЛС. "
            "Требуется ручная верификация."
        )

    has_match = bool(records)
    disclaimer = (
        "Сведения получены из публичного реестра ЕФРСБ (bankrot.fedresurs.ru). "
        "Это информационная справка, не юридическое заключение о статусе банкрота."
    )
    if has_match:
        disclaimer = f"{disclaimer} {HOMONYM_WARNING_RU}"
    checked_at = datetime.now(tz=timezone.utc)
    return IndividualBankruptcyReport(
        query_fio=normalized_fio,
        query_birth_date=parsed_birth,
        query_snils=normalized_snils,
        query_hash=query_hash,
        has_active_bankruptcy=has_match,
        active_cases=records,
        total_matches=len(records),
        source=source,
        warnings=warnings,
        disclaimer_ru=disclaimer,
        checked_at=checked_at,
        source_registry_ru=EFRSB_REGISTRY_RU,
        source_url=EFRSB_URL,
        data_as_of=None,
        data_as_of_note_ru=REGISTRY_DATE_UNPUBLISHED_RU,
        homonym_warning_ru=HOMONYM_WARNING_RU if has_match else None,
        notice_ru=build_notice_ru(
            source_registry_ru=EFRSB_REGISTRY_RU,
            source_url=EFRSB_URL,
            queried_at=checked_at,
            data_as_of=None,
            has_match=has_match,
        ),
    )
