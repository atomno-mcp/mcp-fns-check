"""Фабрики респонсов для smoke-архетипов (R-6).

Выносим однообразные respx-моки всех 5 внешних источников в одно место, чтобы
`test_smoke_archetypes.py` читался как таблица «архетип ↔ ожидаемый вердикт»,
а не как трёхсотстрочная портянка with-блоков.

Каждый архетип конструирует `ScenarioResponses` — именованный набор payload'ов
(egrul_rows, efrsb_cases, pb_tags, fssp_items, kad_items). Хелпер `mock_scenario`
применяет эти payload'ы к респ-роутеру, а fallback'ы (сбой конкретного источника)
подставляются через точечные override'ы в тесте.

Никаких «магических» умолчаний для контрольных цифр — все ИНН/ОГРН в фикстурах
валидны по алгоритму ФНС и зафиксированы в модуле как константы. Если ФНС
когда-то поменяет алгоритм — сломаются не архетипы, а `validators.py`, и мы
это увидим в test_validators.py, а не в smoke-тестах.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
import respx

from atomno_mcp_fns_check.sources.efrsb import EFRSB_BASE_URL, EFRSB_SEARCH_PATH
from atomno_mcp_fns_check.sources.egrul import EGRUL_BASE_URL
from atomno_mcp_fns_check.sources.fssp import FSSP_BASE_URL, FSSP_SEARCH_PATH
from atomno_mcp_fns_check.sources.kad import KAD_BASE_URL, KAD_SEARCH_PATH
from atomno_mcp_fns_check.sources.pb_fns import PB_BASE_URL, PB_SEARCH_PATH

# --- Константы URL'ов (повторяем, чтобы не тянуть из test_contractor.py) ---

EGRUL_POST_URL = f"{EGRUL_BASE_URL}/"
EFRSB_URL = f"{EFRSB_BASE_URL}{EFRSB_SEARCH_PATH}"
PB_URL = f"{PB_BASE_URL}{PB_SEARCH_PATH}"
FSSP_URL = f"{FSSP_BASE_URL}{FSSP_SEARCH_PATH}"
KAD_URL = f"{KAD_BASE_URL}{KAD_SEARCH_PATH}"


# --- Валидные архетипные идентификаторы (контрольные цифры сходятся) ---

# Сбербанк — реальный публичный ИНН (10 цифр).
INN_LEGAL_SBER = "7707083893"
OGRN_LEGAL_SBER = "1027700132195"

# Синтетический ИП — 12-цифровой ИНН с корректной контрольной цифрой
# (подобран руками, алгоритм ФНС: два последовательных веса).
INN_INDIVIDUAL_VALID = "500100732259"
OGRNIP_INDIVIDUAL_VALID = "321774600143014"

# ИНН руководителя для сценария дисквалификации — используется как ключ
# в реестре дисквалифицированных ФНС (не валидируется как реальный ИНН —
# это идентификатор записи реестра).
DIRECTOR_INN_DISQUALIFIED = "504700000056"
DIRECTOR_NAME_DISQUALIFIED = "СИДОРОВ С.С."

# ИНН + ФИО руководителя для сценария массового руководителя.
DIRECTOR_INN_MASS = "770700000042"
DIRECTOR_NAME_MASS = "ИВАНОВ И.И."

# Массовый юр.адрес.
MASS_ADDRESS = "127015, г Москва, ул Бумажная, д 1"


# --- Базовый конструктор egrul-строки ---


def make_egrul_row(
    *,
    inn: str = INN_LEGAL_SBER,
    ogrn: str = OGRN_LEGAL_SBER,
    name: str = "ООО Тест",
    kind: str = "ul",  # "ul" — юрлицо, "ip" — ИП
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


# --- Сценарии ---


@dataclass(slots=True)
class ScenarioResponses:
    """Payload'ы для 5 источников + token ЕГРЮЛ (у ЕГРЮЛ двухэтапный протокол)."""

    egrul_token: str = "tk-smoke"
    egrul_rows: list[dict[str, Any]] = field(default_factory=list)
    efrsb_cases: list[dict[str, Any]] = field(default_factory=list)
    pb_tags: list[Any] = field(default_factory=list)
    pb_raw_payload: dict[str, Any] | None = None
    fssp_items: list[dict[str, Any]] = field(default_factory=list)
    fssp_captcha: bool = False
    kad_items: list[dict[str, Any]] = field(default_factory=list)

    # Точечные http-override'ы (если нужно смоделировать 5xx/timeout/antibot):
    egrul_status: int = 200
    efrsb_status: int = 200
    pb_status: int = 200
    fssp_status: int = 200
    kad_status: int = 200


def mock_scenario(m: respx.MockRouter, scenario: ScenarioResponses) -> None:
    """Применить payload'ы сценария ко всем 5 источникам."""
    # --- ЕГРЮЛ (POST токен → GET результат) ---
    m.post(EGRUL_POST_URL).mock(
        return_value=httpx.Response(200, json={"t": scenario.egrul_token})
    )
    m.get(f"{EGRUL_BASE_URL}/search-result/{scenario.egrul_token}").mock(
        return_value=httpx.Response(
            scenario.egrul_status,
            json={"rows": scenario.egrul_rows},
        )
    )

    # --- ЕФРСБ ---
    m.get(EFRSB_URL).mock(
        return_value=httpx.Response(
            scenario.efrsb_status,
            json={"pageData": scenario.efrsb_cases},
        )
    )

    # --- Прозрачный бизнес ФНС ---
    if scenario.pb_raw_payload is not None:
        pb_payload = scenario.pb_raw_payload
    else:
        pb_payload = {"ul": {"data": [{"tags": scenario.pb_tags}]}}
    m.get(PB_URL).mock(
        return_value=httpx.Response(scenario.pb_status, json=pb_payload)
    )

    # --- ФССП (CAPTCHA-флаг имеет приоритет над items) ---
    if scenario.fssp_captcha:
        fssp_payload: dict[str, Any] = {"captcha": True}
    else:
        fssp_payload = {"response": {"result": scenario.fssp_items}}
    m.get(FSSP_URL).mock(
        return_value=httpx.Response(scenario.fssp_status, json=fssp_payload)
    )

    # --- КАД (POST) ---
    m.post(KAD_URL).mock(
        return_value=httpx.Response(
            scenario.kad_status,
            json={"Result": {"Items": scenario.kad_items}},
            headers={"content-type": "application/json"},
        )
    )


# --- Именованные архетипы (ровно 10 штук) ---


def archetype_clean_legal() -> ScenarioResponses:
    """#1. Действующая юр.лицо без нарушений — safe_to_proceed."""
    return ScenarioResponses(
        egrul_rows=[make_egrul_row(name='ПАО "Сбербанк"')],
    )


def archetype_active_individual() -> ScenarioResponses:
    """#2. Действующий ИП (12-цифровой ИНН, ОГРНИП 15) — safe_to_proceed."""
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
    """#3. Ликвидирована — impossible_contractor_defunct."""
    return ScenarioResponses(
        egrul_rows=[
            make_egrul_row(
                name='ООО "Прошлое"',
                liquidation_date="2020-01-15",
            )
        ],
    )


def archetype_bankruptcy_active() -> ScenarioResponses:
    """#4. Активное банкротство (конкурсное производство) — high_risk_do_not_proceed."""
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
    """#5. Руководитель дисквалифицирован ФНС — high_risk_do_not_proceed."""
    return ScenarioResponses(
        egrul_rows=[
            make_egrul_row(
                name='ООО "С Дисквалиф. Директором"',
                director_name=DIRECTOR_NAME_DISQUALIFIED,
                director_inn=DIRECTOR_INN_DISQUALIFIED,
            )
        ],
    )


def archetype_mass_address() -> ScenarioResponses:
    """#6. Массовый юр.адрес — manual_review_required (medium risk)."""
    return ScenarioResponses(
        egrul_rows=[
            make_egrul_row(
                name='ООО "По массовому адресу"',
                address=MASS_ADDRESS,
            )
        ],
    )


def archetype_mass_director() -> ScenarioResponses:
    """#7. Массовый руководитель (≥10 юр.лиц в реестре ФНС) — high risk."""
    return ScenarioResponses(
        egrul_rows=[
            make_egrul_row(
                name='ООО "С Массовым Руководителем"',
                director_name=DIRECTOR_NAME_MASS,
                director_inn=DIRECTOR_INN_MASS,
            )
        ],
    )


def archetype_no_reporting() -> ScenarioResponses:
    """#8. Прозрачный бизнес ФНС: нет отчётности >1 года — manual_review (medium)."""
    return ScenarioResponses(
        egrul_rows=[make_egrul_row(name='ООО "Техническая"')],
        pb_raw_payload={
            "ul": {
                "data": [
                    {
                        "tags": [
                            "Сведения о непредставлении налоговой отчётности более года."
                        ]
                    }
                ]
            }
        },
    )


def archetype_large_enforcement() -> ScenarioResponses:
    """#9. Крупные открытые исполнительные производства (∑ > 1M ₽) — manual_review (medium)."""
    return ScenarioResponses(
        egrul_rows=[make_egrul_row(name='ООО "С Долгами"')],
        fssp_items=[
            {"ip_number": "12345/24/77001-ИП", "subjAmount": "500000.00", "status": "Возбуждено"},
            {"ip_number": "12346/24/77001-ИП", "subjAmount": "700000.00", "status": "Возбуждено"},
        ],
    )


def archetype_fssp_captcha_degraded() -> ScenarioResponses:
    """#10. Чистая карточка, но ФССП требует CAPTCHA — manual_review_required (graceful)."""
    return ScenarioResponses(
        egrul_rows=[make_egrul_row(name='ООО "ФССП Не Доступен"')],
        fssp_captcha=True,
    )


# --- Registry-сетап для архетипов 5 и 7 ---


async def preload_disqualified(ctx: Any) -> None:
    """Положить запись в локальный реестр дисквалифицированных ФНС."""
    await ctx.registries.upsert_disqualified(
        [
            {
                "person_inn": DIRECTOR_INN_DISQUALIFIED,
                "full_name": DIRECTOR_NAME_DISQUALIFIED,
                "disqualification_date": "2024-02-10",
                "disqualification_until": "2099-02-10",
            }
        ]
    )


async def preload_mass_director(ctx: Any, companies_count: int = 42) -> None:
    """Положить руководителя в реестр массовых ФНС (companies_count ≥ 10)."""
    await ctx.registries.upsert_mass_directors(
        [
            {
                "director_inn": DIRECTOR_INN_MASS,
                "full_name": DIRECTOR_NAME_MASS,
                "companies_count": companies_count,
            }
        ]
    )


async def preload_mass_address(ctx: Any, address: str = MASS_ADDRESS) -> None:
    """Положить юр.адрес в реестр массовых ФНС."""
    await ctx.registries.upsert_mass_addresses(
        [{"address": address, "registered_entities_count": 50}]
    )
