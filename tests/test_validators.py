"""Тесты валидаторов ИНН и ОГРН."""

from __future__ import annotations

import pytest

from atomno_mcp_fns_check.errors import ValidationError
from atomno_mcp_fns_check.validators import (
    assert_valid_inn,
    assert_valid_ogrn,
    detect_subject_type,
    is_valid_inn,
    is_valid_ogrn,
    parse_ogrn_meta,
)

# Реальные ИНН известных компаний — публично известные данные.
VALID_INN_LEGAL = [
    "7707083893",  # Сбербанк
    "7728168971",  # Газпром
    "7704217370",  # Роснефть
]

# Валидные 12-значные ИНН (контрольные цифры посчитаны и проверены).
VALID_INN_INDIVIDUAL = [
    "500100732259",
    "773173381311",
]

VALID_OGRN_LEGAL = [
    "1027700132195",  # Сбербанк
    "1037700013020",  # Газпром
]

VALID_OGRNIP = [
    "304500116000061",
    "320774000000048",
]


class TestINN:
    @pytest.mark.parametrize("inn", VALID_INN_LEGAL)
    def test_valid_legal(self, inn: str) -> None:
        assert is_valid_inn(inn) is True
        assert detect_subject_type(inn) == "legal_entity"

    @pytest.mark.parametrize("inn", VALID_INN_INDIVIDUAL)
    def test_valid_individual(self, inn: str) -> None:
        assert is_valid_inn(inn) is True
        assert detect_subject_type(inn) == "individual"

    @pytest.mark.parametrize(
        "inn",
        [
            "",
            "abc",
            "123",
            "12345678901",  # 11 цифр — невалидная длина
            "1111111111",   # контрольная не сходится
            "7707083894",   # Сбербанк с искажённой контрольной
            "1234567890",   # просто последовательность — контрольная не сходится
            None,
            12345,
        ],
    )
    def test_invalid(self, inn: object) -> None:
        assert is_valid_inn(inn) is False  # type: ignore[arg-type]

    def test_assert_raises(self) -> None:
        with pytest.raises(ValidationError) as exc:
            assert_valid_inn("1234567890")
        assert exc.value.code == "invalid_input"
        assert "1234567890" in exc.value.message_ru

    def test_assert_passes(self) -> None:
        assert assert_valid_inn("7707083893") == "7707083893"

    def test_detect_invalid_raises(self) -> None:
        with pytest.raises(ValidationError):
            detect_subject_type("1111111111")


class TestOGRN:
    @pytest.mark.parametrize("ogrn", VALID_OGRN_LEGAL)
    def test_valid_legal(self, ogrn: str) -> None:
        assert is_valid_ogrn(ogrn) is True

    @pytest.mark.parametrize("ogrn", VALID_OGRNIP)
    def test_valid_individual(self, ogrn: str) -> None:
        assert is_valid_ogrn(ogrn) is True

    @pytest.mark.parametrize(
        "ogrn",
        [
            "",
            "abc",
            "1234567890",  # 10 цифр — не та длина
            "1027700132196",  # Сбер ОГРН с искажённой контрольной
            "1234567890123",  # длина ок, контрольная не сходится
            None,
        ],
    )
    def test_invalid(self, ogrn: object) -> None:
        assert is_valid_ogrn(ogrn) is False  # type: ignore[arg-type]

    def test_assert_raises(self) -> None:
        with pytest.raises(ValidationError):
            assert_valid_ogrn("1234567890123")

    def test_parse_meta_legal(self) -> None:
        meta = parse_ogrn_meta("1027700132195")
        assert meta["sign"] == 1
        assert meta["region_code"] == "77"
        assert meta["registration_year"] == 2002
        assert meta["subject_type"] == "legal_entity"

    def test_parse_meta_individual(self) -> None:
        meta = parse_ogrn_meta("304500116000061")
        assert meta["subject_type"] == "individual"
        assert meta["region_code"] == "50"
