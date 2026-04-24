"""Тесты нормализации хита ЕГРЮЛ в CounterpartyCard."""

from __future__ import annotations

from datetime import date

import pytest

from atomno_mcp_fns_check.sources.egrul import EgrulSearchHit
from atomno_mcp_fns_check.tools.cards import (
    _okved_section_letter,
    hit_to_card,
    infer_status_from_hit,
    mask_inn,
    parse_date,
)


class TestParseDate:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("1991-06-20", date(1991, 6, 20)),
            ("20.06.1991", date(1991, 6, 20)),
            ("2024/12/31", date(2024, 12, 31)),
            ("31-12-2024", date(2024, 12, 31)),
            ("", None),
            (None, None),
            ("not a date", None),
            ("2024-13-40", None),
        ],
    )
    def test_parse(self, raw, expected):
        assert parse_date(raw) == expected


class TestMaskInn:
    def test_mask_12_digits(self):
        assert mask_inn("770512345612") == "770*****12"

    @pytest.mark.parametrize("value", ["7707083893", "", None, "abcdefghijkl", "1234567"])
    def test_no_mask_for_invalid_or_short(self, value):
        assert mask_inn(value) is None


class TestStatusFromHit:
    def _hit(self, **kwargs):
        defaults = dict(
            inn="7707083893",
            ogrn="1027700132195",
            kpp=None,
            name_full="Test",
            name_short=None,
            address=None,
            okved_code=None,
            okved_name=None,
            director_name=None,
            director_position=None,
            registration_date=None,
            liquidation_date=None,
            is_individual=False,
        )
        defaults.update(kwargs)
        return EgrulSearchHit(**defaults)

    def test_active(self):
        assert infer_status_from_hit(self._hit()) == "active"

    def test_liquidated_when_liquidation_date(self):
        assert infer_status_from_hit(self._hit(liquidation_date="2023-05-12")) == "liquidated"

    def test_active_when_liquidation_date_unparsable(self):
        assert infer_status_from_hit(self._hit(liquidation_date="garbage")) == "active"


class TestOkvedSection:
    @pytest.mark.parametrize(
        "code,letter",
        [
            ("01.11", "A"),
            ("10", "C"),
            ("46.90", "G"),
            ("62.01", "J"),
            ("64.19", "K"),
            ("85", "P"),
            ("86.10", "Q"),
            ("99", "U"),
            ("xx", None),
        ],
    )
    def test_section(self, code, letter):
        assert _okved_section_letter(code) == letter


class TestHitToCard:
    def _sber_hit(self):
        return EgrulSearchHit(
            inn="7707083893",
            ogrn="1027700132195",
            kpp="773601001",
            name_full="ПАО СБЕРБАНК РОССИИ",
            name_short="ПАО СБЕРБАНК",
            address="117997, Г.Москва, УЛ. ВАВИЛОВА, Д. 19",
            okved_code="64.19",
            okved_name="Денежное посредничество прочее",
            director_name="Греф Г. О.",
            director_position="Президент",
            registration_date="1991-06-20",
            liquidation_date=None,
            is_individual=False,
        )

    def test_full_card(self):
        card = hit_to_card(self._sber_hit(), cache_age_hours=0.5)
        assert card.inn == "7707083893"
        assert card.ogrn == "1027700132195"
        assert card.kpp == "773601001"
        assert card.type == "legal_entity"
        assert card.status == "active"
        assert card.name.full == "ПАО СБЕРБАНК РОССИИ"
        assert card.name.short == "ПАО СБЕРБАНК"
        assert card.registration.date == date(1991, 6, 20)
        assert card.address is not None
        assert card.address.region_code == "11"  # 117997 → "11"
        assert card.director is not None
        assert card.director.full_name == "Греф Г. О."
        assert card.okved_main is not None
        assert card.okved_main.code == "64.19"
        assert card.okved_main.section == "K"
        assert card.data_source_meta.cache_age_hours == 0.5
        assert card.data_source_meta.fetched_at is not None

    def test_individual_subject_type(self):
        hit = EgrulSearchHit(
            inn="500100732259",
            ogrn="304500116000061",
            kpp=None,
            name_full="ИП Иванов Иван Иванович",
            name_short=None,
            address=None,
            okved_code=None,
            okved_name=None,
            director_name=None,
            director_position=None,
            registration_date=None,
            liquidation_date=None,
            is_individual=True,
        )
        card = hit_to_card(hit)
        assert card.type == "individual"
        assert card.director is None

    def test_minimal_hit(self):
        hit = EgrulSearchHit(
            inn="7707083893",
            ogrn="",
            kpp=None,
            name_full=None,
            name_short=None,
            address=None,
            okved_code=None,
            okved_name=None,
            director_name=None,
            director_position=None,
            registration_date=None,
            liquidation_date=None,
            is_individual=False,
        )
        card = hit_to_card(hit)
        assert card.name.full == "—"
        assert card.address is None
        assert card.okved_main is None
        assert card.ogrn is None

    def test_subject_type_corrected_from_inn_length(self):
        hit = EgrulSearchHit(
            inn="500100732259",  # 12 digits → individual
            ogrn="",
            kpp=None,
            name_full=None,
            name_short=None,
            address=None,
            okved_code=None,
            okved_name=None,
            director_name=None,
            director_position=None,
            registration_date=None,
            liquidation_date=None,
            is_individual=False,
        )
        card = hit_to_card(hit)
        assert card.type == "individual"
