"""Тесты расширенных проверок: налоги, приставы, арбитраж.

Что изменилось после ревизии источников 02.08.2026:

  * `tax_debts` берётся из открытых данных ФНС `debtam` (локальная таблица
    `tax_debts`), а не из «Прозрачного бизнеса» — в его ответе такого поля
    нет вообще, и проверка всегда врала «долгов нет»;
  * `enforcement_proceedings` ходит на актуальный адрес формы ФССП и разбирает
    настоящий JSONP-ответ;
  * проверки `no_reporting` больше нет: источника не существует, она всегда
    молчала и одна блокировала «низкий риск» для любой компании.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from atomno_mcp_fns_check.context import ServiceContext
from atomno_mcp_fns_check.db.cache import SQLiteCache
from atomno_mcp_fns_check.db.registries import RegistryStore
from atomno_mcp_fns_check.sources.disqualified import DisqualifiedClient
from atomno_mcp_fns_check.sources.efrsb import EFRSB_BASE_URL, EFRSB_SEARCH_PATH, EfrsbClient
from atomno_mcp_fns_check.sources.egrul import EGRUL_BASE_URL, EgrulClient, EgrulSearchHit
from atomno_mcp_fns_check.sources.fssp import FsspClient
from atomno_mcp_fns_check.sources.kad import KAD_BASE_URL, KAD_SEARCH_PATH, KadClient
from atomno_mcp_fns_check.tools.red_flags import (
    check_active_lawsuits,
    check_enforcement_proceedings,
    check_for_red_flags,
    check_tax_debts,
)

from .source_mocks import (
    mock_disqualified_clean,
    mock_fssp_captcha,
    mock_fssp_cases,
    mock_fssp_clean,
    mock_fssp_internal_error,
    seed_fresh_registry,
    seed_stale_registry,
)

TOKEN = "tk-rf-ext"
EGRUL_POST = f"{EGRUL_BASE_URL}/"
EGRUL_GET = f"{EGRUL_BASE_URL}/search-result/{TOKEN}"
EFRSB_URL = f"{EFRSB_BASE_URL}{EFRSB_SEARCH_PATH}"
KAD_URL = f"{KAD_BASE_URL}{KAD_SEARCH_PATH}"
INN = "7707083893"

#: Одна запись `debtam` в том виде, в каком её кладёт загрузчик реестров.
DEBT_ROW = {
    "inn": INN,
    "org_name": 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ДОЛЖНИК"',
    "total_debt_rub": 850_000.0,
    "penalty_rub": 120_000.0,
    "fine_rub": 45_000.0,
    "taxes": [
        {"name": "Налог на добавленную стоимость", "debt": 800_000.0, "penalty": 100_000.0, "fine": 45_000.0},
        {"name": "Транспортный налог", "debt": 50_000.0, "penalty": 20_000.0, "fine": 0.0},
    ],
    "report_date": "2026-07-01",
}


async def _make_ctx(tmp_path, *, registries: RegistryStore | None) -> ServiceContext:
    return ServiceContext.for_testing(
        egrul=EgrulClient(backoff_base=0.0),
        efrsb=EfrsbClient(),
        cache=SQLiteCache(tmp_path / "cache.sqlite", default_ttl_hours=24),
        registries=registries,
        fssp=FsspClient(timeout=2.0),
        kad=KadClient(timeout=2.0),
        disqualified=DisqualifiedClient(),
    )


@pytest.fixture
async def ctx(tmp_path):
    """Реестры наполнены и свежие; налоговых долгов у контрагента нет."""
    registries = RegistryStore(tmp_path / "reg.sqlite")
    c = await _make_ctx(tmp_path, registries=registries)
    await c.__aenter__()
    await registries.upsert_mass_addresses(
        [{"address": "г Москва, ул Массовая, д 1", "registered_entities_count": 60}]
    )
    await seed_fresh_registry(registries, "mass_addresses", rows=1)
    await registries.upsert_mass_directors(
        [{"director_inn": "770700009999", "full_name": "МАССОВЫЙ М.М.", "companies_count": 40}]
    )
    await seed_fresh_registry(registries, "mass_directors", rows=1)
    await registries.upsert_tax_debts(
        [dict(DEBT_ROW, inn="5000000009", org_name="ООО ДРУГОЙ ДОЛЖНИК")]
    )
    await seed_fresh_registry(registries, "tax_debts", rows=1)
    yield c
    await c.__aexit__(None, None, None)


def _hit(**overrides) -> EgrulSearchHit:
    base = dict(
        inn=INN,
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


def _egrul_payload(name: str = "ООО Чистая", address: str = "ул Совсем чистая, 1") -> dict:
    return {
        "rows": [
            {
                "i": INN,
                "o": "1027700132195",
                "n": name,
                "k": "ul",
                "a": address,
                "g": "Чистый Ч.Ч.",
            }
        ]
    }


def _mock_egrul(m: respx.MockRouter, payload: dict) -> None:
    m.post(EGRUL_POST).mock(return_value=httpx.Response(200, json={"t": TOKEN}))
    m.get(EGRUL_GET).mock(return_value=httpx.Response(200, json=payload))


def _mock_kad_clean(m: respx.MockRouter) -> None:
    m.post(KAD_URL).mock(
        return_value=httpx.Response(
            200, json={"Result": {"Items": []}}, headers={"content-type": "application/json"}
        )
    )


# --- tax_debts (открытые данные ФНС debtam) ---


class TestCheckTaxDebts:
    async def test_no_registry_skipped(self, tmp_path):
        c = await _make_ctx(tmp_path, registries=None)
        async with c:
            r = await check_tax_debts(c, _hit())
        assert r.flag is None
        assert r.skipped_reason is not None

    async def test_debt_over_million_is_high(self, ctx):
        await ctx.registries.upsert_tax_debts([DEBT_ROW])
        r = await check_tax_debts(ctx, _hit())
        assert r.flag is not None
        assert r.flag.code == "tax_debts"
        # 850 000 недоимки + 120 000 пеней + 45 000 штрафов > 1 млн
        assert r.flag.level == "high"
        assert r.flag.details["total_rub"] == pytest.approx(1_015_000.0)
        assert r.flag.details["source"] == "ФНС, открытые данные «debtam»"
        assert r.flag.details["registry_data_as_of"]

    async def test_small_debt_is_low(self, ctx):
        await ctx.registries.upsert_tax_debts(
            [dict(DEBT_ROW, total_debt_rub=1000.0, penalty_rub=200.0, fine_rub=0.0)]
        )
        r = await check_tax_debts(ctx, _hit())
        assert r.flag is not None
        assert r.flag.level == "low"

    async def test_medium_debt(self, ctx):
        await ctx.registries.upsert_tax_debts(
            [dict(DEBT_ROW, total_debt_rub=150_000.0, penalty_rub=0.0, fine_rub=0.0)]
        )
        r = await check_tax_debts(ctx, _hit())
        assert r.flag is not None
        assert r.flag.level == "medium"

    async def test_no_debt_on_fresh_registry_is_clean(self, ctx):
        r = await check_tax_debts(ctx, _hit())
        assert r.flag is None
        assert r.error is None

    async def test_empty_registry_returns_error_not_clean(self, tmp_path):
        """Не загрузили debtam — обязаны сказать «не проверено»."""
        registries = RegistryStore(tmp_path / "empty.sqlite")
        await registries.init()
        c = await _make_ctx(tmp_path, registries=registries)
        async with c:
            r = await check_tax_debts(c, _hit())
        assert r.flag is None
        assert r.error is not None
        assert r.error.error == "registry_empty"

    async def test_stale_registry_returns_error_not_clean(self, tmp_path):
        """Выгрузка ФНС старая — «долгов нет» ничего не доказывает."""
        registries = RegistryStore(tmp_path / "stale.sqlite")
        c = await _make_ctx(tmp_path, registries=registries)
        async with c:
            await registries.upsert_tax_debts(
                [dict(DEBT_ROW, inn="5000000009", org_name="ООО ДРУГОЙ")]
            )
            await seed_stale_registry(registries, "tax_debts", rows=1, days_old=400)
            r = await check_tax_debts(c, _hit())
        assert r.flag is None
        assert r.error is not None
        assert r.error.error == "registry_stale"


# --- набор проверок: их семь, отчётности среди них нет ---


class TestCheckSet:
    async def test_no_reporting_check_is_gone(self, ctx):
        """Проверки отчётности больше нет ни в наборе, ни в весах, ни в кодах."""
        from atomno_mcp_fns_check.tools.red_flags import FLAG_WEIGHTS

        assert "no_reporting" not in FLAG_WEIGHTS
        assert len(FLAG_WEIGHTS) == 7

        with respx.mock() as m:
            _mock_egrul(m, _egrul_payload())
            m.get(EFRSB_URL).mock(return_value=httpx.Response(200, json={"pageData": []}))
            mock_disqualified_clean(m)
            mock_fssp_clean(m)
            _mock_kad_clean(m)
            report = await check_for_red_flags(ctx, INN, include_extended=True)

        assert report.coverage.total == 7
        assert "no_reporting" not in report.checks_passed
        assert all(e.code != "no_reporting" for e in report.errors)


# --- enforcement_proceedings (ФССП) ---


class TestCheckEnforcement:
    async def test_no_cases(self, ctx):
        with respx.mock() as m:
            mock_fssp_clean(m)
            r = await check_enforcement_proceedings(ctx, _hit())
        assert r.flag is None
        assert r.error is None

    async def test_low_single_small(self, ctx):
        with respx.mock() as m:
            mock_fssp_cases(
                m, [{"number": "12345/24/77001-ИП", "amount_text": "5 000,00"}]
            )
            r = await check_enforcement_proceedings(ctx, _hit())
        assert r.flag is not None
        assert r.flag.level == "low"
        assert r.flag.details["cases_count"] == 1

    async def test_high_big_amount(self, ctx):
        with respx.mock() as m:
            mock_fssp_cases(
                m, [{"number": "12345/24/77001-ИП", "amount_text": "5 000 000,00"}]
            )
            r = await check_enforcement_proceedings(ctx, _hit())
        assert r.flag is not None
        assert r.flag.level == "high"

    async def test_high_many_cases(self, ctx):
        with respx.mock() as m:
            mock_fssp_cases(
                m,
                [
                    {"number": f"1234{i}/24/77001-ИП", "amount_text": "1 000,00"}
                    for i in range(5)
                ],
            )
            r = await check_enforcement_proceedings(ctx, _hit())
        assert r.flag is not None
        assert r.flag.level == "high"
        assert r.flag.details["cases_count"] == 5

    async def test_captcha_recorded_as_error_not_clean(self, ctx):
        """Настоящее поведение ФССП сегодня: код с картинки."""
        with respx.mock() as m:
            _mock_egrul(m, _egrul_payload())
            m.get(EFRSB_URL).mock(return_value=httpx.Response(200, json={"pageData": []}))
            mock_disqualified_clean(m)
            mock_fssp_captcha(m)
            _mock_kad_clean(m)
            report = await check_for_red_flags(ctx, INN, include_extended=True)

        errors = [e for e in report.errors if e.code == "enforcement_proceedings"]
        assert errors
        assert "enforcement_proceedings" not in report.checks_passed

    async def test_internal_error_recorded_as_error(self, ctx):
        with respx.mock() as m:
            mock_fssp_internal_error(m)
            with pytest.raises(Exception) as exc:
                await check_enforcement_proceedings(ctx, _hit())
        assert "внутренней ошибкой" in str(exc.value)


# --- active_lawsuits ---


class TestCheckActiveLawsuits:
    async def test_no_cases(self, ctx):
        with respx.mock() as m:
            _mock_kad_clean(m)
            r = await check_active_lawsuits(ctx, _hit())
        assert r.flag is None

    async def test_threshold_filters_out_small(self, ctx):
        with respx.mock() as m:
            m.post(KAD_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "Result": {
                            "Items": [
                                {
                                    "CaseNumber": "А40-1/2025",
                                    "Role": "Ответчик",
                                    "Amount": 50_000,
                                    "Status": "Принято",
                                }
                            ]
                        }
                    },
                    headers={"content-type": "application/json"},
                )
            )
            r = await check_active_lawsuits(ctx, _hit(), threshold_rub=1_000_000)
        assert r.flag is None
        assert r.skipped_reason is not None

    async def test_threshold_keeps_big(self, ctx):
        with respx.mock() as m:
            m.post(KAD_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "Result": {
                            "Items": [
                                {
                                    "CaseNumber": "А40-1/2025",
                                    "Role": "Ответчик",
                                    "Amount": 7_000_000,
                                    "Status": "Принято",
                                }
                            ]
                        }
                    },
                    headers={"content-type": "application/json"},
                )
            )
            r = await check_active_lawsuits(ctx, _hit(), threshold_rub=1_000_000)
        assert r.flag is not None
        assert r.flag.level == "high"
        assert r.flag.details["threshold_rub"] == 1_000_000


# --- агрегатор с расширенными проверками ---


class TestExtendedAggregator:
    async def test_clean_company_reaches_low_risk(self, ctx):
        """Все семь проверок ответили, ничего не нашли — «низкий риск» законен."""
        with respx.mock() as m:
            _mock_egrul(m, _egrul_payload())
            m.get(EFRSB_URL).mock(return_value=httpx.Response(200, json={"pageData": []}))
            mock_disqualified_clean(m)
            mock_fssp_clean(m)
            _mock_kad_clean(m)
            report = await check_for_red_flags(
                ctx, INN, include_extended=True, lawsuits_threshold_rub=1_000_000
            )

        assert report.flags == []
        assert report.errors == []
        assert report.overall_risk_level == "low"
        assert report.coverage.total == 7
        assert report.coverage.answered == 7
        assert report.coverage.failed == 0
        assert report.coverage.is_complete
        assert "Все источники ответили" in report.summary_ru

    async def test_single_silent_source_still_blocks_low_risk(self, ctx):
        """Главная защита цела: молчит один источник — «низкого риска» нет."""
        with respx.mock() as m:
            _mock_egrul(m, _egrul_payload())
            m.get(EFRSB_URL).mock(return_value=httpx.Response(200, json={"pageData": []}))
            mock_disqualified_clean(m)
            mock_fssp_captcha(m)  # ФССП просит код с картинки
            _mock_kad_clean(m)
            report = await check_for_red_flags(
                ctx, INN, include_extended=True, lawsuits_threshold_rub=1_000_000
            )

        assert report.flags == []
        assert report.overall_risk_level == "unknown"
        assert report.coverage.total == 7
        assert report.coverage.answered == 6
        assert report.coverage.failed == 1
        assert not report.coverage.is_complete
        assert "не определён" in report.summary_ru

    async def test_dirty_company_high(self, ctx):
        await ctx.registries.upsert_tax_debts([DEBT_ROW])
        with respx.mock() as m:
            _mock_egrul(m, _egrul_payload(name="ООО Грязная", address="г Москва, ул Массовая, д 1"))
            m.get(EFRSB_URL).mock(return_value=httpx.Response(200, json={"pageData": []}))
            mock_disqualified_clean(m)
            mock_fssp_cases(m, [{"number": "999/24/77001-ИП", "amount_text": "2 000 000,00"}])
            m.post(KAD_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "Result": {
                            "Items": [
                                {
                                    "CaseNumber": "А40-9000/2025",
                                    "Role": "Ответчик",
                                    "Amount": 6_000_000,
                                    "Status": "Принято",
                                }
                            ]
                        }
                    },
                    headers={"content-type": "application/json"},
                )
            )
            report = await check_for_red_flags(
                ctx, INN, include_extended=True, lawsuits_threshold_rub=1_000_000
            )

        codes = {f.code for f in report.flags}
        assert {"mass_address", "tax_debts", "enforcement_proceedings", "active_lawsuits"} <= codes
        assert report.overall_risk_level == "high"
        assert report.overall_risk_score >= 80

    async def test_high_risk_survives_incomplete_coverage(self, ctx):
        """Найденный риск не должен теряться из-за неполноты проверок."""
        await ctx.registries.upsert_tax_debts([DEBT_ROW])
        with respx.mock() as m:
            _mock_egrul(m, _egrul_payload())
            m.get(EFRSB_URL).mock(return_value=httpx.Response(503))
            mock_disqualified_clean(m)
            mock_fssp_captcha(m)
            _mock_kad_clean(m)
            report = await check_for_red_flags(ctx, INN, include_extended=True)

        assert any(f.code == "tax_debts" for f in report.flags)
        assert report.overall_risk_level in ("medium", "high")
        # Молчат ЕФРСБ и ФССП — две проверки из семи.
        assert report.coverage.failed == 2
        assert not report.coverage.is_complete
