"""Тесты check_for_red_flags (S3): 4 проверки + агрегатор."""

from __future__ import annotations

import httpx
import pytest
import respx

from atomno_mcp_fns_check.context import ServiceContext
from atomno_mcp_fns_check.db.cache import SQLiteCache
from atomno_mcp_fns_check.db.registries import RegistryStore
from atomno_mcp_fns_check.errors import ValidationError
from atomno_mcp_fns_check.sources.efrsb import EFRSB_BASE_URL, EFRSB_SEARCH_PATH, EfrsbClient
from atomno_mcp_fns_check.sources.egrul import EGRUL_BASE_URL, EgrulSearchHit, EgrulClient
from atomno_mcp_fns_check.tools.red_flags import (
    check_bankruptcy_records,
    check_disqualified_director,
    check_for_red_flags,
    check_mass_address,
    check_mass_director,
)

TOKEN = "tk-rf"
EFRSB_URL = f"{EFRSB_BASE_URL}{EFRSB_SEARCH_PATH}"


def _mock_egrul(m: respx.MockRouter, payload: dict) -> None:
    m.post(f"{EGRUL_BASE_URL}/").mock(return_value=httpx.Response(200, json={"t": TOKEN}))
    m.get(f"{EGRUL_BASE_URL}/search-result/{TOKEN}").mock(
        return_value=httpx.Response(200, json=payload)
    )


@pytest.fixture
async def ctx(tmp_path):
    cache = SQLiteCache(tmp_path / "cache.sqlite", default_ttl_hours=24)
    registries = RegistryStore(tmp_path / "reg.sqlite")
    egrul = EgrulClient(backoff_base=0.0)
    efrsb = EfrsbClient()
    c = ServiceContext.for_testing(
        egrul=egrul, efrsb=efrsb, cache=cache, registries=registries
    )
    await c.__aenter__()
    # Подсыпаем фикстуры в реестры.
    await registries.upsert_mass_addresses(
        [{"address": "127015, г Москва, ул Бумажная, д 1", "registered_entities_count": 50}]
    )
    await registries.upsert_mass_directors(
        [
            {
                "director_inn": "770700000099",
                "full_name": "ИВАНОВ ИВАН ИВАНОВИЧ",
                "companies_count": 87,
            }
        ]
    )
    await registries.upsert_disqualified(
        [
            {
                "person_inn": "504700000056",
                "full_name": "СИДОРОВ С.С.",
                "disqualification_date": "2024-02-10",
                "disqualification_until": "2099-02-10",
            }
        ]
    )
    yield c
    await c.__aexit__(None, None, None)


def _hit(**overrides) -> EgrulSearchHit:
    base = dict(
        inn="7707083893",
        ogrn="1027700132195",
        kpp=None,
        name_full="ООО Тест",
        name_short=None,
        address=None,
        okved_code=None,
        okved_name=None,
        director_name=None,
        director_position=None,
        registration_date=None,
        liquidation_date=None,
        is_individual=False,
        raw={},
    )
    base.update(overrides)
    return EgrulSearchHit(**base)


# --- mass_address ---


class TestCheckMassAddress:
    async def test_hit_high(self, ctx):
        hit = _hit(address="г Москва, ул Бумажная, д 1")
        result = await check_mass_address(ctx, hit)
        assert result.flag is not None
        assert result.flag.code == "mass_address"
        assert result.flag.level == "high"
        assert "массовой регистрации" in result.flag.message_ru

    async def test_no_address(self, ctx):
        result = await check_mass_address(ctx, _hit(address=None))
        assert result.flag is None
        assert result.skipped_reason is not None

    async def test_no_match(self, ctx):
        result = await check_mass_address(ctx, _hit(address="ул Несуществующая, 999"))
        assert result.flag is None
        assert result.skipped_reason is None

    async def test_no_registries_skipped(self, tmp_path):
        cache = SQLiteCache(tmp_path / "c.sqlite")
        c = ServiceContext.for_testing(
            egrul=EgrulClient(), efrsb=EfrsbClient(), cache=cache, registries=None
        )
        async with c:
            result = await check_mass_address(c, _hit(address="ул А, 1"))
        assert result.flag is None
        assert result.skipped_reason is not None


# --- mass_director ---


class TestCheckMassDirector:
    async def test_hit_by_inn(self, ctx):
        hit = _hit(director_name="ИВАНОВ ИВАН", raw={"director_inn": "770700000099"})
        result = await check_mass_director(ctx, hit)
        assert result.flag is not None
        assert result.flag.level == "high"
        assert result.flag.details["match_precision"] == "exact_inn"

    async def test_hit_by_name_only(self, ctx):
        hit = _hit(director_name="ИВАНОВ ИВАН ИВАНОВИЧ")
        result = await check_mass_director(ctx, hit)
        assert result.flag is not None
        assert result.flag.details["match_precision"] == "approximate_name_only"

    async def test_no_director_skipped(self, ctx):
        result = await check_mass_director(ctx, _hit())
        assert result.flag is None
        assert result.skipped_reason is not None

    async def test_no_match(self, ctx):
        hit = _hit(director_name="Незнакомец Н.Н.")
        result = await check_mass_director(ctx, hit)
        assert result.flag is None


# --- disqualified ---


class TestCheckDisqualifiedDirector:
    async def test_active_disqualification_high(self, ctx):
        hit = _hit(director_name="СИДОРОВ С.С.", raw={"director_inn": "504700000056"})
        result = await check_disqualified_director(ctx, hit)
        assert result.flag is not None
        assert result.flag.level == "high"
        assert result.flag.details["active_disqualifications"] == 1

    async def test_no_director_skipped(self, ctx):
        result = await check_disqualified_director(ctx, _hit())
        assert result.flag is None
        assert result.skipped_reason is not None

    async def test_no_match(self, ctx):
        hit = _hit(director_name="Чистый Ч.Ч.")
        result = await check_disqualified_director(ctx, hit)
        assert result.flag is None

    async def test_expired_returns_medium(self, tmp_path):
        registries = RegistryStore(tmp_path / "r.sqlite")
        await registries.init()
        await registries.upsert_disqualified(
            [
                {
                    "person_inn": "504700000056",
                    "full_name": "СТАРЫЙ С.С.",
                    "disqualification_date": "2010-01-01",
                    "disqualification_until": "2012-01-01",
                }
            ]
        )
        c = ServiceContext.for_testing(
            egrul=EgrulClient(),
            efrsb=EfrsbClient(),
            cache=SQLiteCache(tmp_path / "c.sqlite"),
            registries=registries,
        )
        async with c:
            hit = _hit(director_name="СТАРЫЙ С.С.", raw={"director_inn": "504700000056"})
            result = await check_disqualified_director(c, hit)
        assert result.flag is not None
        assert result.flag.level == "medium"
        assert result.flag.details["active_disqualifications"] == 0


# --- bankruptcy_records ---


class TestCheckBankruptcyRecords:
    async def test_active_bankruptcy_high(self, ctx):
        hit = _hit(inn="7707083893")
        with respx.mock() as m:
            m.get(EFRSB_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "pageData": [
                            {
                                "caseNumber": "А40-1/2025",
                                "stageName": "Конкурсное производство",
                                "isActive": True,
                            }
                        ]
                    },
                )
            )
            result = await check_bankruptcy_records(ctx, hit)
        assert result.flag is not None
        assert result.flag.level == "high"
        assert result.flag.details["cases_count"] == 1

    async def test_observation_medium(self, ctx):
        hit = _hit(inn="7707083893")
        with respx.mock() as m:
            m.get(EFRSB_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "pageData": [
                            {
                                "caseNumber": "А40-2/2025",
                                "stageName": "Наблюдение",
                                "isActive": True,
                            }
                        ]
                    },
                )
            )
            result = await check_bankruptcy_records(ctx, hit)
        assert result.flag is not None
        assert result.flag.level == "medium"

    async def test_no_cases(self, ctx):
        hit = _hit(inn="7707083893")
        with respx.mock() as m:
            m.get(EFRSB_URL).mock(return_value=httpx.Response(200, json={"pageData": []}))
            result = await check_bankruptcy_records(ctx, hit)
        assert result.flag is None
        assert result.skipped_reason is None


# --- aggregator ---


def _sber_payload(**extra_root):
    row = {
        "i": "7707083893",
        "o": "1027700132195",
        "n": "ПАО СБЕРБАНК",
        "k": "ul",
        "a": "127015, г Москва, ул Бумажная, д 1",
        "g": "ИВАНОВ ИВАН ИВАНОВИЧ",
        "gp": "Президент",
    }
    row.update(extra_root)
    return {"rows": [row]}


class TestAggregator:
    async def test_invalid_inn(self, ctx):
        with pytest.raises(ValidationError):
            await check_for_red_flags(ctx, "1111111111")

    async def test_clean_company_low_risk(self, ctx):
        clean = {
            "rows": [
                {
                    "i": "7707083893",
                    "o": "1027700132195",
                    "n": "ООО Чистая",
                    "k": "ul",
                    "a": "ул Чистая, 100",
                    "g": "Чистый Ч.Ч.",
                }
            ]
        }
        with respx.mock() as m:
            _mock_egrul(m, clean)
            m.get(EFRSB_URL).mock(return_value=httpx.Response(200, json={"pageData": []}))
            report = await check_for_red_flags(ctx, "7707083893")

        assert report.overall_risk_level == "low"
        assert report.overall_risk_score == 0
        assert report.flags == []
        assert "Не найдено" in report.summary_ru or "не обнаружено" in report.summary_ru

    async def test_multiple_flags_high(self, ctx):
        with respx.mock() as m:
            _mock_egrul(m, _sber_payload())
            m.get(EFRSB_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "pageData": [
                            {
                                "caseNumber": "А40-3/2025",
                                "stageName": "Конкурсное производство",
                                "isActive": True,
                            }
                        ]
                    },
                )
            )
            report = await check_for_red_flags(ctx, "7707083893")

        codes = {f.code for f in report.flags}
        # mass_address (адрес из реестра), mass_director (по ФИО),
        # bankruptcy_records (конкурсное производство).
        assert "mass_address" in codes
        assert "mass_director" in codes
        assert "bankruptcy_records" in codes
        assert report.overall_risk_level == "high"
        assert report.overall_risk_score >= 50
        assert "высоких" in report.summary_ru or "высокий" in report.summary_ru

    async def test_efrsb_failure_recorded_as_error(self, ctx):
        with respx.mock() as m:
            clean = {
                "rows": [
                    {
                        "i": "7707083893",
                        "o": "1027700132195",
                        "n": "ООО Тест",
                        "k": "ul",
                        "a": "ул Чистая, 100",
                    }
                ]
            }
            _mock_egrul(m, clean)
            m.get(EFRSB_URL).mock(return_value=httpx.Response(503))
            report = await check_for_red_flags(ctx, "7707083893")

        assert any(e.code == "bankruptcy_records" for e in report.errors)
        # Остальные проверки прошли (либо passed, либо skipped).
        assert report.overall_risk_level in {"low", "medium"}

    async def test_uses_cache_for_egrul(self, ctx):
        with respx.mock() as m:
            route_post = m.post(f"{EGRUL_BASE_URL}/").mock(
                return_value=httpx.Response(200, json={"t": TOKEN})
            )
            m.get(f"{EGRUL_BASE_URL}/search-result/{TOKEN}").mock(
                return_value=httpx.Response(200, json=_sber_payload())
            )
            m.get(EFRSB_URL).mock(return_value=httpx.Response(200, json={"pageData": []}))

            await check_for_red_flags(ctx, "7707083893")
            calls_after_first = route_post.call_count
            await check_for_red_flags(ctx, "7707083893")
            assert route_post.call_count == calls_after_first
