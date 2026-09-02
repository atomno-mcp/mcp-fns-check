"""Сквозные тесты типовых контрагентов через `check_contractor`.

Каждый тест — один типовой контрагент: полный прогон со всеми источниками на
заглушках настоящего формата, ассерт на вердикт, набор сработавших признаков,
жизненный статус и полноту опроса источников.

Что важно понимать про вердикты после ревизии источников 02.08.2026:

  * проверок семь: проверка «не сдаёт отчётность» убрана — публичного источника
    по отчётности нет, и вечно молчащая проверка одна блокировала «можно
    работать» для любой компании;
  * «можно работать» достижимо и на базовом наборе из 4 проверок, и на полном
    из 7 — но только когда все они реально ответили и ничего не нашли;
  * стоит замолчать любому источнику — вердикт падает до «нужна ручная
    проверка». Это не регресс, а требование правила честности;
  * реестры открытых данных должны быть помечены свежей выгрузкой, иначе
    проверка отвечает «не проверено» вместо «чисто».
"""

from __future__ import annotations

import pytest
import respx

from atomno_mcp_fns_check.context import ServiceContext
from atomno_mcp_fns_check.db.cache import SQLiteCache
from atomno_mcp_fns_check.db.registries import RegistryStore
from atomno_mcp_fns_check.sources.disqualified import DisqualifiedClient
from atomno_mcp_fns_check.sources.efrsb import EfrsbClient
from atomno_mcp_fns_check.sources.egrul import EgrulClient
from atomno_mcp_fns_check.sources.fssp import FsspClient
from atomno_mcp_fns_check.sources.kad import KadClient
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
    archetype_tax_debtor,
    mock_scenario,
    preload_mass_address,
    preload_mass_director,
    preload_tax_debt,
)
from .source_mocks import seed_fresh_registry


@pytest.fixture
async def smoke_ctx(tmp_path):
    """Контекст с наполненными и свежими реестрами.

    Реестры не должны быть пустыми: пустая таблица — это «данные не загружены»,
    а не «проверили, чисто» (правило честности, прецедент 2026-07-27). Служебные
    записи ниже не совпадают ни с одним типовым контрагентом и только
    подтверждают, что выгрузка физически была.
    """
    cache = SQLiteCache(tmp_path / "cache.sqlite", default_ttl_hours=24)
    registries = RegistryStore(tmp_path / "reg.sqlite")
    ctx = ServiceContext.for_testing(
        egrul=EgrulClient(backoff_base=0.0),
        efrsb=EfrsbClient(),
        cache=cache,
        registries=registries,
        fssp=FsspClient(timeout=2.0),
        kad=KadClient(timeout=2.0),
        disqualified=DisqualifiedClient(timeout=2.0),
    )
    await ctx.__aenter__()
    await registries.upsert_mass_addresses(
        [{"address": "служебная запись, ни с чем не совпадает", "registered_entities_count": 1}]
    )
    await registries.upsert_mass_directors(
        [{"director_inn": "000000000000", "full_name": "СЛУЖЕБНАЯ ЗАПИСЬ", "companies_count": 1}]
    )
    await registries.upsert_tax_debts(
        [
            {
                "inn": "000000000000",
                "org_name": "СЛУЖЕБНАЯ ЗАПИСЬ",
                "total_debt_rub": 1.0,
                "report_date": "2026-07-01",
            }
        ]
    )
    for registry in ("mass_addresses", "mass_directors", "tax_debts"):
        await seed_fresh_registry(registries, registry, rows=1)
    yield ctx
    await ctx.__aexit__(None, None, None)


def _flag_codes(report) -> set[str]:
    return {f.code for f in report.risks.flags}


# --- #1 чистая действующая компания ---


class TestArchetypeCleanLegal:
    async def test_base_checks_only_gives_safe_to_proceed(self, smoke_ctx):
        """Все 4 базовые проверки ответили и молчат — «можно работать»."""
        with respx.mock(assert_all_called=False) as m:
            mock_scenario(m, archetype_clean_legal())
            report = await check_contractor(
                smoke_ctx, INN_LEGAL_SBER, include_extended_risks=False
            )

        assert report.verdict_action == "safe_to_proceed"
        assert report.legal_status.status == "active"
        assert report.risks.overall_risk_level == "low"
        assert _flag_codes(report) == set()
        assert report.risks.errors == []
        assert report.risks.coverage.is_complete
        assert report.risks.coverage.answered == 4
        assert report.identifier_type == "inn"
        assert report.inn == INN_LEGAL_SBER

    async def test_full_set_of_seven_checks_gives_safe_to_proceed(self, smoke_ctx):
        """Полный набор: все семь проверок ответили и молчат — «можно работать»."""
        with respx.mock(assert_all_called=False) as m:
            mock_scenario(m, archetype_clean_legal())
            report = await check_contractor(smoke_ctx, INN_LEGAL_SBER)

        assert report.verdict_action == "safe_to_proceed"
        assert _flag_codes(report) == set()
        assert report.risks.errors == []
        assert report.risks.overall_risk_level == "low"
        assert report.risks.coverage.is_complete
        assert report.risks.coverage.total == 7
        assert report.risks.coverage.answered == 7


# --- #2 действующий ИП ---


class TestArchetypeActiveIndividual:
    async def test_ip_clean_safe_to_proceed(self, smoke_ctx):
        with respx.mock(assert_all_called=False) as m:
            mock_scenario(m, archetype_active_individual())
            report = await check_contractor(
                smoke_ctx, INN_INDIVIDUAL_VALID, include_extended_risks=False
            )

        assert report.verdict_action == "safe_to_proceed"
        assert report.legal_status.status == "active"
        assert _flag_codes(report) == set()
        assert report.card.type == "individual"
        assert report.inn == INN_INDIVIDUAL_VALID


# --- #3 ликвидирована ---


class TestArchetypeLiquidated:
    async def test_liquidated_impossible_verdict(self, smoke_ctx):
        with respx.mock(assert_all_called=False) as m:
            mock_scenario(m, archetype_liquidated())
            report = await check_contractor(smoke_ctx, INN_LEGAL_SBER)

        assert report.verdict_action == "impossible_contractor_defunct"
        assert report.legal_status.status == "liquidated"
        assert any(
            "прекратил существование" in r.lower() or "ликвидирован" in r.lower()
            for r in report.recommendations
        ), f"Ожидалась рекомендация о ликвидации, получили: {report.recommendations}"


# --- #4 активное банкротство ---


class TestArchetypeBankruptcyActive:
    async def test_konkursnoe_production_triggers_do_not_proceed(self, smoke_ctx):
        with respx.mock(assert_all_called=False) as m:
            mock_scenario(m, archetype_bankruptcy_active())
            report = await check_contractor(smoke_ctx, INN_LEGAL_SBER)

        assert report.verdict_action == "high_risk_do_not_proceed"
        assert report.legal_status.status == "bankruptcy"
        assert "bankruptcy_records" in _flag_codes(report)
        assert any(
            "арбитражн" in r.lower() or "банкрот" in r.lower()
            for r in report.recommendations
        )


# --- #5 дисквалифицированное лицо в управлении ---


class TestArchetypeDisqualifiedDirector:
    async def test_active_disqualification_blocks_contract(self, smoke_ctx):
        with respx.mock(assert_all_called=False) as m:
            mock_scenario(m, archetype_disqualified_director())
            report = await check_contractor(smoke_ctx, INN_LEGAL_SBER)

        assert report.verdict_action == "high_risk_do_not_proceed"
        disqualified = [f for f in report.risks.flags if f.code == "disqualified_director"]
        assert disqualified, "Ожидался признак «дисквалифицированный руководитель»"
        assert disqualified[0].level == "high"
        assert any("дисквалиф" in r.lower() for r in report.recommendations)

    async def test_source_is_credited_in_report(self, smoke_ctx):
        with respx.mock(assert_all_called=False) as m:
            mock_scenario(m, archetype_disqualified_director())
            report = await check_contractor(smoke_ctx, INN_LEGAL_SBER)

        assert "fns_disqualified" in report.sources.sources_queried


# --- #6 массовый адрес ---


class TestArchetypeMassAddress:
    async def test_mass_address_triggers_manual_review(self, smoke_ctx):
        await preload_mass_address(smoke_ctx)

        with respx.mock(assert_all_called=False) as m:
            mock_scenario(m, archetype_mass_address())
            report = await check_contractor(smoke_ctx, INN_LEGAL_SBER)

        assert "mass_address" in _flag_codes(report)
        assert report.verdict_action == "manual_review_required"


# --- #7 массовый руководитель ---


class TestArchetypeMassDirector:
    async def test_mass_director_42_companies_is_high_risk(self, smoke_ctx):
        await preload_mass_director(smoke_ctx, companies_count=42)

        with respx.mock(assert_all_called=False) as m:
            mock_scenario(m, archetype_mass_director())
            report = await check_contractor(smoke_ctx, INN_LEGAL_SBER)

        mass = [f for f in report.risks.flags if f.code == "mass_director"]
        assert mass, "Ожидался признак «массовый руководитель»"
        assert mass[0].level == "high"
        assert "руководител" in mass[0].message_ru.lower()
        assert report.verdict_action in (
            "manual_review_required",
            "high_risk_do_not_proceed",
        )


# --- #8 налоговая недоимка (открытые данные ФНС `debtam`) ---


class TestArchetypeTaxDebtor:
    async def test_tax_debt_from_debtam_raises_risk(self, smoke_ctx):
        await preload_tax_debt(smoke_ctx, INN_LEGAL_SBER, total_debt_rub=3_000_000.0)

        with respx.mock(assert_all_called=False) as m:
            mock_scenario(m, archetype_tax_debtor())
            report = await check_contractor(smoke_ctx, INN_LEGAL_SBER)

        debts = [f for f in report.risks.flags if f.code == "tax_debts"]
        assert debts, "Ожидался признак «налоговая задолженность»"
        assert debts[0].level == "high"
        assert report.verdict_action in (
            "manual_review_required",
            "high_risk_do_not_proceed",
        )
        assert any("debtam" in r or "ФНС" in r for r in report.recommendations)


# --- #9 крупные исполнительные производства ---


class TestArchetypeLargeEnforcement:
    async def test_enforcement_over_1m_flag_is_high_level(self, smoke_ctx):
        with respx.mock(assert_all_called=False) as m:
            mock_scenario(m, archetype_large_enforcement())
            report = await check_contractor(smoke_ctx, INN_LEGAL_SBER)

        enforcement = [f for f in report.risks.flags if f.code == "enforcement_proceedings"]
        assert enforcement, "Ожидался признак «исполнительные производства»"
        assert enforcement[0].level == "high"
        assert report.verdict_action == "manual_review_required"


# --- #10 ФССП требует код с картинки ---


class TestArchetypeFsspCaptchaGraceful:
    async def test_captcha_forces_manual_review_not_crash(self, smoke_ctx):
        with respx.mock(assert_all_called=False) as m:
            mock_scenario(m, archetype_fssp_captcha_degraded())
            report = await check_contractor(smoke_ctx, INN_LEGAL_SBER)

        enforcement_errors = [
            e for e in report.risks.errors if e.code == "enforcement_proceedings"
        ]
        assert enforcement_errors, "Код с картинки должен попасть в список ошибок"
        assert "картинки" in enforcement_errors[0].message_ru.lower()
        assert report.verdict_action == "manual_review_required"
        assert report.card is not None
        assert report.legal_status.status == "active"

    async def test_captcha_never_reads_as_clean(self, smoke_ctx):
        """Недоступный источник не может дать «низкий риск»."""
        with respx.mock(assert_all_called=False) as m:
            mock_scenario(m, archetype_fssp_captcha_degraded())
            report = await check_contractor(smoke_ctx, INN_LEGAL_SBER)

        assert "enforcement_proceedings" not in report.risks.checks_passed
        assert report.risks.overall_risk_level == "unknown"


# --- сводный тест покрытия вердиктов ---


class TestArchetypeCoverage:
    async def test_all_four_verdict_actions_covered(self, smoke_ctx):
        from atomno_mcp_fns_check.schemas import VerdictAction

        expected = set(VerdictAction.__args__)  # type: ignore[attr-defined]
        collected: set[str] = set()

        with respx.mock(assert_all_called=False) as m:
            mock_scenario(m, archetype_clean_legal())
            r = await check_contractor(
                smoke_ctx, INN_LEGAL_SBER, include_extended_risks=False
            )
            collected.add(r.verdict_action)

        with respx.mock(assert_all_called=False) as m:
            mock_scenario(m, archetype_liquidated())
            r = await check_contractor(smoke_ctx, INN_LEGAL_SBER)
            collected.add(r.verdict_action)

        with respx.mock(assert_all_called=False) as m:
            mock_scenario(m, archetype_bankruptcy_active())
            r = await check_contractor(smoke_ctx, INN_LEGAL_SBER)
            collected.add(r.verdict_action)

        with respx.mock(assert_all_called=False) as m:
            mock_scenario(m, archetype_fssp_captcha_degraded())
            r = await check_contractor(smoke_ctx, INN_LEGAL_SBER)
            collected.add(r.verdict_action)

        missing = expected - collected
        assert not missing, (
            f"Типовые контрагенты не покрывают вердикты: {missing}. "
            f"Добавь сценарий на каждый."
        )
