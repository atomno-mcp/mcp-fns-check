"""Pydantic v2 модели карточки контрагента и сопутствующих структур.

Структура `CounterpartyCard` — соответствует возврату тулза `check_inn`
(см. SPEC §3.1). На S1 задача — зафиксировать публичный контракт; нормализация
из конкретных JSON-ответов ФНС появится на S2 (`tools.py` + `sources/egrul.py`).
"""

from __future__ import annotations

from datetime import date as DateT
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SubjectType = Literal["legal_entity", "individual"]
LegalStatus = Literal[
    "active",
    "liquidating",
    "bankruptcy",
    "liquidated",
    "reorganizing",
    "excluded_inactive",
]
TaxRegime = Literal["general", "usn_income", "usn_income_minus_expenses", "esxn", "psn", "npd", "unknown"]


class StrictModel(BaseModel):
    model_config = ConfigDict(
        str_strip_whitespace=True,
        populate_by_name=True,
        extra="ignore",
    )


class CounterpartyName(StrictModel):
    full: str = Field(description="Полное наименование")
    short: str | None = Field(default=None, description="Сокращённое наименование, если есть")


class RegistrationInfo(StrictModel):
    date: DateT | None = Field(default=None, description="Дата создания записи в ЕГРЮЛ/ЕГРИП")
    registering_authority: str | None = None
    okato: str | None = Field(default=None, description="Код ОКАТО (Общероссийский классификатор объектов административно-территориального деления)")
    oktmo: str | None = Field(default=None, description="Код ОКТМО (Общероссийский классификатор территорий муниципальных образований)")


class AddressInfo(StrictModel):
    full: str
    region_code: str | None = None
    is_mass_address: bool = False
    address_status: Literal["valid", "invalid", "unknown"] = "unknown"


class DirectorInfo(StrictModel):
    full_name: str | None = None
    position: str | None = None
    inn_masked: str | None = Field(
        default=None,
        description="ИНН руководителя в маске XXX*****YY (см. SPEC §3.1 шаг 6)",
    )
    appointed_date: DateT | None = None
    is_disqualified: bool = False
    is_mass_director: bool = False


class FounderInfo(StrictModel):
    type: SubjectType | Literal["state", "foreign", "other"]
    name: str
    inn: str | None = None
    share_percent: float | None = Field(default=None, ge=0.0, le=100.0)


class OkvedInfo(StrictModel):
    code: str
    name: str | None = None
    section: str | None = Field(default=None, description="Раздел верхнего уровня ОКВЭД-2 (одна латинская буква)")
    section_name: str | None = None
    is_licensable: bool = False
    license_authority: str | None = None


class ExtendedInfo(StrictModel):
    average_employees: int | None = None
    average_employees_year: int | None = None
    tax_load_percent: float | None = None
    tax_load_year: int | None = None
    special_regimes: list[str] = Field(default_factory=list)


class DataSourceMeta(StrictModel):
    egrul_extract_date: DateT | None = None
    pb_extract_date: DateT | None = None
    cache_age_hours: float | None = None
    fetched_at: datetime | None = None


class CounterpartyCard(StrictModel):
    """Карточка контрагента — единый ответ для check_inn / check_ogrn."""

    inn: str
    ogrn: str | None = None
    kpp: str | None = Field(default=None, description="Код причины постановки на учёт (только для юр.лица)")
    type: SubjectType
    status: LegalStatus
    name: CounterpartyName
    registration: RegistrationInfo = Field(default_factory=RegistrationInfo)
    address: AddressInfo | None = None
    director: DirectorInfo | None = None
    founders: list[FounderInfo] = Field(default_factory=list)
    okved_main: OkvedInfo | None = None
    additional_okveds: list[OkvedInfo] = Field(default_factory=list)
    tax_regime: TaxRegime = "unknown"
    authorized_capital_rub: float | None = None
    msp_status: str | None = Field(
        default=None,
        description="Статус в реестре МСП (Малое и Среднее Предпринимательство), если применимо",
    )
    extended: ExtendedInfo | None = None
    data_source_meta: DataSourceMeta = Field(default_factory=DataSourceMeta)


class LegalStatusReport(StrictModel):
    """Ответ для get_legal_status."""

    inn: str | None = None
    ogrn: str | None = None
    status: LegalStatus
    status_label_ru: str
    status_changed_at: DateT | None = None
    bankruptcy_stage: str | None = None
    liquidation_phase: str | None = None
    exclusion_reason: str | None = None
    sources_checked: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    checked_at: datetime


class OkvedReport(StrictModel):
    """Ответ для get_okveds."""

    inn: str | None = None
    ogrn: str | None = None
    main_okved: OkvedInfo | None = None
    additional_okveds: list[OkvedInfo] = Field(default_factory=list)
    total_count: int = 0
    history: list[dict] | None = None
    warnings: list[str] = Field(default_factory=list)


# --- Red flags ---

RiskLevel = Literal["low", "medium", "high"]

#: Итоговый уровень риска. Отдельно от `RiskLevel`, потому что кроме трёх
#: степеней риска бывает четвёртое состояние — «не определён»: часть проверок
#: не выполнилась, и «низкий риск» в этом случае был бы враньём.
#: См. `C:/Users/Velcat/.cursor/rules/service-response-honesty.mdc`.
OverallRiskLevel = Literal["low", "medium", "high", "unknown"]

#: Коды проверок риска. Их семь. Восьмая — непредставление налоговой
#: отчётности — убрана 02.08.2026: публичного источника этих сведений нет,
#: проверка всегда молчала и делала вердикт «можно работать» недостижимым.
FlagCode = Literal[
    "mass_address",
    "mass_director",
    "disqualified_director",
    "tax_debts",
    "bankruptcy_records",
    "enforcement_proceedings",
    "active_lawsuits",
]


class FlagDetail(StrictModel):
    """Один обнаруженный риск-флаг."""

    code: FlagCode
    level: RiskLevel
    message_ru: str
    details: dict = Field(default_factory=dict)


class CheckError(StrictModel):
    """Информация об упавшей проверке."""

    code: FlagCode
    error: str
    message_ru: str


class CheckCoverage(StrictModel):
    """Сколько проверок реально отработало.

    Без этой секции отчёт нельзя прочитать честно: `flags: []` одинаково
    выглядит и когда всё проверено и чисто, и когда ни один источник не ответил.
    """

    #: Сколько проверок запускалось.
    total: int = 0
    #: Сколько дали достоверный ответ (нашли флаг либо подтвердили отсутствие).
    answered: int = 0
    #: Сколько нашли риск.
    flagged: int = 0
    #: Сколько подтвердили отсутствие риска.
    clean: int = 0
    #: Сколько пропущено (нет входных данных, источник не подключён).
    skipped: int = 0
    #: Сколько не выполнилось (источник не ответил, реестр пуст/устарел).
    failed: int = 0

    @property
    def is_complete(self) -> bool:
        return self.total > 0 and self.answered == self.total


class RedFlagsReport(StrictModel):
    """Ответ для check_for_red_flags."""

    inn: str
    overall_risk_level: OverallRiskLevel
    overall_risk_score: int = Field(ge=0, le=100)
    summary_ru: str
    flags: list[FlagDetail] = Field(default_factory=list)
    checks_passed: list[FlagCode] = Field(default_factory=list)
    checks_skipped: list[FlagCode] = Field(default_factory=list)
    errors: list[CheckError] = Field(default_factory=list)
    coverage: CheckCoverage = Field(default_factory=CheckCoverage)
    checked_at: datetime


# --- Directors history ---


class DirectorChange(StrictModel):
    """Один период «правления» директора."""

    from_date: DateT | None = Field(default=None, alias="from")
    to_date: DateT | None = Field(default=None, alias="to")
    full_name: str | None = None
    position: str | None = None
    is_current: bool = False


class FounderChange(StrictModel):
    """Один период владения учредителя."""

    from_date: DateT | None = Field(default=None, alias="from")
    to_date: DateT | None = Field(default=None, alias="to")
    type: str
    name: str
    inn: str | None = None
    share_percent: float | None = None
    is_current: bool = False


class DirectorsHistoryReport(StrictModel):
    """Ответ для get_directors_history."""

    inn: str
    directors_history: list[DirectorChange] = Field(default_factory=list)
    founders_history: list[FounderChange] = Field(default_factory=list)
    total_director_changes: int = 0
    total_founder_changes: int = 0
    data_completeness_warning: str | None = None
    sources_used: list[str] = Field(default_factory=list)


# --- Contractor aggregator (check_contractor, SPEC v0.1 §4.1, §6.2) ---

IdentifierType = Literal["inn", "ogrn"]
ContractorTier = Literal["open", "free", "pro"]
VerdictAction = Literal[
    "safe_to_proceed",
    "manual_review_required",
    "high_risk_do_not_proceed",
    "impossible_contractor_defunct",
]


class ContractorSources(StrictModel):
    """Метаданные источников, на которых собран ответ `check_contractor`."""

    egrul_extract_date: DateT | None = None
    efrsb_check_date: DateT | None = None
    pb_fns_check_date: DateT | None = None
    fssp_check_date: DateT | None = None
    kad_check_date: DateT | None = None
    registries_check_date: DateT | None = None
    cache_age_hours: float | None = None
    sources_queried: list[str] = Field(default_factory=list)


IndividualMatchConfidence = Literal["exact", "possible", "unknown"]
IndividualBankruptcySource = Literal["efrsb_json", "efrsb_html"]


class IndividualBankruptcyRecord(StrictModel):
    """Одно дело о банкротстве физического лица."""

    case_number: str | None = None
    stage_ru: str | None = None
    stage: str | None = None
    is_active: bool = True
    debtor_fio: str | None = None
    birth_date: DateT | None = None
    inn: str | None = None
    snils: str | None = None
    region: str | None = None
    match_confidence: IndividualMatchConfidence = "unknown"


class IndividualBankruptcyReport(StrictModel):
    """Ответ для check_individual_bankruptcy."""

    query_fio: str
    query_birth_date: DateT
    query_snils: str | None = None
    query_hash: str = Field(
        description="sha256(fio_lower|birth_date|snils) — для audit без plain-text ПДн",
    )
    has_active_bankruptcy: bool
    active_cases: list[IndividualBankruptcyRecord] = Field(default_factory=list)
    total_matches: int = 0
    source: IndividualBankruptcySource = "efrsb_json"
    warnings: list[str] = Field(default_factory=list)
    disclaimer_ru: str = (
        "Сведения получены из публичного реестра ЕФРСБ (bankrot.fedresurs.ru). "
        "Это информационная справка, не юридическое заключение о статусе банкрота."
    )
    checked_at: datetime
    source_registry_ru: str = (
        "Единый федеральный реестр сведений о банкротстве (ЕФРСБ)"
    )
    source_url: str = "https://bankrot.fedresurs.ru"
    data_as_of: str | None = None
    data_as_of_note_ru: str = "Реестр свою дату обновления не публикует."
    homonym_warning_ru: str | None = None
    notice_ru: str = ""


class ContractorReport(StrictModel):
    """Агрегированный ответ `check_contractor` — главный тул (SPEC v0.1 §6.2).

    Собирает базовую карточку (ЕГРЮЛ), юридический статус (с обогащением из
    ЕФРСБ) и результат риск-чека (до 7 проверок). Поверх складывает
    детерминированный вердикт `verdict.action` и список human-readable
    рекомендаций для AI-агента. Никакие значения не придумываются:
    если источник недоступен — это фиксируется в `risks.errors` и
    `sources.sources_queried`, а не заменяется «разумным умолчанием».
    """

    identifier: str = Field(description="Исходный идентификатор (ИНН или ОГРН) — как пришёл от пользователя")
    identifier_type: IdentifierType
    inn: str
    ogrn: str | None = None
    card: CounterpartyCard
    legal_status: LegalStatusReport
    risks: RedFlagsReport
    verdict_action: VerdictAction
    verdict_reason_ru: str
    recommendations: list[str] = Field(default_factory=list)
    sources: ContractorSources = Field(default_factory=ContractorSources)
    tier: ContractorTier = "open"
    checked_at: datetime


_STATUS_LABELS_RU: dict[str, str] = {
    "active": "Действующее",
    "liquidating": "В процессе ликвидации",
    "bankruptcy": "Банкротство",
    "liquidated": "Ликвидировано",
    "reorganizing": "В стадии реорганизации",
    "excluded_inactive": "Исключено из ЕГРЮЛ как недействующее",
}


def status_label_ru(status: str) -> str:
    """Человекочитаемый русский лейбл для значения LegalStatus."""
    return _STATUS_LABELS_RU.get(status, status)
