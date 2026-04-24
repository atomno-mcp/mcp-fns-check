"""Тесты check_for_red_flags S4 — 4 новые проверки + extended-агрегатор."""

from __future__ import annotations

import httpx
import pytest
import respx

from atomno_mcp_fns_check.context import ServiceContext
from atomno_mcp_fns_check.db.cache import SQLiteCache
from atomno_mcp_fns_check.db.registries import RegistryStore
from atomno_mcp_fns_check.sources.efrsb import EFRSB_BASE_URL, EFRSB_SEARCH_PATH, EfrsbClient
from atomno_mcp_fns_check.sources.egrul import EGRUL_BASE_URL, EgrulClient, EgrulSearchHit
from atomno_mcp_fns_check.sources.fssp import FSSP_BASE_URL, FSSP_SEARCH_PATH, FsspClient
from atomno_mcp_fns_check.sources.kad import KAD_BASE_URL, KAD_SEARCH_PATH, KadClient
from atomno_mcp_fns_check.sources.pb_fns import PB_BASE_URL, PB_SEARCH_PATH, PbFnsClient
from atomno_mcp_fns_check.tools.red_flags import (
    check_active_lawsuits,
    check_enforcement_proceedings,
    check_for_red_flags,
    check_no_reporting,
    check_tax_debts,
)

TOKEN = "tk-rf-ext"
EGRUL_POST = f"{EGRUL_BASE_URL}/"
EGRUL_GET = f"{EGRUL_BASE_URL}/search-result/{TOKEN}"
EFRSB_URL = f"{EFRSB_BASE_URL}{EFRSB_SEARCH_PATH}"
PB_URL = f"{PB_BASE_URL}{PB_SEARCH_PATH}"
FSSP_URL = f"{FSSP_BASE_URL}{FSSP_SEARCH_PATH}"
KAD_URL = f"{KAD_BASE_URL}{KAD_SEARCH_PATH}"


@pytest.fixture
async def ctx(tmp_path):
    cache = SQLiteCache(tmp_path / "cache.sqlite", default_ttl_hours=24)
    registries = RegistryStore(tmp_path / "reg.sqlite")
    egrul = EgrulClient(backoff_base=0.0)
    efrsb = EfrsbClient()
    pb = PbFnsClient(timeout=2.0)
    fssp = FsspClient(timeout=2.0)
    kad = KadClient(timeout=2.0)
    c = ServiceContext.for_testing(
        egrul=egrul,
        efrsb=efrsb,
        cache=cache,
        registries=registries,
        pb_fns=pb,
        fssp=fssp,
        kad=kad,
    )
    await c.__aenter__()
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


def _sber_payload():
    return {
        "rows": [
            {
                "i": "7707083893",
                "o": "1027700132195",
                "n": "ПАО СБЕРБАНК",
                "k": "ul",
                "a": "ул Тестовая, 1",
                "g": "ИВАНОВ И.И.",
            }
        ]
    }


# --- tax_debts ---


class TestCheckTaxDebts:
    async def test_no_pb_client_skipped(self, tmp_path):
        c = ServiceContext.for_testing(
            egrul=EgrulClient(),
            efrsb=EfrsbClient(),
            cache=SQLiteCache(tmp_path / "c.sqlite"),
            pb_fns=None,
        )
        async with c:
            r = await check_tax_debts(c, _hit())
        assert r.flag is None
        assert r.skipped_reason is not None

    async def test_hit_medium(self, ctx):
        with respx.mock() as m:
            m.get(PB_URL).mock(
                return_value=httpx.Response(
                    200, json={"ul": {"data": [{"tags": ["tax_debt"]}]}}
                )
            )
            r = await check_tax_debts(ctx, _hit())
        assert r.flag is not None
        assert r.flag.code == "tax_debts"
        assert r.flag.level == "medium"

    async def test_no_hit(self, ctx):
        with respx.mock() as m:
            m.get(PB_URL).mock(
                return_value=httpx.Response(200, json={"ul": {"data": [{"tags": []}]}})
            )
            r = await check_tax_debts(ctx, _hit())
        assert r.flag is None


# --- no_reporting ---


class TestCheckNoReporting:
    async def test_hit_high(self, ctx):
        with respx.mock() as m:
            m.get(PB_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={"ul": {"data": [{"tags": ["no_reporting"]}]}},
                )
            )
            r = await check_no_reporting(ctx, _hit())
        assert r.flag is not None
        assert r.flag.code == "no_reporting"
        assert r.flag.level == "high"

    async def test_no_hit(self, ctx):
        with respx.mock() as m:
            m.get(PB_URL).mock(
                return_value=httpx.Response(200, json={"ul": {"data": [{"tags": []}]}})
            )
            r = await check_no_reporting(ctx, _hit())
        assert r.flag is None


# --- enforcement_proceedings ---


class TestCheckEnforcement:
    async def test_no_cases(self, ctx):
        with respx.mock() as m:
            m.get(FSSP_URL).mock(
                return_value=httpx.Response(200, json={"response": {"result": []}})
            )
            r = await check_enforcement_proceedings(ctx, _hit())
        assert r.flag is None

    async def test_low_single_small(self, ctx):
        with respx.mock() as m:
            m.get(FSSP_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "response": {
                            "result": [
                                {
                                    "ip_number": "1",
                                    "debt_amount": 5000,
                                    "status": "Возбуждено",
                                }
                            ]
                        }
                    },
                )
            )
            r = await check_enforcement_proceedings(ctx, _hit())
        assert r.flag is not None
        assert r.flag.level == "low"
        assert r.flag.details["cases_count"] == 1

    async def test_high_many_or_big(self, ctx):
        with respx.mock() as m:
            m.get(FSSP_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "response": {
                            "result": [
                                {
                                    "ip_number": "x",
                                    "debt_amount": 5_000_000,
                                    "status": "Возбуждено",
                                }
                            ]
                        }
                    },
                )
            )
            r = await check_enforcement_proceedings(ctx, _hit())
        assert r.flag is not None
        assert r.flag.level == "high"

    async def test_captcha_recorded_via_aggregator(self, ctx):
        with respx.mock() as m:
            m.post(EGRUL_POST).mock(
                return_value=httpx.Response(200, json={"t": TOKEN})
            )
            m.get(EGRUL_GET).mock(return_value=httpx.Response(200, json=_sber_payload()))
            m.get(EFRSB_URL).mock(return_value=httpx.Response(200, json={"pageData": []}))
            m.get(PB_URL).mock(
                return_value=httpx.Response(200, json={"ul": {"data": [{"tags": []}]}})
            )
            m.get(FSSP_URL).mock(
                return_value=httpx.Response(200, json={"captcha": True})
            )
            m.post(KAD_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={"Result": {"Items": []}},
                    headers={"content-type": "application/json"},
                )
            )
            report = await check_for_red_flags(ctx, "7707083893", include_extended=True)
        assert any(e.code == "enforcement_proceedings" for e in report.errors)


# --- active_lawsuits ---


class TestCheckActiveLawsuits:
    async def test_no_cases(self, ctx):
        with respx.mock() as m:
            m.post(KAD_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={"Result": {"Items": []}},
                    headers={"content-type": "application/json"},
                )
            )
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


# --- aggregator with include_extended=True ---


class TestExtendedAggregator:
    async def test_eight_checks_clean(self, ctx):
        """Чистая компания со всеми 8 проверками."""
        with respx.mock() as m:
            m.post(EGRUL_POST).mock(
                return_value=httpx.Response(200, json={"t": TOKEN})
            )
            m.get(EGRUL_GET).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "rows": [
                            {
                                "i": "7707083893",
                                "o": "1027700132195",
                                "n": "ООО Чистая",
                                "k": "ul",
                                "a": "ул Совсем чистая, 1",
                                "g": "Чистый Ч.Ч.",
                            }
                        ]
                    },
                )
            )
            m.get(EFRSB_URL).mock(return_value=httpx.Response(200, json={"pageData": []}))
            m.get(PB_URL).mock(
                return_value=httpx.Response(200, json={"ul": {"data": [{"tags": []}]}})
            )
            m.get(FSSP_URL).mock(
                return_value=httpx.Response(200, json={"response": {"result": []}})
            )
            m.post(KAD_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={"Result": {"Items": []}},
                    headers={"content-type": "application/json"},
                )
            )
            report = await check_for_red_flags(
                ctx, "7707083893", include_extended=True, lawsuits_threshold_rub=1_000_000
            )
        assert report.overall_risk_level == "low"
        assert report.flags == []
        assert len(report.checks_passed) >= 4
        # Должны быть выполнены все 8 проверок (passed или skipped без error).
        assert all(e.code != "tax_debts" for e in report.errors)
        assert all(e.code != "no_reporting" for e in report.errors)

    async def test_eight_checks_dirty_high(self, ctx):
        """Грязная компания: налоги + нет отчётности + ИП + крупный иск."""
        with respx.mock() as m:
            m.post(EGRUL_POST).mock(
                return_value=httpx.Response(200, json={"t": TOKEN})
            )
            m.get(EGRUL_GET).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "rows": [
                            {
                                "i": "7707083893",
                                "o": "1027700132195",
                                "n": "ООО Грязная",
                                "k": "ul",
                                "a": "ул Чистая, 100",
                                "g": "Никто Н.Н.",
                            }
                        ]
                    },
                )
            )
            m.get(EFRSB_URL).mock(return_value=httpx.Response(200, json={"pageData": []}))
            m.get(PB_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={"ul": {"data": [{"tags": ["tax_debt", "no_reporting"]}]}},
                )
            )
            m.get(FSSP_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "response": {
                            "result": [
                                {
                                    "ip_number": "1",
                                    "debt_amount": 2_000_000,
                                    "status": "Возбуждено",
                                }
                            ]
                        }
                    },
                )
            )
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
                ctx, "7707083893", include_extended=True, lawsuits_threshold_rub=1_000_000
            )
        codes = {f.code for f in report.flags}
        assert "tax_debts" in codes
        assert "no_reporting" in codes
        assert "enforcement_proceedings" in codes
        assert "active_lawsuits" in codes
        assert report.overall_risk_level == "high"
        assert report.overall_risk_score >= 80
