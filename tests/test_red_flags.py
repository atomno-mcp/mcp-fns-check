"""Тесты check_for_red_flags: 4 базовые проверки + агрегатор.

Отдельный блок — `TestRegistryFreshness`: пустой или устаревший реестр обязан
давать «не проверено» с причиной, а не «в списке не значится». Это тот самый
случай, из-за которого клиент может заключить сделку, опираясь на пустоту.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from atomno_mcp_fns_check.context import ServiceContext
from atomno_mcp_fns_check.db.cache import SQLiteCache
from atomno_mcp_fns_check.db.registries import RegistryStore
from atomno_mcp_fns_check.errors import ValidationError
from atomno_mcp_fns_check.sources.disqualified import DisqualifiedClient
from atomno_mcp_fns_check.sources.efrsb import EFRSB_BASE_URL, EFRSB_SEARCH_PATH, EfrsbClient
from atomno_mcp_fns_check.sources.egrul import EGRUL_BASE_URL, EgrulClient, EgrulSearchHit
from atomno_mcp_fns_check.tools.red_flags import (
    check_bankruptcy_records,
    check_disqualified_director,
    check_for_red_flags,
    check_mass_address,
    check_mass_director,
)

from .source_mocks import (
    mock_disqualified_clean,
    mock_disqualified_hit,
    mock_disqualified_unavailable,
    seed_fresh_registry,
    seed_stale_registry,
)

TOKEN = "tk-rf"
EFRSB_URL = f"{EFRSB_BASE_URL}{EFRSB_SEARCH_PATH}"
INN = "7707083893"


def _mock_egrul(m: respx.MockRouter, payload: dict) -> None:
    m.post(f"{EGRUL_BASE_URL}/").mock(return_value=httpx.Response(200, json={"t": TOKEN}))
    m.get(f"{EGRUL_BASE_URL}/search-result/{TOKEN}").mock(
        return_value=httpx.Response(200, json=payload)
    )


async def _make_ctx(tmp_path, *, registries: RegistryStore | None) -> ServiceContext:
    return ServiceContext.for_testing(
        egrul=EgrulClient(backoff_base=0.0),
        efrsb=EfrsbClient(),
        cache=SQLiteCache(tmp_path / "cache.sqlite", default_ttl_hours=24),
        registries=registries,
        disqualified=DisqualifiedClient(),
    )


@pytest.fixture
async def ctx(tmp_path):
    """Контекст со свежими, наполненными реестрами."""
    registries = RegistryStore(tmp_path / "reg.sqlite")
    c = await _make_ctx(tmp_path, registries=registries)
    await c.__aenter__()

    await registries.upsert_mass_addresses(
        [{"address": "127015, г Москва, ул Бумажная, д 1", "registered_entities_count": 50}]
    )
    await seed_fresh_registry(registries, "mass_addresses", rows=1)

    await registries.upsert_mass_directors(
        [
            {
                "director_inn": "770700000099",
                "full_name": "ИВАНОВ ИВАН ИВАНОВИЧ",
                "companies_count": 87,
            }
        ]
    )
    await seed_fresh_registry(registries, "mass_directors", rows=1)

    yield c
    await c.__aexit__(None, None, None)


@pytest.fixture
async def empty_ctx(tmp_path):
    """Контекст с подключённой, но пустой базой реестров (ETL не запускался)."""
    registries = RegistryStore(tmp_path / "empty.sqlite")
    await registries.init()
    c = await _make_ctx(tmp_path, registries=registries)
    await c.__aenter__()
    yield c
    await c.__aexit__(None, None, None)


@pytest.fixture
async def stale_ctx(tmp_path):
    """Контекст с наполненными, но давно не обновлявшимися реестрами."""
    registries = RegistryStore(tmp_path / "stale.sqlite")
    c = await _make_ctx(tmp_path, registries=registries)
    await c.__aenter__()
    await registries.upsert_mass_addresses(
        [{"address": "г Москва, ул Старая, д 1", "registered_entities_count": 40}]
    )
    await seed_stale_registry(registries, "mass_addresses", rows=1)
    await registries.upsert_mass_directors(
        [{"director_inn": "770700000099", "full_name": "СТАРЫЙ С.С.", "companies_count": 12}]
    )
    await seed_stale_registry(registries, "mass_directors", rows=1)
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


# --- mass_address ---


class TestCheckMassAddress:
    async def test_hit_high(self, ctx):
        result = await check_mass_address(ctx, _hit(address="г Москва, ул Бумажная, д 1"))
        assert result.flag is not None
        assert result.flag.level == "high"
        assert "массовой регистрации" in result.flag.message_ru
        # У положительного совпадения обязана быть дата данных ФНС.
        assert result.flag.details["registry_data_as_of"]

    async def test_no_address(self, ctx):
        """Поиск ЕГРЮЛ адрес не отдаёт — проверка помечается невыполненной."""
        result = await check_mass_address(ctx, _hit(address=None))
        assert result.flag is None
        assert result.error is None
        assert result.skipped_reason is not None
        assert "адрес" in result.skipped_reason.lower()

    async def test_address_from_caller_is_used(self, ctx):
        """Адрес, переданный вызывающим, включает проверку."""
        result = await check_mass_address(
            ctx,
            _hit(address=None),
            address="г Москва, ул Бумажная, д 1",
        )
        assert result.flag is not None
        assert result.flag.level == "high"

    async def test_caller_address_wins_over_egrul(self, ctx):
        result = await check_mass_address(
            ctx,
            _hit(address="ул Несуществующая, 999"),
            address="г Москва, ул Бумажная, д 1",
        )
        assert result.flag is not None

    async def test_no_match_on_fresh_registry_is_clean(self, ctx):
        result = await check_mass_address(ctx, _hit(address="ул Несуществующая, 999"))
        assert result.flag is None
        assert result.skipped_reason is None
        assert result.error is None

    async def test_no_registries_skipped(self, tmp_path):
        c = await _make_ctx(tmp_path, registries=None)
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
        assert result.flag.details["role"] == "leader"

    async def test_founder_is_reported_as_founder(self, tmp_path):
        """Массовый учредитель не должен выдаваться за руководителя."""
        registries = RegistryStore(tmp_path / "r.sqlite")
        c = await _make_ctx(tmp_path, registries=registries)
        async with c:
            await registries.upsert_mass_directors(
                [
                    {
                        "director_inn": "770700000011",
                        "full_name": "УЧРЕДИТЕЛЬ У.У.",
                        "companies_count": 33,
                        "role": "founder",
                    }
                ]
            )
            await seed_fresh_registry(registries, "mass_directors", rows=1)
            hit = _hit(raw={"director_inn": "770700000011"})
            result = await check_mass_director(c, hit)

        assert result.flag is not None
        assert result.flag.details["role"] == "founder"
        assert "учредител" in result.flag.message_ru.lower()

    async def test_hit_by_name_only(self, ctx):
        result = await check_mass_director(ctx, _hit(director_name="ИВАНОВ ИВАН ИВАНОВИЧ"))
        assert result.flag is not None
        assert result.flag.details["match_precision"] == "approximate_name_only"

    async def test_no_director_skipped(self, ctx):
        result = await check_mass_director(ctx, _hit())
        assert result.flag is None
        assert result.skipped_reason is not None

    async def test_no_match_on_fresh_registry_is_clean(self, ctx):
        result = await check_mass_director(ctx, _hit(director_name="Незнакомец Н.Н."))
        assert result.flag is None
        assert result.error is None


# --- честность реестров: пустой и устаревший ---


class TestRegistryFreshness:
    """Незагруженный или устаревший реестр = «не проверено», а не «чисто»."""

    async def test_empty_mass_address_registry_returns_error(self, empty_ctx):
        result = await check_mass_address(empty_ctx, _hit(address="ул Бумажная, 1"))
        assert result.flag is None
        assert result.skipped_reason is None
        assert result.error is not None
        assert result.error.error == "registry_empty"
        assert "пуста" in result.error.message_ru

    async def test_empty_mass_director_registry_returns_error(self, empty_ctx):
        hit = _hit(director_name="Кто Угодно", raw={"director_inn": "770700000099"})
        result = await check_mass_director(empty_ctx, hit)
        assert result.flag is None
        assert result.error is not None
        assert result.error.error == "registry_empty"

    async def test_stale_mass_address_registry_returns_error(self, stale_ctx):
        """Реестр наполнен, но выгрузка ФНС 900-дневной давности."""
        result = await check_mass_address(stale_ctx, _hit(address="ул Совершенно Другая, 5"))
        assert result.flag is None
        assert result.skipped_reason is None
        assert result.error is not None
        assert result.error.error == "registry_stale"
        assert "устарели" in result.error.message_ru
        assert "ничего не доказывает" in result.error.message_ru

    async def test_stale_mass_director_registry_returns_error(self, stale_ctx):
        hit = _hit(director_name="Никому Не Известный", raw={"director_inn": "770700000077"})
        result = await check_mass_director(stale_ctx, hit)
        assert result.flag is None
        assert result.error is not None
        assert result.error.error == "registry_stale"

    async def test_stale_registry_still_reports_positive_match(self, stale_ctx):
        """Совпадение по устаревшим данным — всё равно риск, но с датой данных."""
        result = await check_mass_address(stale_ctx, _hit(address="г Москва, ул Старая, д 1"))
        assert result.flag is not None
        assert result.flag.details["registry_data_as_of"]
        assert "по данным ФНС от" in result.flag.message_ru

    async def test_empty_registry_never_lands_in_checks_passed(self, empty_ctx):
        """Главное свойство: пустой реестр не даёт «проверка пройдена»."""
        clean = {
            "rows": [
                {
                    "i": INN,
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
            mock_disqualified_clean(m)
            report = await check_for_red_flags(empty_ctx, INN)

        error_codes = {e.code for e in report.errors}
        assert {"mass_address", "mass_director"} <= error_codes
        assert "mass_address" not in report.checks_passed
        assert "mass_director" not in report.checks_passed
        assert report.flags == []
        # И итоговый уровень не «низкий»: часть проверок не отработала.
        assert report.overall_risk_level == "unknown"
        assert "не определён" in report.summary_ru


# --- disqualified (живой реестр ФНС) ---


class TestCheckDisqualifiedDirector:
    async def test_active_disqualification_high(self, ctx):
        with respx.mock() as m:
            mock_disqualified_hit(m, company_inn=INN)
            result = await check_disqualified_director(ctx, _hit())
        assert result.flag is not None
        assert result.flag.level == "high"
        assert result.flag.details["active_disqualifications"] == 1
        assert result.flag.details["match_precision"] == "exact_company_inn"

    async def test_expired_returns_medium(self, ctx):
        with respx.mock() as m:
            mock_disqualified_hit(
                m,
                company_inn=INN,
                date_from="01.01.2019 00:00:00",
                date_until="01.01.2020 00:00:00",
            )
            result = await check_disqualified_director(ctx, _hit())
        assert result.flag is not None
        assert result.flag.level == "medium"
        assert result.flag.details["active_disqualifications"] == 0

    async def test_no_match_is_clean(self, ctx):
        with respx.mock() as m:
            mock_disqualified_clean(m)
            result = await check_disqualified_director(ctx, _hit())
        assert result.flag is None
        assert result.error is None

    async def test_client_not_connected_is_skipped(self, tmp_path):
        c = ServiceContext.for_testing(
            egrul=EgrulClient(),
            efrsb=EfrsbClient(),
            cache=SQLiteCache(tmp_path / "c.sqlite"),
            registries=None,
            disqualified=None,
        )
        async with c:
            result = await check_disqualified_director(c, _hit())
        assert result.flag is None
        assert result.skipped_reason is not None

    async def test_source_unavailable_becomes_error_not_clean(self, ctx):
        """Реестр не ответил — проверка попадает в ошибки, а не в «чисто»."""
        clean = {
            "rows": [
                {"i": INN, "o": "1027700132195", "n": "ООО Тест", "k": "ul", "a": "ул Ч, 1"}
            ]
        }
        with respx.mock() as m:
            _mock_egrul(m, clean)
            m.get(EFRSB_URL).mock(return_value=httpx.Response(200, json={"pageData": []}))
            mock_disqualified_unavailable(m)
            report = await check_for_red_flags(ctx, INN)

        assert any(e.code == "disqualified_director" for e in report.errors)
        assert "disqualified_director" not in report.checks_passed


# --- bankruptcy_records ---


class TestCheckBankruptcyRecords:
    async def test_active_bankruptcy_high(self, ctx):
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
            result = await check_bankruptcy_records(ctx, _hit())
        assert result.flag is not None
        assert result.flag.level == "high"
        assert result.flag.details["cases_count"] == 1

    async def test_observation_medium(self, ctx):
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
            result = await check_bankruptcy_records(ctx, _hit())
        assert result.flag is not None
        assert result.flag.level == "medium"

    async def test_no_cases(self, ctx):
        with respx.mock() as m:
            m.get(EFRSB_URL).mock(return_value=httpx.Response(200, json={"pageData": []}))
            result = await check_bankruptcy_records(ctx, _hit())
        assert result.flag is None
        assert result.skipped_reason is None


# --- агрегатор ---


def _sber_payload(**extra_root):
    row = {
        "i": INN,
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
        """Все четыре проверки ответили, флагов нет — только тогда «низкий»."""
        clean = {
            "rows": [
                {
                    "i": INN,
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
            mock_disqualified_clean(m)
            report = await check_for_red_flags(ctx, INN)

        assert report.overall_risk_level == "low"
        assert report.overall_risk_score == 0
        assert report.flags == []
        assert report.errors == []
        assert report.coverage.is_complete
        assert report.coverage.answered == 4
        assert "Все источники ответили" in report.summary_ru

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
            mock_disqualified_clean(m)
            report = await check_for_red_flags(ctx, INN)

        codes = {f.code for f in report.flags}
        assert {"mass_address", "mass_director", "bankruptcy_records"} <= codes
        assert report.overall_risk_level == "high"
        assert report.overall_risk_score >= 50

    async def test_efrsb_failure_recorded_as_error(self, ctx):
        clean = {
            "rows": [
                {"i": INN, "o": "1027700132195", "n": "ООО Тест", "k": "ul", "a": "ул Чистая, 100"}
            ]
        }
        with respx.mock() as m:
            _mock_egrul(m, clean)
            m.get(EFRSB_URL).mock(return_value=httpx.Response(503))
            mock_disqualified_clean(m)
            report = await check_for_red_flags(ctx, INN)

        assert any(e.code == "bankruptcy_records" for e in report.errors)
        # Одна проверка молчит — «низкий риск» показывать нельзя.
        assert report.overall_risk_level == "unknown"
        assert not report.coverage.is_complete

    async def test_uses_cache_for_egrul(self, ctx):
        with respx.mock() as m:
            route_post = m.post(f"{EGRUL_BASE_URL}/").mock(
                return_value=httpx.Response(200, json={"t": TOKEN})
            )
            m.get(f"{EGRUL_BASE_URL}/search-result/{TOKEN}").mock(
                return_value=httpx.Response(200, json=_sber_payload())
            )
            m.get(EFRSB_URL).mock(return_value=httpx.Response(200, json={"pageData": []}))
            mock_disqualified_clean(m)

            await check_for_red_flags(ctx, INN)
            calls_after_first = route_post.call_count
            await check_for_red_flags(ctx, INN)
            assert route_post.call_count == calls_after_first
