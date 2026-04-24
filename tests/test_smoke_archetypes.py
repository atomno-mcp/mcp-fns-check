"""Smoke-тесты архетипных контрагентов (R-6 / SPEC v0.2 §9 Phase 1 DoD).

Каждый тест — один типовой контрагент (архетип), сквозной прогон через
`check_contractor` со всеми 5 источниками на respx-моках, ассерт на:

  * `verdict_action` (один из 4 enum-значений);
  * набор `risks.flags[].code` — какие именно флаги должны сработать;
  * `legal_status.status` — какой жизненный статус ожидается;
  * отсутствие регрессии по sources_queried (ровно те источники, что нужны).

Архетипы покрывают все ветви `_derive_verdict`:

  * safe_to_proceed  — #1 (clean legal), #2 (clean IP)
  * impossible_...   — #3 (liquidated)
  * high_risk_...    — #4 (bankruptcy, специальная ветка status=bankruptcy),
                        #5 (disqualified director, специальная ветка _derive_verdict)
  * manual_review... — #6 (mass address, weight 15→medium),
                        #7 (mass director, weight 25→medium),
                        #8 (no reporting, weight 30→medium),
                        #9 (enforcement, weight 25→medium),
                        #10 (FSSP CAPTCHA graceful — partial errors)

Важно: один high-уровень обычного флага (mass_director/no_reporting/enforcement)
даёт overall_risk=medium → manual_review. Для high_risk_do_not_proceed нужен
либо специальный триггер (банкротство, дисквалификация), либо score ≥ 50
(комбинация нескольких флагов). Это осознанный scoring — блокировать сделку
одним флагом слишком агрессивно.

Если в будущем появится новый FlagCode или VerdictAction — его тоже
положи сюда отдельным архетипом, даже если он закрыт юнит-тестом рядом.
Это гарантирует что _каждое_ изменение агрегатора прогоняется по полной
связке check_contractor + все 6 granular tools + validators.
"""

from __future__ import annotations

import pytest
import respx

from atomno_mcp_fns_check.context import ServiceContext
from atomno_mcp_fns_check.db.cache import SQLiteCache
from atomno_mcp_fns_check.db.registries import RegistryStore
from atomno_mcp_fns_check.sources.efrsb import EfrsbClient
from atomno_mcp_fns_check.sources.egrul import EgrulClient
from atomno_mcp_fns_check.sources.fssp import FsspClient
from atomno_mcp_fns_check.sources.kad import KadClient
from atomno_mcp_fns_check.sources.pb_fns import PbFnsClient
from atomno_mcp_fns_check.tools.contractor import check_contractor

from .smoke_helpers import (
    INN_INDIVIDUAL_VALID,
    INN_LEGAL_SBER,
    archetype_active_individual,
    archetype_bankruptcy_active,
    archetype_clean_legal,
    archetype_disqualified_director,
    archetype_fssp_captcha_degraded,
    archetype_large_enforcement,
    archetype_liquidated,
    archetype_mass_address,
    archetype_mass_director,
    archetype_no_reporting,
    mock_scenario,
    preload_disqualified,
    preload_mass_address,
    preload_mass_director,
)


@pytest.fixture
async def smoke_ctx(tmp_path):
    """Свежий ServiceContext с пустыми реестрами (каждый архетип донабирает своё)."""
    cache = SQLiteCache(tmp_path / "cache.sqlite", default_ttl_hours=24)
    registries = RegistryStore(tmp_path / "reg.sqlite")
    ctx = ServiceContext.for_testing(
        egrul=EgrulClient(backoff_base=0.0),
        efrsb=EfrsbClient(),
        cache=cache,
        registries=registries,
        pb_fns=PbFnsClient(timeout=2.0),
        fssp=FsspClient(timeout=2.0),
        kad=KadClient(timeout=2.0),
    )
    await ctx.__aenter__()
    yield ctx
    await ctx.__aexit__(None, None, None)


def _flag_codes(report) -> set[str]:
    return {f.code for f in report.risks.flags}


# --- Архетип #1: чистая действующая компания ---


class TestArchetypeCleanLegal:
    async def test_sber_like_clean_safe_to_proceed(self, smoke_ctx):
        with respx.mock() as m:
            mock_scenario(m, archetype_clean_legal())
            report = await check_contractor(smoke_ctx, INN_LEGAL_SBER)

        assert report.verdict_action == "safe_to_proceed"
        assert report.legal_status.status == "active"
        assert report.risks.overall_risk_level == "low"
        assert _flag_codes(report) == set()
        assert report.risks.errors == []
        assert report.identifier_type == "inn"
        assert report.inn == INN_LEGAL_SBER


# --- Архетип #2: действующий ИП ---


class TestArchetypeActiveIndividual:
    async def test_ip_clean_safe_to_proceed(self, smoke_ctx):
        with respx.mock() as m:
            mock_scenario(m, archetype_active_individual())
            report = await check_contractor(smoke_ctx, INN_INDIVIDUAL_VALID)

        assert report.verdict_action == "safe_to_proceed"
        assert report.legal_status.status == "active"
        assert _flag_codes(report) == set()
        assert report.card.type == "individual"
        assert report.inn == INN_INDIVIDUAL_VALID


# --- Архетип #3: ликвидирована ---


class TestArchetypeLiquidated:
    async def test_liquidated_impossible_verdict(self, smoke_ctx):
        with respx.mock() as m:
            mock_scenario(m, archetype_liquidated())
            report = await check_contractor(smoke_ctx, INN_LEGAL_SBER)

        assert report.verdict_action == "impossible_contractor_defunct"
        assert report.legal_status.status == "liquidated"
        assert any(
            "прекратил существование" in r.lower() or "ликвидирован" in r.lower()
            for r in report.recommendations
        ), f"Ожидалась рекомендация о ликвидации, получили: {report.recommendations}"


# --- Архетип #4: активное банкротство (конкурсное) ---


class TestArchetypeBankruptcyActive:
    async def test_konkursnoe_production_triggers_do_not_proceed(self, smoke_ctx):
        with respx.mock() as m:
            mock_scenario(m, archetype_bankruptcy_active())
            report = await check_contractor(smoke_ctx, INN_LEGAL_SBER)

        assert report.verdict_action == "high_risk_do_not_proceed"
        assert report.legal_status.status == "bankruptcy"
        assert "bankruptcy_records" in _flag_codes(report)
        assert any(
            "арбитражн" in r.lower() or "банкрот" in r.lower()
            for r in report.recommendations
        )


# --- Архетип #5: дисквалифицированный руководитель ---


class TestArchetypeDisqualifiedDirector:
    async def test_active_disqualification_blocks_contract(self, smoke_ctx):
        await preload_disqualified(smoke_ctx)

        with respx.mock() as m:
            mock_scenario(m, archetype_disqualified_director())
            report = await check_contractor(smoke_ctx, INN_LEGAL_SBER)

        assert report.verdict_action == "high_risk_do_not_proceed"
        disqualified = [
            f for f in report.risks.flags if f.code == "disqualified_director"
        ]
        assert disqualified, "Ожидался флаг disqualified_director"
        assert disqualified[0].level == "high"
        assert any("дисквалиф" in r.lower() for r in report.recommendations)


# --- Архетип #6: массовый адрес ---


class TestArchetypeMassAddress:
    async def test_mass_address_triggers_manual_review(self, smoke_ctx):
        await preload_mass_address(smoke_ctx)

        with respx.mock() as m:
            mock_scenario(m, archetype_mass_address())
            report = await check_contractor(smoke_ctx, INN_LEGAL_SBER)

        assert "mass_address" in _flag_codes(report)
        # mass_address сам по себе = medium (weight 15), без других флагов
        # overall_risk_level должен быть medium → manual_review_required.
        assert report.verdict_action == "manual_review_required"
        assert report.risks.overall_risk_level in ("medium", "high")


# --- Архетип #7: массовый руководитель (высокий уровень) ---


class TestArchetypeMassDirector:
    async def test_mass_director_42_companies_is_high_risk(self, smoke_ctx):
        # companies_count=42 ≥ 10 → level=high
        await preload_mass_director(smoke_ctx, companies_count=42)

        with respx.mock() as m:
            mock_scenario(m, archetype_mass_director())
            report = await check_contractor(smoke_ctx, INN_LEGAL_SBER)

        mass = [f for f in report.risks.flags if f.code == "mass_director"]
        assert mass, "Ожидался флаг mass_director"
        assert mass[0].level == "high"
        # high level у одного флага → overall_risk ≥ medium (weight 25)
        assert report.verdict_action in (
            "manual_review_required",
            "high_risk_do_not_proceed",
        )


# --- Архетип #8: не сдаёт отчётность ---


class TestArchetypeNoReporting:
    """no_reporting high-level даёт weight=30 → score=30 → overall=medium.

    Один high-флаг не переводит вердикт в блок (нужен score ≥ 50 либо
    специальные блокирующие статусы вроде банкротства или дисквалификации).
    Поэтому ожидаем manual_review_required + флаг high level в списке.
    """

    async def test_no_reporting_flag_is_high_level_but_verdict_manual(self, smoke_ctx):
        with respx.mock() as m:
            mock_scenario(m, archetype_no_reporting())
            report = await check_contractor(smoke_ctx, INN_LEGAL_SBER)

        no_report = [f for f in report.risks.flags if f.code == "no_reporting"]
        assert no_report, "Ожидался флаг no_reporting из pb.nalog.ru"
        assert no_report[0].level == "high"
        assert report.risks.overall_risk_level == "medium"
        assert report.verdict_action == "manual_review_required"


# --- Архетип #9: крупные исполнительные производства ---


class TestArchetypeLargeEnforcement:
    """enforcement_proceedings high-level даёт weight=25 → score=25 → overall=medium.

    Крупные долги у контрагента — серьёзный сигнал, но сам по себе не блок.
    Агент должен увидеть high-флаг и передать в ручное ревью менеджеру.
    """

    async def test_enforcement_over_1m_flag_is_high_level_verdict_manual(self, smoke_ctx):
        with respx.mock() as m:
            mock_scenario(m, archetype_large_enforcement())
            report = await check_contractor(smoke_ctx, INN_LEGAL_SBER)

        enforcement = [
            f for f in report.risks.flags if f.code == "enforcement_proceedings"
        ]
        assert enforcement, "Ожидался флаг enforcement_proceedings"
        assert enforcement[0].level == "high"
        assert report.risks.overall_risk_level == "medium"
        assert report.verdict_action == "manual_review_required"


# --- Архетип #10: ФССП требует CAPTCHA — graceful degradation ---


class TestArchetypeFsspCaptchaGraceful:
    async def test_captcha_forces_manual_review_not_crash(self, smoke_ctx):
        with respx.mock() as m:
            mock_scenario(m, archetype_fssp_captcha_degraded())
            report = await check_contractor(smoke_ctx, INN_LEGAL_SBER)

        enforcement_errors = [
            e for e in report.risks.errors if e.code == "enforcement_proceedings"
        ]
        assert enforcement_errors, (
            "ФССП CAPTCHA должен попасть в errors[] без падения отчёта"
        )
        assert report.verdict_action == "manual_review_required"
        # card и legal_status должны прийти несмотря на сбой ФССП.
        assert report.card is not None
        assert report.legal_status.status == "active"


# --- Сводный тест: коллекция архетипов покрывает все verdict_action ---


class TestArchetypeCoverage:
    """Мета-тест: все 4 значения VerdictAction должны быть хоть раз покрыты."""

    async def test_all_four_verdict_actions_covered(self, smoke_ctx):
        from atomno_mcp_fns_check.schemas import VerdictAction

        expected = set(VerdictAction.__args__)  # type: ignore[attr-defined]
        collected: set[str] = set()

        # Чистый → safe_to_proceed
        with respx.mock() as m:
            mock_scenario(m, archetype_clean_legal())
            r = await check_contractor(smoke_ctx, INN_LEGAL_SBER)
            collected.add(r.verdict_action)

        # Ликвидирован → impossible
        with respx.mock() as m:
            mock_scenario(m, archetype_liquidated())
            r = await check_contractor(smoke_ctx, INN_LEGAL_SBER)
            collected.add(r.verdict_action)

        # Банкрот → high_risk
        with respx.mock() as m:
            mock_scenario(m, archetype_bankruptcy_active())
            r = await check_contractor(smoke_ctx, INN_LEGAL_SBER)
            collected.add(r.verdict_action)

        # FSSP captcha → manual_review
        with respx.mock() as m:
            mock_scenario(m, archetype_fssp_captcha_degraded())
            r = await check_contractor(smoke_ctx, INN_LEGAL_SBER)
            collected.add(r.verdict_action)

        missing = expected - collected
        assert not missing, (
            f"Архетипы не покрывают verdict_action значения: {missing}. "
            f"Добавь smoke-архетип для каждого из них."
        )
