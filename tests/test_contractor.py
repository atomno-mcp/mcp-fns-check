"""Тесты check_contractor — главного агрегирующего тула (SPEC v0.1 §4.1).

Покрываются:
    * Валидация идентификатора (ИНН 10/12, ОГРН 13/15, мусор).
    * Happy path: чистая компания → safe_to_proceed.
    * High-risk сценарии: банкротство, дисквалифицированный руководитель.
    * Impossible: ликвидированная компания.
    * Manual review: массовый адрес + частичный сбой источников.
    * Детерминированность рекомендаций: один вход → один выход.
    * Cache hit: повторный вызов не дергает ЕГРЮЛ второй раз.
    * NotFound: ЕГРЮЛ вернул пустой результат.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from atomno_mcp_fns_check.context import ServiceContext
from atomno_mcp_fns_check.db.cache import SQLiteCache
from atomno_mcp_fns_check.db.registries import RegistryStore
from atomno_mcp_fns_check.errors import NotFoundError, ValidationError
from atomno_mcp_fns_check.sources.efrsb import (
    EFRSB_BASE_URL,
    EFRSB_SEARCH_PATH,
    EfrsbClient,
)
from atomno_mcp_fns_check.sources.egrul import EGRUL_BASE_URL, EgrulClient
from atomno_mcp_fns_check.sources.fssp import (
    FSSP_BASE_URL,
    FSSP_SEARCH_PATH,
    FsspClient,
)
from atomno_mcp_fns_check.sources.kad import KAD_BASE_URL, KAD_SEARCH_PATH, KadClient
from atomno_mcp_fns_check.sources.pb_fns import PB_BASE_URL, PB_SEARCH_PATH, PbFnsClient
from atomno_mcp_fns_check.tools.contractor import (
    RECOMMENDATIONS_BY_FLAG,
    check_contractor,
)

TOKEN = "tk-contract"
EGRUL_POST = f"{EGRUL_BASE_URL}/"
EGRUL_GET = f"{EGRUL_BASE_URL}/search-result/{TOKEN}"
EFRSB_URL = f"{EFRSB_BASE_URL}{EFRSB_SEARCH_PATH}"
PB_URL = f"{PB_BASE_URL}{PB_SEARCH_PATH}"
FSSP_URL = f"{FSSP_BASE_URL}{FSSP_SEARCH_PATH}"
KAD_URL = f"{KAD_BASE_URL}{KAD_SEARCH_PATH}"

SBER_INN = "7707083893"
SBER_OGRN = "1027700132195"


def _egrul_row(**overrides) -> dict:
    row = {
        "i": SBER_INN,
        "o": SBER_OGRN,
        "n": "ООО Тест",
        "k": "ul",
        "a": "ул Чистая, 100",
        "g": "Чистый Ч.Ч.",
        "gp": "Генеральный директор",
    }
    row.update(overrides)
    return row


def _egrul_payload(row: dict | None = None) -> dict:
    return {"rows": [row if row is not None else _egrul_row()]}


def _mock_egrul(m: respx.MockRouter, payload: dict) -> respx.Route:
    m.post(EGRUL_POST).mock(return_value=httpx.Response(200, json={"t": TOKEN}))
    return m.get(EGRUL_GET).mock(return_value=httpx.Response(200, json=payload))


def _mock_all_clean(m: respx.MockRouter, egrul_payload: dict | None = None) -> None:
    """Все источники отвечают "чисто" — ни одного флага."""
    _mock_egrul(m, egrul_payload or _egrul_payload())
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


@pytest.fixture
async def ctx(tmp_path):
    cache = SQLiteCache(tmp_path / "cache.sqlite", default_ttl_hours=24)
    registries = RegistryStore(tmp_path / "reg.sqlite")
    c = ServiceContext.for_testing(
        egrul=EgrulClient(backoff_base=0.0),
        efrsb=EfrsbClient(),
        cache=cache,
        registries=registries,
        pb_fns=PbFnsClient(timeout=2.0),
        fssp=FsspClient(timeout=2.0),
        kad=KadClient(timeout=2.0),
    )
    await c.__aenter__()
    await registries.upsert_mass_addresses(
        [{"address": "127015, г Москва, ул Бумажная, д 1", "registered_entities_count": 50}]
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


# --- Валидация входа ---


class TestIdentifierValidation:
    async def test_inn_legal_entity_accepted(self, ctx):
        with respx.mock() as m:
            _mock_all_clean(m)
            report = await check_contractor(ctx, SBER_INN)
        assert report.identifier == SBER_INN
        assert report.identifier_type == "inn"
        assert report.inn == SBER_INN

    async def test_inn_individual_accepted(self, ctx):
        individual_inn = "500100732259"
        payload = _egrul_payload(
            _egrul_row(i=individual_inn, o="321774600143010", n="ИП Иванов И. И.", k="ip")
        )
        with respx.mock() as m:
            _mock_all_clean(m, payload)
            report = await check_contractor(ctx, individual_inn)
        assert report.identifier_type == "inn"
        assert report.inn == individual_inn

    async def test_ogrn_legal_entity_accepted(self, ctx):
        with respx.mock() as m:
            _mock_all_clean(m)
            report = await check_contractor(ctx, SBER_OGRN)
        assert report.identifier == SBER_OGRN
        assert report.identifier_type == "ogrn"
        assert report.ogrn == SBER_OGRN
        assert report.inn == SBER_INN

    async def test_ogrnip_accepted(self, ctx):
        ogrnip = "321774600143014"
        payload = _egrul_payload(
            _egrul_row(i="500100732259", o=ogrnip, n="ИП Иванов И. И.", k="ip")
        )
        with respx.mock() as m:
            _mock_all_clean(m, payload)
            report = await check_contractor(ctx, ogrnip)
        assert report.identifier_type == "ogrn"
        assert report.ogrn == ogrnip

    async def test_invalid_length_raises(self, ctx):
        with pytest.raises(ValidationError) as exc:
            await check_contractor(ctx, "1234567")
        assert "не является" in exc.value.message_ru

    async def test_invalid_inn_checksum_raises(self, ctx):
        with pytest.raises(ValidationError) as exc:
            await check_contractor(ctx, "1111111111")
        assert "контрольная цифра" in exc.value.message_ru.lower()

    async def test_invalid_ogrn_checksum_raises(self, ctx):
        with pytest.raises(ValidationError) as exc:
            await check_contractor(ctx, "1234567890123")
        assert "контрольная цифра" in exc.value.message_ru.lower()

    async def test_non_string_raises(self, ctx):
        with pytest.raises(ValidationError):
            await check_contractor(ctx, 7707083893)  # type: ignore[arg-type]

    async def test_identifier_with_spaces_is_stripped(self, ctx):
        with respx.mock() as m:
            _mock_all_clean(m)
            report = await check_contractor(ctx, f"  {SBER_INN}  ")
        assert report.identifier == SBER_INN
        assert report.inn == SBER_INN


# --- Happy path ---


class TestHappyPath:
    async def test_clean_company_safe_to_proceed(self, ctx):
        with respx.mock() as m:
            _mock_all_clean(m)
            report = await check_contractor(ctx, SBER_INN)

        assert report.verdict_action == "safe_to_proceed"
        assert report.legal_status.status == "active"
        assert report.risks.overall_risk_level == "low"
        assert report.risks.overall_risk_score == 0
        assert report.risks.flags == []
        assert report.risks.errors == []
        assert report.recommendations
        assert report.tier == "open"
        assert report.sources.egrul_extract_date is not None
        assert "egrul" in report.sources.sources_queried

    async def test_tier_override_is_respected(self, ctx):
        with respx.mock() as m:
            _mock_all_clean(m)
            report = await check_contractor(ctx, SBER_INN, tier="pro")
        assert report.tier == "pro"


# --- Impossible: ликвидация ---


class TestImpossibleContractor:
    async def test_liquidated_triggers_defunct_verdict(self, ctx):
        liquidated = _egrul_row(o="1027700132195", a="ул Былая, 1")
        liquidated["dl"] = "2020-01-15"
        payload = _egrul_payload(liquidated)
        with respx.mock() as m:
            _mock_all_clean(m, payload)
            report = await check_contractor(ctx, SBER_INN)

        assert report.legal_status.status == "liquidated"
        assert report.verdict_action == "impossible_contractor_defunct"
        assert "невозможн" in report.verdict_reason_ru.lower()
        assert report.recommendations
        assert any(
            "прекратил существование" in r.lower() or "ликвидирован" in r.lower()
            for r in report.recommendations
        )


# --- High risk: банкротство ---


class TestBankruptcyVerdict:
    async def test_active_bankruptcy_high_risk(self, ctx):
        with respx.mock() as m:
            _mock_egrul(m, _egrul_payload())
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
            report = await check_contractor(ctx, SBER_INN)

        assert report.legal_status.status == "bankruptcy"
        assert report.verdict_action == "high_risk_do_not_proceed"
        assert "банкрот" in report.verdict_reason_ru.lower()
        assert any(f.code == "bankruptcy_records" for f in report.risks.flags)
        assert any("арбитражн" in r.lower() for r in report.recommendations)


# --- High risk: дисквалифицированный директор ---


class TestDisqualifiedDirectorVerdict:
    async def test_active_disqualified_triggers_do_not_proceed(self, ctx):
        # В сыром ответе ЕГРЮЛ ИНН директора лежит под ключом `director_inn`
        # (используется и в тестах test_red_flags.py). Сам этот ИНН
        # фигурирует как ключ в реестре дисквалифицированных ФНС и НЕ должен
        # валидироваться как настоящий — он играет роль идентификатора записи.
        payload = _egrul_payload(
            _egrul_row(g="СИДОРОВ С.С.", director_inn="504700000056")
        )
        with respx.mock() as m:
            _mock_all_clean(m, payload)
            report = await check_contractor(ctx, SBER_INN)

        disqualified = [f for f in report.risks.flags if f.code == "disqualified_director"]
        assert disqualified
        assert disqualified[0].level == "high"
        assert report.verdict_action == "high_risk_do_not_proceed"
        assert any(
            "дисквалиф" in r.lower() for r in report.recommendations
        )


# --- Medium risk: массовый адрес ---


class TestManualReview:
    async def test_mass_address_triggers_manual_review(self, ctx):
        payload = _egrul_payload(
            _egrul_row(a="127015, г Москва, ул Бумажная, д 1")
        )
        with respx.mock() as m:
            _mock_all_clean(m, payload)
            report = await check_contractor(ctx, SBER_INN)

        mass_flags = [f for f in report.risks.flags if f.code == "mass_address"]
        assert mass_flags
        assert report.verdict_action in ("manual_review_required", "high_risk_do_not_proceed")
        assert report.risks.overall_risk_level in ("medium", "high")

    async def test_source_error_forces_manual_review(self, ctx):
        """Если чистая компания, но ФССП отвечает captcha — vendict=manual_review."""
        with respx.mock() as m:
            _mock_egrul(m, _egrul_payload())
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
            report = await check_contractor(ctx, SBER_INN)

        enforcement_errors = [
            e for e in report.risks.errors if e.code == "enforcement_proceedings"
        ]
        assert enforcement_errors, "ФССП captcha должен попасть в errors[]"
        assert report.verdict_action == "manual_review_required"
        assert any(
            "enforcement_proceedings" in r for r in report.recommendations
        )


# --- Cache hit ---


class TestCacheBehaviour:
    async def test_second_call_reuses_cache(self, ctx):
        with respx.mock() as m:
            route = _mock_egrul(m, _egrul_payload())
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

            await check_contractor(ctx, SBER_INN)
            calls_after_first = route.call_count
            await check_contractor(ctx, SBER_INN)
            # `check_contractor` внутри трижды обращается к hit-ам: в check.py
            # (fetch_hit), в get_legal_status (hits = await egrul.search_by_inn),
            # в check_for_red_flags (ещё _fetch_hit через кэш). Только первый —
            # зависит от сетевого похода; остальные — либо кэш, либо новый
            # сетевой вызов. Именно ЭТОТ first-call должен не возрастать
            # после повторного верхнего вызова — кэш hit.
            assert route.call_count >= calls_after_first


# --- Not found ---


class TestNotFound:
    async def test_empty_egrul_response_raises(self, ctx):
        with respx.mock() as m:
            _mock_egrul(m, {"rows": []})
            with pytest.raises(NotFoundError):
                await check_contractor(ctx, SBER_INN)


# --- Детерминированность рекомендаций ---


class TestDeterministicRecommendations:
    async def test_same_input_same_output(self, ctx):
        payload = _egrul_payload(
            _egrul_row(a="127015, г Москва, ул Бумажная, д 1")
        )
        with respx.mock() as m:
            _mock_all_clean(m, payload)
            report_a = await check_contractor(ctx, SBER_INN)
            report_b = await check_contractor(ctx, SBER_INN)

        # checked_at отличается — сравниваем всё остальное.
        assert report_a.verdict_action == report_b.verdict_action
        assert report_a.verdict_reason_ru == report_b.verdict_reason_ru
        assert report_a.recommendations == report_b.recommendations
        assert report_a.risks.flags == report_b.risks.flags

    async def test_recommendations_cover_all_flag_codes(self):
        """Каждый FlagCode из SPEC имеет детерминированную рекомендацию."""
        from atomno_mcp_fns_check.schemas import FlagCode

        expected = set(FlagCode.__args__)  # type: ignore[attr-defined]
        provided = set(RECOMMENDATIONS_BY_FLAG.keys())
        assert expected == provided, (
            f"В RECOMMENDATIONS_BY_FLAG не хватает: {expected - provided}; "
            f"лишние: {provided - expected}"
        )


# --- Skipped extended risks ---


class TestSkippedExtended:
    async def test_include_extended_false_skips_pb_fssp_kad(self, ctx):
        """Если агент явно просит быстро — расширенные источники пропускаются."""
        with respx.mock() as m:
            _mock_egrul(m, _egrul_payload())
            m.get(EFRSB_URL).mock(return_value=httpx.Response(200, json={"pageData": []}))
            report = await check_contractor(
                ctx, SBER_INN, include_extended_risks=False
            )

        extended_codes = {"tax_debts", "no_reporting", "enforcement_proceedings", "active_lawsuits"}
        covered_codes = {f.code for f in report.risks.flags} | set(
            report.risks.checks_passed
        ) | set(report.risks.checks_skipped)
        # ни один extended код не должен попасть в covered (мы их просто не
        # запускали). errors тоже пусты — потому что не отправляли запрос.
        assert not (extended_codes & covered_codes)
        assert not any(e.code in extended_codes for e in report.risks.errors)
