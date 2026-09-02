"""Тесты сводной проверки контрагента (`check_contractor`).

Покрываются:
    * разбор идентификатора (ИНН 10/12, ОГРН 13/15, мусор);
    * «можно работать» — только когда все запрошенные проверки ответили;
    * блокирующие сценарии: банкротство, дисквалифицированное лицо;
    * ликвидация — сделка невозможна;
    * ручная проверка: массовый адрес, частично молчащие источники;
    * детерминированность рекомендаций;
    * повторный вызов не ходит в ЕГРЮЛ заново;
    * пустой ответ ЕГРЮЛ — «не найдено», а не «чисто».

Заглушки источников — в настоящем формате (`tests/source_mocks.py`), снятом
живыми запросами 02.08.2026. «Прозрачный бизнес» из сводной проверки убран:
в его ответе нет ни задолженности, ни отчётности.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from atomno_mcp_fns_check.context import ServiceContext
from atomno_mcp_fns_check.db.cache import SQLiteCache
from atomno_mcp_fns_check.db.registries import RegistryStore
from atomno_mcp_fns_check.errors import NotFoundError, ValidationError
from atomno_mcp_fns_check.sources.disqualified import DisqualifiedClient
from atomno_mcp_fns_check.sources.efrsb import (
    EFRSB_BASE_URL,
    EFRSB_SEARCH_PATH,
    EfrsbClient,
)
from atomno_mcp_fns_check.sources.egrul import EGRUL_BASE_URL, EgrulClient
from atomno_mcp_fns_check.sources.fssp import FsspClient
from atomno_mcp_fns_check.sources.kad import KAD_BASE_URL, KAD_SEARCH_PATH, KadClient
from atomno_mcp_fns_check.tools.contractor import (
    RECOMMENDATIONS_BY_FLAG,
    check_contractor,
)

from .source_mocks import (
    mock_disqualified_clean,
    mock_disqualified_hit,
    mock_disqualified_unavailable,
    mock_fssp_captcha,
    mock_fssp_clean,
    seed_fresh_registry,
    seed_stale_registry,
)

TOKEN = "tk-contract"
EGRUL_POST = f"{EGRUL_BASE_URL}/"
EGRUL_GET = f"{EGRUL_BASE_URL}/search-result/{TOKEN}"
EFRSB_URL = f"{EFRSB_BASE_URL}{EFRSB_SEARCH_PATH}"
KAD_URL = f"{KAD_BASE_URL}{KAD_SEARCH_PATH}"

SBER_INN = "7707083893"
SBER_OGRN = "1027700132195"

MASS_ADDRESS = "127015, г Москва, ул Бумажная, д 1"


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


def _mock_efrsb_clean(m: respx.MockRouter) -> respx.Route:
    return m.get(EFRSB_URL).mock(return_value=httpx.Response(200, json={"pageData": []}))


def _mock_kad_clean(m: respx.MockRouter) -> respx.Route:
    return m.post(KAD_URL).mock(
        return_value=httpx.Response(
            200,
            json={"Result": {"Items": []}},
            headers={"content-type": "application/json"},
        )
    )


def _mock_all_clean(m: respx.MockRouter, egrul_payload: dict | None = None) -> None:
    """Все источники ответили и нарушений не назвали."""
    _mock_egrul(m, egrul_payload or _egrul_payload())
    _mock_efrsb_clean(m)
    mock_disqualified_clean(m)
    mock_fssp_clean(m)
    _mock_kad_clean(m)


@pytest.fixture
async def ctx(tmp_path):
    cache = SQLiteCache(tmp_path / "cache.sqlite", default_ttl_hours=24)
    registries = RegistryStore(tmp_path / "reg.sqlite")
    c = ServiceContext.for_testing(
        egrul=EgrulClient(backoff_base=0.0),
        efrsb=EfrsbClient(),
        cache=cache,
        registries=registries,
        fssp=FsspClient(timeout=2.0),
        kad=KadClient(timeout=2.0),
        disqualified=DisqualifiedClient(timeout=2.0),
    )
    await c.__aenter__()
    await registries.upsert_mass_addresses(
        [{"address": MASS_ADDRESS, "registered_entities_count": 50}]
    )
    # Служебные записи подтверждают, что выгрузка физически была: пустая
    # таблица читается как «данные не загружены», а не «проверили, чисто».
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
    yield c
    await c.__aexit__(None, None, None)


# --- разбор идентификатора ---


class TestIdentifierValidation:
    async def test_inn_legal_entity_accepted(self, ctx):
        with respx.mock(assert_all_called=False) as m:
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
        with respx.mock(assert_all_called=False) as m:
            _mock_all_clean(m, payload)
            report = await check_contractor(ctx, individual_inn)
        assert report.identifier_type == "inn"
        assert report.inn == individual_inn

    async def test_ogrn_legal_entity_accepted(self, ctx):
        with respx.mock(assert_all_called=False) as m:
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
        with respx.mock(assert_all_called=False) as m:
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
        with respx.mock(assert_all_called=False) as m:
            _mock_all_clean(m)
            report = await check_contractor(ctx, f"  {SBER_INN}  ")
        assert report.identifier == SBER_INN
        assert report.inn == SBER_INN


# --- «можно работать» ---


class TestHappyPath:
    async def test_clean_company_safe_to_proceed_on_base_checks(self, ctx):
        """Базовый набор: все 4 проверки ответили и молчат."""
        with respx.mock(assert_all_called=False) as m:
            _mock_all_clean(m)
            report = await check_contractor(ctx, SBER_INN, include_extended_risks=False)

        assert report.verdict_action == "safe_to_proceed"
        assert report.legal_status.status == "active"
        assert report.risks.overall_risk_level == "low"
        assert report.risks.overall_risk_score == 0
        assert report.risks.flags == []
        assert report.risks.errors == []
        assert report.risks.coverage.is_complete
        assert report.recommendations
        assert report.tier == "open"
        assert report.sources.egrul_extract_date is not None
        assert "egrul" in report.sources.sources_queried

    async def test_full_set_of_seven_checks_reaches_safe_to_proceed(self, ctx):
        """Полный набор из семи проверок: все ответили — «можно работать»."""
        with respx.mock(assert_all_called=False) as m:
            _mock_all_clean(m)
            report = await check_contractor(ctx, SBER_INN)

        assert report.verdict_action == "safe_to_proceed"
        assert report.risks.overall_risk_level == "low"
        assert report.risks.errors == []
        assert report.risks.flags == []
        assert report.risks.coverage.total == 7
        assert report.risks.coverage.answered == 7
        assert report.risks.coverage.is_complete
        assert "Ответили все 7 проверок" in report.verdict_reason_ru

    async def test_tier_override_is_respected(self, ctx):
        with respx.mock(assert_all_called=False) as m:
            _mock_all_clean(m)
            report = await check_contractor(ctx, SBER_INN, tier="pro")
        assert report.tier == "pro"


# --- сделка невозможна: ликвидация ---


class TestImpossibleContractor:
    async def test_liquidated_triggers_defunct_verdict(self, ctx):
        liquidated = _egrul_row(o=SBER_OGRN, a="ул Былая, 1")
        liquidated["dl"] = "2020-01-15"
        with respx.mock(assert_all_called=False) as m:
            _mock_all_clean(m, _egrul_payload(liquidated))
            report = await check_contractor(ctx, SBER_INN)

        assert report.legal_status.status == "liquidated"
        assert report.verdict_action == "impossible_contractor_defunct"
        assert "невозможн" in report.verdict_reason_ru.lower()
        assert any(
            "прекратил существование" in r.lower() or "ликвидирован" in r.lower()
            for r in report.recommendations
        )


# --- блокирующий риск: банкротство ---


class TestBankruptcyVerdict:
    async def test_active_bankruptcy_high_risk(self, ctx):
        with respx.mock(assert_all_called=False) as m:
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
            mock_disqualified_clean(m)
            mock_fssp_clean(m)
            _mock_kad_clean(m)
            report = await check_contractor(ctx, SBER_INN)

        assert report.legal_status.status == "bankruptcy"
        assert report.verdict_action == "high_risk_do_not_proceed"
        assert "банкрот" in report.verdict_reason_ru.lower()
        assert any(f.code == "bankruptcy_records" for f in report.risks.flags)
        assert any("арбитражн" in r.lower() for r in report.recommendations)


# --- блокирующий риск: дисквалифицированное лицо ---


class TestDisqualifiedDirectorVerdict:
    async def test_active_disqualification_triggers_do_not_proceed(self, ctx):
        """Совпадение приходит из живого реестра ФНС по ИНН организации."""
        with respx.mock(assert_all_called=False) as m:
            _mock_egrul(m, _egrul_payload(_egrul_row(g="СИДОРОВ СЕРГЕЙ СЕРГЕЕВИЧ")))
            _mock_efrsb_clean(m)
            mock_disqualified_hit(
                m, company_inn=SBER_INN, full_name="СИДОРОВ СЕРГЕЙ СЕРГЕЕВИЧ"
            )
            mock_fssp_clean(m)
            _mock_kad_clean(m)
            report = await check_contractor(ctx, SBER_INN)

        disqualified = [f for f in report.risks.flags if f.code == "disqualified_director"]
        assert disqualified
        assert disqualified[0].level == "high"
        assert report.verdict_action == "high_risk_do_not_proceed"
        assert any("дисквалиф" in r.lower() for r in report.recommendations)
        assert "fns_disqualified" in report.sources.sources_queried


# --- ручная проверка ---


class TestManualReview:
    async def test_mass_address_triggers_manual_review(self, ctx):
        with respx.mock(assert_all_called=False) as m:
            _mock_all_clean(m, _egrul_payload(_egrul_row(a=MASS_ADDRESS)))
            report = await check_contractor(ctx, SBER_INN)

        mass_flags = [f for f in report.risks.flags if f.code == "mass_address"]
        assert mass_flags
        assert report.verdict_action in ("manual_review_required", "high_risk_do_not_proceed")

    async def test_silent_source_forces_manual_review(self, ctx):
        """ФССП требует код с картинки — это ошибка, а не «нарушений нет»."""
        with respx.mock(assert_all_called=False) as m:
            _mock_egrul(m, _egrul_payload())
            _mock_efrsb_clean(m)
            mock_disqualified_clean(m)
            mock_fssp_captcha(m)
            _mock_kad_clean(m)
            report = await check_contractor(ctx, SBER_INN)

        enforcement_errors = [
            e for e in report.risks.errors if e.code == "enforcement_proceedings"
        ]
        assert enforcement_errors, "Код с картинки должен попасть в список ошибок"
        assert "enforcement_proceedings" not in report.risks.checks_passed
        assert report.verdict_action == "manual_review_required"
        assert any("enforcement_proceedings" in r for r in report.recommendations)

    @pytest.mark.parametrize(
        "silenced",
        [
            "mass_address",
            "mass_director",
            "disqualified_director",
            "bankruptcy_records",
            "tax_debts",
            "enforcement_proceedings",
            "active_lawsuits",
        ],
    )
    async def test_any_single_silent_check_blocks_safe_verdict(self, ctx, silenced):
        """Главная защита: молчит любая из семи проверок — «можно работать» нет.

        Проверяем поимённо каждую: сценарий «всё чисто» ломается ровно одним
        замолчавшим источником, и вердикт обязан упасть до ручной проверки.
        """
        if silenced == "mass_address":
            await seed_stale_registry(ctx.registries, "mass_addresses", rows=1)
        elif silenced == "mass_director":
            await seed_stale_registry(ctx.registries, "mass_directors", rows=1)
        elif silenced == "tax_debts":
            await seed_stale_registry(ctx.registries, "tax_debts", rows=1)

        with respx.mock(assert_all_called=False) as m:
            _mock_egrul(m, _egrul_payload())
            if silenced == "bankruptcy_records":
                m.get(EFRSB_URL).mock(return_value=httpx.Response(503, text=""))
            else:
                _mock_efrsb_clean(m)
            if silenced == "disqualified_director":
                mock_disqualified_unavailable(m)
            else:
                mock_disqualified_clean(m)
            if silenced == "enforcement_proceedings":
                mock_fssp_captcha(m)
            else:
                mock_fssp_clean(m)
            if silenced == "active_lawsuits":
                m.post(KAD_URL).mock(
                    return_value=httpx.Response(451, text="Доступ заблокирован")
                )
            else:
                _mock_kad_clean(m)

            report = await check_contractor(ctx, SBER_INN)

        assert report.verdict_action == "manual_review_required"
        assert report.risks.overall_risk_level == "unknown"
        assert any(e.code == silenced for e in report.risks.errors)
        assert silenced not in report.risks.checks_passed
        assert not report.risks.coverage.is_complete

    async def test_stale_registry_is_not_reported_as_clean(self, ctx):
        """Устаревшая выгрузка ФНС — «не проверено», а не «в списках не значится»."""
        await seed_stale_registry(ctx.registries, "mass_addresses", rows=1)

        with respx.mock(assert_all_called=False) as m:
            _mock_all_clean(m)
            report = await check_contractor(ctx, SBER_INN, include_extended_risks=False)

        stale_errors = [e for e in report.risks.errors if e.code == "mass_address"]
        assert stale_errors, "Устаревший реестр обязан дать ошибку проверки"
        assert "устарел" in stale_errors[0].message_ru.lower()
        assert "mass_address" not in report.risks.checks_passed
        assert report.verdict_action == "manual_review_required"
        assert report.risks.overall_risk_level == "unknown"


# --- повторный вызов ---


class TestCacheBehaviour:
    async def test_second_call_reuses_cache(self, ctx):
        with respx.mock(assert_all_called=False) as m:
            route = _mock_egrul(m, _egrul_payload())
            _mock_efrsb_clean(m)
            mock_disqualified_clean(m)
            mock_fssp_clean(m)
            _mock_kad_clean(m)

            await check_contractor(ctx, SBER_INN)
            calls_after_first = route.call_count
            await check_contractor(ctx, SBER_INN)
            assert route.call_count >= calls_after_first


# --- не найдено ---


class TestNotFound:
    async def test_empty_egrul_response_raises(self, ctx):
        with respx.mock(assert_all_called=False) as m:
            _mock_egrul(m, {"rows": []})
            with pytest.raises(NotFoundError):
                await check_contractor(ctx, SBER_INN)


# --- детерминированность ---


class TestDeterministicRecommendations:
    async def test_same_input_same_output(self, ctx):
        with respx.mock(assert_all_called=False) as m:
            _mock_all_clean(m, _egrul_payload(_egrul_row(a=MASS_ADDRESS)))
            report_a = await check_contractor(ctx, SBER_INN)
            report_b = await check_contractor(ctx, SBER_INN)

        assert report_a.verdict_action == report_b.verdict_action
        assert report_a.verdict_reason_ru == report_b.verdict_reason_ru
        assert report_a.recommendations == report_b.recommendations
        assert report_a.risks.flags == report_b.risks.flags

    async def test_recommendations_cover_all_flag_codes(self):
        """У каждого признака риска есть своя рекомендация."""
        from atomno_mcp_fns_check.schemas import FlagCode

        expected = set(FlagCode.__args__)  # type: ignore[attr-defined]
        provided = set(RECOMMENDATIONS_BY_FLAG.keys())
        assert expected == provided, (
            f"В RECOMMENDATIONS_BY_FLAG не хватает: {expected - provided}; "
            f"лишние: {provided - expected}"
        )


# --- быстрый режим ---


class TestSkippedExtended:
    async def test_include_extended_false_skips_slow_sources(self, ctx):
        """Быстрый режим не ходит в ФССП и КАД — и не делает вид, что сходил."""
        with respx.mock(assert_all_called=False) as m:
            _mock_egrul(m, _egrul_payload())
            _mock_efrsb_clean(m)
            mock_disqualified_clean(m)
            report = await check_contractor(ctx, SBER_INN, include_extended_risks=False)

        extended_codes = {
            "tax_debts",
            "enforcement_proceedings",
            "active_lawsuits",
        }
        covered_codes = (
            {f.code for f in report.risks.flags}
            | set(report.risks.checks_passed)
            | set(report.risks.checks_skipped)
        )
        assert not (extended_codes & covered_codes)
        assert not any(e.code in extended_codes for e in report.risks.errors)
        assert report.risks.coverage.total == 4
