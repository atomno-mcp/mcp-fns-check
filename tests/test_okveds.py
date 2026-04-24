"""Тесты расшифровки ОКВЭД и сборки OkvedReport."""

from __future__ import annotations

import pytest

from atomno_mcp_fns_check.sources.egrul import EgrulSearchHit
from atomno_mcp_fns_check.tools.okveds import build_okved_report, lookup_okved


class TestLookup:
    def test_known_code_full_match(self):
        info = lookup_okved("64.19")
        assert info.name == "Денежное посредничество прочее"
        assert info.section == "K"
        assert info.section_name and "финансовая" in info.section_name
        assert info.is_licensable is True
        assert info.license_authority and "ЦБ" in info.license_authority

    def test_known_top_level(self):
        info = lookup_okved("46")
        assert info.name and "Торговля оптовая" in info.name
        assert info.section == "G"
        assert info.is_licensable is False

    def test_cascading_fallback_to_two_digit(self):
        info = lookup_okved("46.10.1")
        assert info.code == "46.10.1"
        assert info.name and "Торговля оптовая" in info.name

    def test_cascading_fallback_to_parent_dot(self):
        info = lookup_okved("64.19.5")
        assert info.code == "64.19.5"
        assert info.name == "Денежное посредничество прочее"

    def test_unknown_code(self):
        info = lookup_okved("99.99")
        assert info.code == "99.99"
        assert info.name is None
        assert info.section == "U"
        assert info.is_licensable is False

    def test_software_dev(self):
        info = lookup_okved("62.01")
        assert info.name and "программного обеспечения" in info.name
        assert info.section == "J"
        assert info.is_licensable is False

    def test_education_licensable(self):
        info = lookup_okved("85.41")
        assert info.is_licensable is True
        assert info.license_authority == "Рособрнадзор"

    @pytest.mark.parametrize("code,letter", [("01", "A"), ("85.21", "P"), ("86.21", "Q")])
    def test_section_letter(self, code: str, letter: str):
        assert lookup_okved(code).section == letter


class TestBuildReport:
    def _hit(self, code: str | None, name: str | None) -> EgrulSearchHit:
        return EgrulSearchHit(
            inn="7707083893",
            ogrn="1027700132195",
            kpp="773601001",
            name_full="Test",
            name_short=None,
            address=None,
            okved_code=code,
            okved_name=name,
            director_name=None,
            director_position=None,
            registration_date=None,
            liquidation_date=None,
            is_individual=False,
        )

    def test_main_only(self):
        report = build_okved_report(self._hit("64.19", "Денежное посредничество прочее"))
        assert report.inn == "7707083893"
        assert report.main_okved is not None
        assert report.main_okved.code == "64.19"
        assert report.main_okved.is_licensable is True
        assert report.additional_okveds == []
        assert report.total_count == 1

    def test_no_main(self):
        report = build_okved_report(self._hit(None, None))
        assert report.main_okved is None
        assert report.total_count == 0

    def test_additional_codes_dedup_and_count(self):
        report = build_okved_report(
            self._hit("64.19", None),
            additional_codes=["64.19", "64.92", "62.01"],
        )
        assert report.main_okved is not None
        assert [o.code for o in report.additional_okveds] == ["64.92", "62.01"]
        assert report.total_count == 3

    def test_fallback_uses_hit_name_when_dict_missing(self):
        report = build_okved_report(self._hit("99.99", "Custom hit name"))
        assert report.main_okved is not None
        assert report.main_okved.name == "Custom hit name"
