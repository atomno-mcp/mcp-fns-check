"""Валидаторы ИНН и ОГРН по официальным алгоритмам ФНС.

ИНН (Идентификационный номер налогоплательщика):
    - 10 цифр для юридического лица.
    - 12 цифр для физического лица или ИП (Индивидуальный предприниматель).
    Контрольная цифра — взвешенная сумма по модулю 11 затем модулю 10.

ОГРН/ОГРНИП (Основной государственный регистрационный номер):
    - 13 цифр для юр.лица.
    - 15 цифр для ИП.
    Контрольная цифра — последний разряд = (N mod 11) mod 10,
    где N — все предыдущие цифры как число.

Источники:
    - Приказ МНС России от 03.03.2004 N БГ-3-09/178.
    - Постановление Правительства РФ от 19.06.2002 N 438 (ОГРН).
"""

from __future__ import annotations

from .errors import ValidationError

_INN_10_WEIGHTS = (2, 4, 10, 3, 5, 9, 4, 6, 8, 0)
_INN_12_WEIGHTS_1 = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8, 0, 0)
_INN_12_WEIGHTS_2 = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8, 0)


def _checksum_inn(digits: tuple[int, ...], weights: tuple[int, ...]) -> int:
    return sum(d * w for d, w in zip(digits, weights, strict=False)) % 11 % 10


def is_valid_inn(value: str) -> bool:
    """Проверить валидность ИНН (длина + контрольная цифра).

    Возвращает True, если ИНН валиден; False — если строка содержит не цифры,
    имеет неверную длину или не сходится контрольная сумма.
    """
    if not isinstance(value, str) or not value.isdigit():
        return False
    if len(value) not in (10, 12):
        return False

    digits = tuple(int(c) for c in value)

    if len(digits) == 10:
        return _checksum_inn(digits[:10], _INN_10_WEIGHTS) == digits[9]

    check1 = _checksum_inn(digits[:11] + (0,), _INN_12_WEIGHTS_1)
    check2 = _checksum_inn(digits[:12], _INN_12_WEIGHTS_2)
    return check1 == digits[10] and check2 == digits[11]


def is_valid_ogrn(value: str) -> bool:
    """Проверить валидность ОГРН (13 цифр, юр.лицо) или ОГРНИП (15 цифр, ИП)."""
    if not isinstance(value, str) or not value.isdigit():
        return False
    if len(value) not in (13, 15):
        return False

    body = value[:-1]
    expected = int(value[-1])

    if len(value) == 13:
        # ОГРН (13): контрольная = (число из 12 цифр) mod 11 mod 10
        return int(body) % 11 % 10 == expected

    # ОГРНИП (15): контрольная = (число из 14 цифр) mod 13 mod 10
    return int(body) % 13 % 10 == expected


def assert_valid_inn(value: str) -> str:
    """Поднимает ValidationError при невалидном ИНН, иначе возвращает значение."""
    if not is_valid_inn(value):
        raise ValidationError(
            f"Невалидный ИНН: '{value}'. Ожидается 10 или 12 цифр с корректной контрольной цифрой.",
            details={"input": value, "expected_length": [10, 12]},
        )
    return value


def assert_valid_ogrn(value: str) -> str:
    """Поднимает ValidationError при невалидном ОГРН/ОГРНИП, иначе возвращает значение."""
    if not is_valid_ogrn(value):
        raise ValidationError(
            f"Невалидный ОГРН: '{value}'. Ожидается 13 (юр.лицо) или 15 (ИП) цифр с корректной контрольной цифрой.",
            details={"input": value, "expected_length": [13, 15]},
        )
    return value


def detect_subject_type(inn: str) -> str:
    """По длине валидного ИНН определить тип субъекта.

    Возвращает 'legal_entity' для 10-значного ИНН, 'individual' для 12-значного.
    Для невалидного ИНН — поднимает ValidationError.
    """
    assert_valid_inn(inn)
    return "legal_entity" if len(inn) == 10 else "individual"


def parse_ogrn_meta(ogrn: str) -> dict:
    """Извлечь метаданные из ОГРН/ОГРНИП.

    Структура (см. SPEC §3.2):
      - первая цифра — признак (1, 5 — юр.лицо; 3 — ИП);
      - цифры 2-3 — последние две цифры года регистрации;
      - цифры 4-5 — код субъекта РФ (двузначный код региона).
    """
    assert_valid_ogrn(ogrn)
    sign = int(ogrn[0])
    year_short = int(ogrn[1:3])
    region_code = ogrn[3:5]
    is_ip = len(ogrn) == 15

    if sign == 3 and not is_ip:
        # Защита от противоречий: признак "3" обычно у ОГРНИП (15 цифр).
        # Не считаем фатальной ошибкой, просто отметим.
        subject_type = "unknown"
    elif is_ip:
        subject_type = "individual"
    else:
        subject_type = "legal_entity"

    full_year = 2000 + year_short if year_short < 70 else 1900 + year_short

    return {
        "sign": sign,
        "year_short": year_short,
        "registration_year": full_year,
        "region_code": region_code,
        "subject_type": subject_type,
    }
