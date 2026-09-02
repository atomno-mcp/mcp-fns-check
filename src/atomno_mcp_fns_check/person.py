"""Нормализация и валидация персональных полей (ФИО, дата рождения, СНИЛС).

Используется в `check_individual_bankruptcy` и REST `/check-individual-bankruptcy`.
Plain-text ПДн в audit-логи не пишем — только `hash_person_query`.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime

from .errors import ValidationError

FIO_MIN_LENGTH = 5
FIO_MAX_LENGTH = 200
SNILS_DIGITS_LENGTH = 11

_FIO_ALLOWED_CHARS = re.compile(r"^[А-Яа-яЁё\s\-']+$")
_DOUBLE_SPACE = re.compile(r"\s+")
_SNILS_DIGITS = re.compile(r"\d+")


def normalize_fio(value: object) -> str:
    """Нормализовать ФИО: strip, схлопывание пробелов, валидация символов."""
    if not isinstance(value, str):
        raise ValidationError(
            f"ФИО должно быть строкой, получено: {type(value).__name__}.",
            details={"input_type": type(value).__name__},
        )

    cleaned = _DOUBLE_SPACE.sub(" ", value.strip())

    if len(cleaned) < FIO_MIN_LENGTH:
        raise ValidationError(
            f"ФИО слишком короткое (минимум {FIO_MIN_LENGTH} символов).",
            details={"input": value, "length": len(cleaned)},
        )

    if len(cleaned) > FIO_MAX_LENGTH:
        raise ValidationError(
            f"ФИО слишком длинное (максимум {FIO_MAX_LENGTH} символов).",
            details={"input": value, "length": len(cleaned)},
        )

    if not _FIO_ALLOWED_CHARS.match(cleaned):
        raise ValidationError(
            "ФИО содержит недопустимые символы. Разрешены только русские "
            "буквы, пробелы, дефисы и апострофы.",
            details={"input": value},
        )

    parts = cleaned.split()
    if len(parts) < 2:
        raise ValidationError(
            "ФИО должно содержать как минимум фамилию и имя.",
            details={"input": value},
        )

    return cleaned


def parse_birth_date(value: object) -> date:
    """Распарсить дату рождения (`YYYY-MM-DD` или `dd.mm.yyyy`)."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()

    if not isinstance(value, str):
        raise ValidationError(
            f"Дата рождения должна быть строкой или date, получено "
            f"{type(value).__name__}.",
            details={"input_type": type(value).__name__},
        )

    raw = value.strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            parsed = datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
        _check_reasonable_birth_date(parsed, raw)
        return parsed

    raise ValidationError(
        f"Дата рождения '{value}' не распознана. "
        f"Ожидается формат 'YYYY-MM-DD' или 'dd.mm.yyyy'.",
        details={"input": value},
    )


def _check_reasonable_birth_date(parsed: date, raw: str) -> None:
    today = date.today()
    if parsed > today:
        raise ValidationError(
            f"Дата рождения '{raw}' в будущем — скорее всего опечатка.",
            details={"input": raw, "parsed": parsed.isoformat()},
        )
    if parsed.year < 1900:
        raise ValidationError(
            f"Дата рождения '{raw}' раньше 1900 года — скорее всего опечатка.",
            details={"input": raw, "parsed": parsed.isoformat()},
        )


def normalize_snils(value: object | None) -> str | None:
    """Нормализовать СНИЛС до 11 цифр или вернуть None."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(
            f"СНИЛС должен быть строкой, получено: {type(value).__name__}.",
            details={"input_type": type(value).__name__},
        )
    raw = value.strip()
    if not raw:
        return None
    digits = "".join(_SNILS_DIGITS.findall(raw))
    if len(digits) != SNILS_DIGITS_LENGTH:
        raise ValidationError(
            f"СНИЛС '{value}' должен содержать {SNILS_DIGITS_LENGTH} цифр.",
            details={"input": value, "digits": len(digits)},
        )
    return digits


def hash_person_query(fio: str, birth_date: date, snils: str | None = None) -> str:
    """Стабильный анонимный ID запроса для audit/cache-ключей."""
    snils_part = (snils or "").strip()
    payload = f"{fio.lower()}|{birth_date.isoformat()}|{snils_part}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
