"""Фабрики ответов источников для сквозных тестов архетипов.

Все ответы — в НАСТОЯЩЕМ формате источников (см. `tests/source_mocks.py` и
`tests/fixtures/`, снято живыми запросами 02.08.2026). Прошлая версия этого
модуля собирала выдуманные структуры (`{"ul":{"data":[{"tags":[...]}]}}` для
«Прозрачного бизнеса», `{"response":{"result":[...]}}` для ФССП) — таких
ответов источники никогда не отдавали, и тесты были зелёными на собственной
ошибке разбора.

Что изменилось по существу:

  * «Прозрачный бизнес» больше не участвует в проверках риска — в его ответе
    нет ни задолженности, ни отчётности. Налоговые долги идут из открытых
    данных ФНС `debtam` (локальная таблица `tax_debts`);
  * реестр дисквалифицированных лиц опрашивается живьём на
    `service.nalog.ru`, а не читается из локальной выгрузки;
  * локальные реестры помечаются свежими явно — устаревший реестр по правилу
    честности даёт «не проверено», а не «чисто».
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
import respx

from atomno_mcp_fns_check.sources.efrsb import EFRSB_BASE_URL, EFRSB_SEARCH_PATH
from atomno_mcp_fns_check.sources.egrul import EGRUL_BASE_URL
from atomno_mcp_fns_check.sources.kad import KAD_BASE_URL, KAD_SEARCH_PATH

from .source_mocks import (
    mock_disqualified_clean,
    mock_disqualified_hit,
    mock_fssp_captcha,
    mock_fssp_cases,
    mock_fssp_clean,
    seed_fresh_registry,
)

EGRUL_POST_URL = f"{EGRUL_BASE_URL}/"
EFRSB_URL = f"{EFRSB_BASE_URL}{EFRSB_SEARCH_PATH}"
KAD_URL = f"{KAD_BASE_URL}{KAD_SEARCH_PATH}"


# --- Валидные архетипные идентификаторы (контрольные цифры сходятся) ---

INN_LEGAL_SBER = "7707083893"
OGRN_LEGAL_SBER = "1027700132195"

INN_INDIVIDUAL_VALID = "500100732259"
OGRNIP_INDIVIDUAL_VALID = "321774600143014"

DIRECTOR_NAME_DISQUALIFIED = "СИДОРОВ СЕРГЕЙ СЕРГЕЕВИЧ"

DIRECTOR_INN_MASS = "770700000042"
DIRECTOR_NAME_MASS = "ИВАНОВ И.И."

MASS_ADDRESS = "127015, г Москва, ул Бумажная, д 1"


def make_egrul_row(
    *,
    inn: str = INN_LEGAL_SBER,
    ogrn: str = OGRN_LEGAL_SBER,
    name: str = "ООО Тест",
    kind: str = "ul",
    address: str = "г Москва, ул Чистая, 100",
    director_name: str = "Чистый Ч.Ч.",
    director_position: str = "Генеральный директор",
    director_inn: str | None = None,
    liquidation_date: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "i": inn,
        "o": ogrn,
        "n": name,
        "k": kind,
        "a": address,
        "g": director_name,
        "gp": director_position,
    }
    if director_inn is not None:
        row["director_inn"] = director_inn
    if liquidation_date is not None:
        row["dl"] = liquidation_date
    return row


def make_egrul_payload(row: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"rows": [row if row is not None else make_egrul_row()]}


@dataclass(slots=True)
class ScenarioResponses:
    """Набор ответов источников для одного архетипа."""

    egrul_token: str = "tk-smoke"
    egrul_rows: list[dict[str, Any]] = field(default_factory=list)
    efrsb_cases: list[dict[str, Any]] = field(default_factory=list)
    #: Исполнительные производства ФССП: [{"number": ..., "amount_text": ...}]
    fssp_cases: list[dict[str, Any]] = field(default_factory=list)
    fssp_captcha: bool = False
    kad_items: list[dict[str, Any]] = field(default_factory=list)
    #: Дисквалифицированное лицо в управлении организации (живой реестр ФНС).
    disqualified_company_inn: str | None = None
    disqualified_person: str = DIRECTOR_NAME_DISQUALIFIED

    egrul_status: int = 200
    efrsb_status: int = 200
    kad_status: int = 200


def mock_scenario(m: respx.MockRouter, scenario: ScenarioResponses) -> None:
    """Применить ответы сценария ко всем источникам."""
    m.post(EGRUL_POST_URL).mock(
        return_value=httpx.Response(200, json={"t": scenario.egrul_token})
    )
    m.get(f"{EGRUL_BASE_URL}/search-result/{scenario.egrul_token}").mock(
        return_value=httpx.Response(scenario.egrul_status, json={"rows": scenario.egrul_rows})
    )

    m.get(EFRSB_URL).mock(
        return_value=httpx.Response(
            scenario.efrsb_status, json={"pageData": scenario.efrsb_cases}
        )
    )

    if scenario.disqualified_company_inn:
        mock_disqualified_hit(
            m,
            company_inn=scenario.disqualified_company_inn,
            full_name=scenario.disqualified_person,
        )
    else:
        mock_disqualified_clean(m)

    if scenario.fssp_captcha:
        mock_fssp_captcha(m)
    elif scenario.fssp_cases:
        mock_fssp_cases(m, scenario.fssp_cases)
    else:
        mock_fssp_clean(m)

    m.post(KAD_URL).mock(
        return_value=httpx.Response(
            scenario.kad_status,
            json={"Result": {"Items": scenario.kad_items}},
            headers={"content-type": "application/json"},
        )
    )


# --- Именованные архетипы ---


def archetype_clean_legal() -> ScenarioResponses:
    """#1. Действующее юр.лицо, все опрошенные источники молчат."""
    return ScenarioResponses(egrul_rows=[make_egrul_row(name='ПАО "Сбербанк"')])


def archetype_active_individual() -> ScenarioResponses:
    """#2. Действующий ИП."""
    return ScenarioResponses(
        egrul_rows=[
            make_egrul_row(
                inn=INN_INDIVIDUAL_VALID,
                ogrn=OGRNIP_INDIVIDUAL_VALID,
                name="ИП Иванов Иван Иванович",
                kind="ip",
                director_name="Иванов Иван Иванович",
                director_position="Индивидуальный предприниматель",
            )
        ],
    )


def archetype_liquidated() -> ScenarioResponses:
    """#3. Ликвидирована — сделка невозможна."""
    return ScenarioResponses(
        egrul_rows=[make_egrul_row(name='ООО "Прошлое"', liquidation_date="2020-01-15")]
    )


def archetype_bankruptcy_active() -> ScenarioResponses:
    """#4. Активное конкурсное производство."""
    return ScenarioResponses(
        egrul_rows=[make_egrul_row(name='ООО "Банкрот"')],
        efrsb_cases=[
            {
                "caseNumber": "А40-123456/2024",
                "stageName": "Конкурсное производство",
                "isActive": True,
            }
        ],
    )


def archetype_disqualified_director() -> ScenarioResponses:
    """#5. В управлении — дисквалифицированное лицо (живой реестр ФНС)."""
    return ScenarioResponses(
        egrul_rows=[
            make_egrul_row(
                name='ООО "С Дисквалиф. Директором"',
                director_name=DIRECTOR_NAME_DISQUALIFIED,
            )
        ],
        disqualified_company_inn=INN_LEGAL_SBER,
    )


def archetype_mass_address() -> ScenarioResponses:
    """#6. Массовый юридический адрес."""
    return ScenarioResponses(
        egrul_rows=[make_egrul_row(name='ООО "По массовому адресу"', address=MASS_ADDRESS)]
    )


def archetype_mass_director() -> ScenarioResponses:
    """#7. Массовый руководитель (≥10 юр.лиц в реестре ФНС)."""
    return ScenarioResponses(
        egrul_rows=[
            make_egrul_row(
                name='ООО "С Массовым Руководителем"',
                director_name=DIRECTOR_NAME_MASS,
                director_inn=DIRECTOR_INN_MASS,
            )
        ],
    )


def archetype_tax_debtor() -> ScenarioResponses:
    """#8. Крупная налоговая недоимка (открытые данные ФНС `debtam`).

    Заменяет прежний архетип «не сдаёт отчётность»: источника по отчётности
    у нас нет, а по недоимке — есть.
    """
    return ScenarioResponses(egrul_rows=[make_egrul_row(name='ООО "Должник"')])


def archetype_large_enforcement() -> ScenarioResponses:
    """#9. Крупные открытые исполнительные производства."""
    return ScenarioResponses(
        egrul_rows=[make_egrul_row(name='ООО "С Долгами"')],
        fssp_cases=[
            {"number": "12345/24/77001-ИП", "amount_text": "500 000,00"},
            {"number": "12346/24/77001-ИП", "amount_text": "700 000,00"},
        ],
    )


def archetype_fssp_captcha_degraded() -> ScenarioResponses:
    """#10. Карточка чистая, но ФССП требует код с картинки."""
    return ScenarioResponses(
        egrul_rows=[make_egrul_row(name='ООО "ФССП Не Доступен"')],
        fssp_captcha=True,
    )


# --- наполнение локальных реестров под архетипы ---


async def preload_mass_director(ctx: Any, companies_count: int = 42) -> None:
    await ctx.registries.upsert_mass_directors(
        [
            {
                "director_inn": DIRECTOR_INN_MASS,
                "full_name": DIRECTOR_NAME_MASS,
                "companies_count": companies_count,
            }
        ]
    )
    await seed_fresh_registry(ctx.registries, "mass_directors", rows=2)


async def preload_mass_address(ctx: Any, address: str = MASS_ADDRESS) -> None:
    await ctx.registries.upsert_mass_addresses(
        [{"address": address, "registered_entities_count": 50}]
    )
    await seed_fresh_registry(ctx.registries, "mass_addresses", rows=2)


async def preload_tax_debt(
    ctx: Any,
    inn: str = INN_LEGAL_SBER,
    *,
    total_debt_rub: float = 3_000_000.0,
) -> None:
    """Положить недоимку из `debtam` по контрагенту."""
    await ctx.registries.upsert_tax_debts(
        [
            {
                "inn": inn,
                "org_name": 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ДОЛЖНИК"',
                "total_debt_rub": total_debt_rub,
                "penalty_rub": 200_000.0,
                "fine_rub": 50_000.0,
                "taxes": [
                    {
                        "name": "Налог на добавленную стоимость",
                        "debt": total_debt_rub,
                        "penalty": 200_000.0,
                        "fine": 50_000.0,
                    }
                ],
                "report_date": "2026-07-01",
            }
        ]
    )
    await seed_fresh_registry(ctx.registries, "tax_debts", rows=2)
