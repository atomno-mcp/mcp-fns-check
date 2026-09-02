"""Локальные реестры из открытых данных ФНС.

Что лежит в этом SQLite-файле (по умолчанию рядом с основным кэшем,
суффикс `.registries.sqlite`):

  * `mass_addresses`        — адреса массовой регистрации (набор `masaddress`);
  * `mass_directors`        — массовые руководители и учредители
                              (наборы `massleaders` + `massfounders`);
  * `disqualified_entities` — **юридические лица**, в состав органов
                              управления которых входят дисквалифицированные
                              лица (набор `disqualifiedpersons`). Важно: набор
                              ФНС перечисляет именно организации (наименование,
                              ОГРН, ИНН, КПП, адрес), а не самих людей, поэтому
                              искать в нём нужно по ИНН самой компании;
  * `tax_debts`             — недоимка по налогам, пени и штрафы
                              (набор `debtam`, обновляется ежемесячно);
  * `registry_meta`         — служебные отметки: дата данных ФНС, дата
                              загрузки, адрес файла, число строк.

Свежесть — часть контракта. Пустая или устаревшая таблица не должна
превращаться в «нарушений не найдено»: вызывающий код обязан спросить
`registry_status()` и вернуть «не проверено» с причиной. См.
`C:/Users/Velcat/.cursor/rules/service-response-honesty.mdc`.

Сопоставление адресов. Точное совпадение строк не работает: ФНС пишет
«АДЫГЕЯ РЕСП;;МАЙКОП Г;;ГАГАРИНА УЛ;7», а ЕГРЮЛ — «385000, Респ Адыгея,
г Майкоп, ул Гагарина, д 7». Поэтому адрес раскладывается на значимые слова
(типы объектов «респ», «г», «ул», «д», «оф» отбрасываются), и совпадением
считается ситуация, когда все слова адреса из реестра присутствуют в адресе
из ЕГРЮЛ.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS mass_addresses (
    address_normalized        TEXT PRIMARY KEY,
    address_raw               TEXT NOT NULL,
    fns_inclusion_date        TEXT,
    registered_entities_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS mass_directors (
    director_inn       TEXT PRIMARY KEY,
    full_name          TEXT,
    full_name_lower    TEXT,
    companies_count    INTEGER NOT NULL DEFAULT 0,
    fns_inclusion_date TEXT,
    role               TEXT NOT NULL DEFAULT 'leader'
);
CREATE INDEX IF NOT EXISTS idx_mass_directors_name_lower
    ON mass_directors(full_name_lower);

CREATE TABLE IF NOT EXISTS disqualified_directors (
    person_inn             TEXT,
    full_name_lower        TEXT,
    disqualification_date  TEXT NOT NULL,
    disqualification_until TEXT,
    reason                 TEXT,
    PRIMARY KEY (person_inn, disqualification_date)
);
CREATE INDEX IF NOT EXISTS idx_disqualified_name
    ON disqualified_directors(full_name_lower);

CREATE TABLE IF NOT EXISTS disqualified_entities (
    company_inn  TEXT PRIMARY KEY,
    company_ogrn TEXT,
    company_name TEXT,
    company_kpp  TEXT,
    address_raw  TEXT
);

CREATE TABLE IF NOT EXISTS tax_debts (
    inn            TEXT PRIMARY KEY,
    org_name       TEXT,
    total_debt_rub REAL NOT NULL DEFAULT 0,
    penalty_rub    REAL NOT NULL DEFAULT 0,
    fine_rub       REAL NOT NULL DEFAULT 0,
    taxes_json     TEXT,
    report_date    TEXT
);

CREATE TABLE IF NOT EXISTS registry_meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

#: Через сколько дней данные реестра считаются устаревшими.
#: `debtam` ФНС публикует ежемесячно, остальные наборы — «по мере изменений»
#: (фактически ФНС остановила их обновление в 2021 году, и это обязано быть
#: видно в ответе, а не замалчиваться).
STALE_AFTER_DAYS: dict[str, int] = {
    "mass_addresses": 400,
    "mass_directors": 400,
    "disqualified_entities": 400,
    "tax_debts": 100,
}

#: Соответствие «имя реестра → таблица».
REGISTRY_TABLES: dict[str, str] = {
    "mass_addresses": "mass_addresses",
    "mass_directors": "mass_directors",
    "disqualified_entities": "disqualified_entities",
    "disqualified_directors": "disqualified_directors",
    "tax_debts": "tax_debts",
}


@dataclass(slots=True)
class MassAddressHit:
    address_normalized: str
    address_raw: str
    fns_inclusion_date: str | None
    registered_entities_count: int


@dataclass(slots=True)
class MassDirectorHit:
    director_inn: str
    full_name: str | None
    companies_count: int
    fns_inclusion_date: str | None
    #: `leader` — набор ФНС «массовые руководители», `founder` — «массовые учредители».
    role: str = "leader"

    @property
    def role_title(self) -> str:
        return "учредитель" if self.role == "founder" else "руководитель"

    @property
    def registry_title_ru(self) -> str:
        """Название набора ФНС, в котором нашлось совпадение."""
        return "массовых учредителей" if self.role == "founder" else "массовых руководителей"


@dataclass(slots=True)
class DisqualifiedHit:
    person_inn: str | None
    full_name: str | None
    disqualification_date: str
    disqualification_until: str | None
    reason: str | None


@dataclass(slots=True)
class DisqualifiedEntityHit:
    """Организация из набора ФНС «дисквалифицированные лица»."""

    company_inn: str
    company_ogrn: str | None
    company_name: str | None
    company_kpp: str | None
    address_raw: str | None


@dataclass(slots=True)
class TaxDebtHit:
    """Недоимка по налогам из набора ФНС `debtam`."""

    inn: str
    org_name: str | None
    total_debt_rub: float
    penalty_rub: float
    fine_rub: float
    taxes: list[dict[str, Any]]
    report_date: str | None


@dataclass(slots=True)
class RegistryStatus:
    """Состояние одного реестра: наполнен ли, насколько свежий.

    `is_usable` — реестр можно использовать для вывода «совпадений нет».
    Если `False`, отрицательный результат поиска ничего не доказывает и
    обязан отдаваться как «не проверено».
    """

    name: str
    rows: int
    data_as_of: str | None
    loaded_at: str | None
    source_url: str | None
    age_days: int | None
    stale_after_days: int

    @property
    def is_empty(self) -> bool:
        return self.rows == 0

    @property
    def is_stale(self) -> bool:
        if self.rows == 0:
            return False
        if self.age_days is None:
            return True
        return self.age_days > self.stale_after_days

    @property
    def is_usable(self) -> bool:
        return not self.is_empty and not self.is_stale

    @property
    def data_as_of_ru(self) -> str:
        """Дата данных ФНС в виде ДД.ММ.ГГГГ или «дата неизвестна»."""
        if not self.data_as_of:
            return "дата неизвестна"
        try:
            return date.fromisoformat(self.data_as_of).strftime("%d.%m.%Y")
        except ValueError:
            return self.data_as_of


_ADDR_INDEX_RE = re.compile(r"^\s*\d{6}\s*[, ]?\s*")
_NON_LETTER_DIGIT_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_MULTI_SPACE_RE = re.compile(r"\s+")

#: Обозначения типов адресных объектов и помещений. Выкидываются при разборе
#: адреса на значимые слова: ФНС и ЕГРЮЛ пишут их по-разному и в разном порядке.
_ADDRESS_NOISE_TOKENS: frozenset[str] = frozenset(
    """
    респ республика обл область край ао аобл г гор город р н рн район
    у улус пгт рп дп п пос поселок посёлок с село сл слобода д дер деревня
    х хутор ст станица снт тер территория мкр микрорайон кв квартал
    ул улица пер переулок пр пркт просп проспект пр кт ш шоссе наб набережная
    б бул бульвар аллея проезд туп тупик линия тракт км
    дом влд влад владение зд здание корп корпус к стр строение соор сооружение
    оф офис пом помещ помещение комн комната ком каб кабинет этаж эт литер лит
    """.split()
)

#: Слишком короткий набор слов даёт ложные совпадения (например, один город).
_MIN_ADDRESS_TOKENS = 3


def normalise_address(value: str) -> str:
    """Привести адрес к каноничной форме для матчинга.

    Шаги: lower-case → срезаем ведущий 6-значный индекс → убираем все
    знаки препинания → схлопываем пробелы → strip.
    """
    if not value:
        return ""
    v = value.lower()
    v = _ADDR_INDEX_RE.sub("", v)
    v = _NON_LETTER_DIGIT_RE.sub(" ", v)
    v = _MULTI_SPACE_RE.sub(" ", v).strip()
    return v


def address_tokens(value: str) -> frozenset[str]:
    """Разложить адрес на значимые слова (без типов объектов и индекса).

    «385000, Респ Адыгея, г Майкоп, ул Гагарина, д 7» →
    {'адыгея', 'майкоп', 'гагарина', '7'}
    """
    normalised = normalise_address(value).replace("ё", "е")
    if not normalised:
        return frozenset()
    tokens = {
        tok
        for tok in normalised.split()
        if tok and tok not in _ADDRESS_NOISE_TOKENS and not (len(tok) == 1 and tok.isalpha())
    }
    return frozenset(tokens)


class RegistryStore:
    """Async SQLite-обёртка над тремя реестрами Open Data ФНС."""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = str(db_path)
        self._initialised = False
        # Индекс адресов по значимым словам. Строится лениво, сбрасывается
        # при любой записи в таблицу. Набор ФНС `masaddress` — порядка
        # нескольких тысяч строк, поэтому держать его в памяти дёшево.
        self._address_index: list[tuple[frozenset[str], MassAddressHit]] | None = None

    async def init(self) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(_CREATE_SQL)
            await db.commit()
        self._initialised = True

    async def _ensure_init(self) -> None:
        if not self._initialised:
            await self.init()

    # --- наполненность реестров ---
    #
    # `registries is not None` означает лишь «SQLite-файл подключён» — таблицы
    # в нём могут быть пустыми, если ETL-загрузчик (см. scripts/etl_registries.py)
    # ещё не запускался. lookup_*() в этом случае честно вернёт «не найдено»,
    # что для проверок красных флагов неотличимо от «проверили — чисто». Чтобы
    # не путать «источник пуст» с «совпадений нет», вызывающий код должен сперва
    # спросить has_*() и, если реестр не наполнен, вернуть честную ошибку, а не
    # тихое «риска нет».

    async def has_mass_addresses(self) -> bool:
        """Есть ли хотя бы одна запись в реестре массовых адресов."""
        await self._ensure_init()
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute("SELECT 1 FROM mass_addresses LIMIT 1") as cur:
                return (await cur.fetchone()) is not None

    async def has_mass_directors(self) -> bool:
        """Есть ли хотя бы одна запись в реестре массовых руководителей."""
        await self._ensure_init()
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute("SELECT 1 FROM mass_directors LIMIT 1") as cur:
                return (await cur.fetchone()) is not None

    async def has_disqualified_directors(self) -> bool:
        """Есть ли хотя бы одна запись в реестре дисквалифицированных лиц."""
        await self._ensure_init()
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute("SELECT 1 FROM disqualified_directors LIMIT 1") as cur:
                return (await cur.fetchone()) is not None

    async def count_rows(self, registry: str) -> int:
        """Сколько строк в таблице реестра."""
        table = REGISTRY_TABLES.get(registry)
        if table is None:
            raise ValueError(f"Неизвестный реестр: {registry}")
        await self._ensure_init()
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(f"SELECT COUNT(*) FROM {table}") as cur:  # noqa: S608
                row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def registry_status(self, registry: str, *, today: date | None = None) -> RegistryStatus:
        """Состояние реестра: сколько строк, на какую дату, не устарел ли.

        Вызывающий код обязан спросить это ДО того, как трактовать
        отсутствие совпадения как «нарушений не найдено».
        """
        rows = await self.count_rows(registry)
        data_as_of = await self.get_meta(f"{registry}.data_as_of")
        loaded_at = await self.get_meta(f"{registry}.last_etl")
        source_url = await self.get_meta(f"{registry}.source_url")

        age_days: int | None = None
        if data_as_of:
            try:
                as_of = date.fromisoformat(data_as_of)
            except ValueError:
                as_of = None
            if as_of is not None:
                reference = today or datetime.now(tz=timezone.utc).date()
                age_days = (reference - as_of).days

        return RegistryStatus(
            name=registry,
            rows=rows,
            data_as_of=data_as_of,
            loaded_at=loaded_at,
            source_url=source_url,
            age_days=age_days,
            stale_after_days=STALE_AFTER_DAYS.get(registry, 400),
        )

    async def record_load(
        self,
        registry: str,
        *,
        rows: int,
        data_as_of: str | None,
        source_url: str | None,
    ) -> None:
        """Зафиксировать факт загрузки реестра: дата данных ФНС, адрес, объём."""
        await self.set_meta(
            f"{registry}.last_etl",
            datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        )
        await self.set_meta(f"{registry}.rows", str(rows))
        if data_as_of:
            await self.set_meta(f"{registry}.data_as_of", data_as_of)
        if source_url:
            await self.set_meta(f"{registry}.source_url", source_url)

    # --- meta ---

    async def set_meta(self, key: str, value: str) -> None:
        await self._ensure_init()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT INTO registry_meta(key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, value, datetime.now(tz=timezone.utc).isoformat()),
            )
            await db.commit()

    async def get_meta(self, key: str) -> str | None:
        await self._ensure_init()
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute("SELECT value FROM registry_meta WHERE key = ?", (key,)) as cur:
                row = await cur.fetchone()
                return row[0] if row else None

    # --- mass_addresses ---

    async def upsert_mass_addresses(
        self, items: list[dict[str, Any]]
    ) -> int:
        """Загрузить пачку записей. Каждая: {address, fns_inclusion_date?, registered_entities_count?}."""
        await self._ensure_init()
        rows = []
        for it in items:
            raw = it.get("address") or it.get("address_raw")
            if not raw:
                continue
            rows.append(
                (
                    normalise_address(str(raw)),
                    str(raw),
                    it.get("fns_inclusion_date"),
                    int(it.get("registered_entities_count") or 0),
                )
            )
        if not rows:
            return 0
        async with aiosqlite.connect(self._db_path) as db:
            await db.executemany(
                "INSERT INTO mass_addresses(address_normalized, address_raw, fns_inclusion_date, "
                "registered_entities_count) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(address_normalized) DO UPDATE SET "
                "address_raw = excluded.address_raw, "
                "fns_inclusion_date = excluded.fns_inclusion_date, "
                "registered_entities_count = excluded.registered_entities_count",
                rows,
            )
            await db.commit()
        self._address_index = None
        return len(rows)

    async def _load_address_index(self) -> list[tuple[frozenset[str], MassAddressHit]]:
        if self._address_index is not None:
            return self._address_index
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT address_normalized, address_raw, fns_inclusion_date, "
                "registered_entities_count FROM mass_addresses"
            ) as cur:
                rows = await cur.fetchall()
        index: list[tuple[frozenset[str], MassAddressHit]] = []
        for row in rows:
            hit = MassAddressHit(
                address_normalized=row[0],
                address_raw=row[1],
                fns_inclusion_date=row[2],
                registered_entities_count=int(row[3]),
            )
            tokens = address_tokens(row[1])
            if len(tokens) >= _MIN_ADDRESS_TOKENS:
                index.append((tokens, hit))
        self._address_index = index
        return index

    async def lookup_mass_address(self, address: str) -> MassAddressHit | None:
        """Найти адрес в реестре массовой регистрации.

        Сначала пробуем точное совпадение нормализованной строки, затем —
        совпадение по значимым словам: все слова адреса из реестра должны
        встретиться в адресе из ЕГРЮЛ. Второе нужно, потому что ФНС и ЕГРЮЛ
        пишут один и тот же адрес по-разному.
        """
        await self._ensure_init()
        norm = normalise_address(address)
        if not norm:
            return None
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT address_normalized, address_raw, fns_inclusion_date, registered_entities_count "
                "FROM mass_addresses WHERE address_normalized = ?",
                (norm,),
            ) as cur:
                row = await cur.fetchone()
        if row:
            return MassAddressHit(
                address_normalized=row[0],
                address_raw=row[1],
                fns_inclusion_date=row[2],
                registered_entities_count=int(row[3]),
            )

        query_tokens = address_tokens(address)
        if len(query_tokens) < _MIN_ADDRESS_TOKENS:
            return None
        best: MassAddressHit | None = None
        best_size = 0
        for tokens, hit in await self._load_address_index():
            if tokens <= query_tokens and len(tokens) > best_size:
                best, best_size = hit, len(tokens)
        return best

    # --- mass_directors ---

    async def upsert_mass_directors(self, items: list[dict[str, Any]]) -> int:
        await self._ensure_init()
        rows = []
        for it in items:
            inn = it.get("director_inn") or it.get("inn")
            if not inn:
                continue
            full_name = it.get("full_name")
            rows.append(
                (
                    str(inn),
                    full_name,
                    full_name.lower() if isinstance(full_name, str) else None,
                    int(it.get("companies_count") or 0),
                    it.get("fns_inclusion_date"),
                    "founder" if it.get("role") == "founder" else "leader",
                )
            )
        if not rows:
            return 0
        async with aiosqlite.connect(self._db_path) as db:
            await db.executemany(
                "INSERT INTO mass_directors("
                "director_inn, full_name, full_name_lower, companies_count, "
                "fns_inclusion_date, role"
                ") VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(director_inn) DO UPDATE SET "
                "full_name = excluded.full_name, "
                "full_name_lower = excluded.full_name_lower, "
                "companies_count = excluded.companies_count, "
                "fns_inclusion_date = excluded.fns_inclusion_date, "
                "role = excluded.role",
                rows,
            )
            await db.commit()
        return len(rows)

    async def lookup_mass_director(
        self, inn: str | None = None, *, full_name: str | None = None
    ) -> MassDirectorHit | None:
        """Lookup массового руководителя по ИНН (точный) или по ФИО (case-insensitive).

        Кириллица в SQLite-стандартном LOWER() не понижается, поэтому используем
        отдельную колонку `full_name_lower`, заполняемую на стороне Python в upsert.
        """
        await self._ensure_init()
        if not inn and not full_name:
            return None
        sql = (
            "SELECT director_inn, full_name, companies_count, fns_inclusion_date, role "
            "FROM mass_directors WHERE "
        )
        clauses: list[str] = []
        params: list[str] = []
        if inn:
            clauses.append("director_inn = ?")
            params.append(inn)
        if full_name:
            clauses.append("full_name_lower = ?")
            params.append(full_name.lower())
        sql += " OR ".join(clauses) + " LIMIT 1"

        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(sql, params) as cur:
                row = await cur.fetchone()
        if not row:
            return None
        return MassDirectorHit(
            director_inn=row[0],
            full_name=row[1],
            companies_count=int(row[2]),
            fns_inclusion_date=row[3],
            role=row[4] or "leader",
        )

    # --- disqualified ---

    async def upsert_disqualified(self, items: list[dict[str, Any]]) -> int:
        await self._ensure_init()
        rows = []
        for it in items:
            disq_date = it.get("disqualification_date")
            if not disq_date:
                continue
            full_name = it.get("full_name")
            rows.append(
                (
                    it.get("person_inn"),
                    full_name.lower() if isinstance(full_name, str) else None,
                    str(disq_date),
                    it.get("disqualification_until"),
                    it.get("reason"),
                )
            )
        if not rows:
            return 0
        async with aiosqlite.connect(self._db_path) as db:
            await db.executemany(
                "INSERT INTO disqualified_directors("
                "person_inn, full_name_lower, disqualification_date, disqualification_until, reason"
                ") VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(person_inn, disqualification_date) DO UPDATE SET "
                "full_name_lower = excluded.full_name_lower, "
                "disqualification_until = excluded.disqualification_until, "
                "reason = excluded.reason",
                rows,
            )
            await db.commit()
        return len(rows)

    async def lookup_disqualified(
        self, *, inn: str | None = None, full_name: str | None = None
    ) -> list[DisqualifiedHit]:
        await self._ensure_init()
        if not inn and not full_name:
            return []
        sql = (
            "SELECT person_inn, full_name_lower, disqualification_date, "
            "disqualification_until, reason FROM disqualified_directors WHERE "
        )
        clauses = []
        params: list[str] = []
        if inn:
            clauses.append("person_inn = ?")
            params.append(inn)
        if full_name:
            clauses.append("full_name_lower = ?")
            params.append(full_name.lower())
        sql += " OR ".join(clauses)

        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()

        return [
            DisqualifiedHit(
                person_inn=row[0],
                full_name=row[1],
                disqualification_date=row[2],
                disqualification_until=row[3],
                reason=row[4],
            )
            for row in rows
        ]

    # --- disqualified_entities (набор ФНС `disqualifiedpersons`) ---

    async def upsert_disqualified_entities(self, items: list[dict[str, Any]]) -> int:
        """Загрузить организации, в чьих органах управления есть дисквалифицированные.

        Каждая запись: {company_inn, company_ogrn?, company_name?, company_kpp?, address_raw?}.
        """
        await self._ensure_init()
        rows = []
        for it in items:
            inn = it.get("company_inn") or it.get("inn")
            if not inn:
                continue
            rows.append(
                (
                    str(inn).strip(),
                    it.get("company_ogrn"),
                    it.get("company_name"),
                    it.get("company_kpp"),
                    it.get("address_raw"),
                )
            )
        if not rows:
            return 0
        async with aiosqlite.connect(self._db_path) as db:
            await db.executemany(
                "INSERT INTO disqualified_entities("
                "company_inn, company_ogrn, company_name, company_kpp, address_raw"
                ") VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(company_inn) DO UPDATE SET "
                "company_ogrn = excluded.company_ogrn, "
                "company_name = excluded.company_name, "
                "company_kpp = excluded.company_kpp, "
                "address_raw = excluded.address_raw",
                rows,
            )
            await db.commit()
        return len(rows)

    async def lookup_disqualified_entity(self, company_inn: str) -> DisqualifiedEntityHit | None:
        await self._ensure_init()
        if not company_inn:
            return None
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT company_inn, company_ogrn, company_name, company_kpp, address_raw "
                "FROM disqualified_entities WHERE company_inn = ?",
                (company_inn.strip(),),
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return None
        return DisqualifiedEntityHit(
            company_inn=row[0],
            company_ogrn=row[1],
            company_name=row[2],
            company_kpp=row[3],
            address_raw=row[4],
        )

    # --- tax_debts (набор ФНС `debtam`) ---

    async def upsert_tax_debts(self, items: list[dict[str, Any]]) -> int:
        """Загрузить пачку записей о недоимке.

        Каждая: {inn, org_name?, total_debt_rub, penalty_rub, fine_rub,
        taxes: [{name, debt, penalty, fine}], report_date?}.
        """
        await self._ensure_init()
        rows = []
        for it in items:
            inn = it.get("inn")
            if not inn:
                continue
            taxes = it.get("taxes")
            rows.append(
                (
                    str(inn).strip(),
                    it.get("org_name"),
                    float(it.get("total_debt_rub") or 0.0),
                    float(it.get("penalty_rub") or 0.0),
                    float(it.get("fine_rub") or 0.0),
                    json.dumps(taxes, ensure_ascii=False) if taxes else None,
                    it.get("report_date"),
                )
            )
        if not rows:
            return 0
        async with aiosqlite.connect(self._db_path) as db:
            await db.executemany(
                "INSERT INTO tax_debts("
                "inn, org_name, total_debt_rub, penalty_rub, fine_rub, taxes_json, report_date"
                ") VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(inn) DO UPDATE SET "
                "org_name = excluded.org_name, "
                "total_debt_rub = excluded.total_debt_rub, "
                "penalty_rub = excluded.penalty_rub, "
                "fine_rub = excluded.fine_rub, "
                "taxes_json = excluded.taxes_json, "
                "report_date = excluded.report_date",
                rows,
            )
            await db.commit()
        return len(rows)

    async def lookup_tax_debt(self, inn: str) -> TaxDebtHit | None:
        await self._ensure_init()
        if not inn:
            return None
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT inn, org_name, total_debt_rub, penalty_rub, fine_rub, taxes_json, report_date "
                "FROM tax_debts WHERE inn = ?",
                (inn.strip(),),
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return None
        taxes: list[dict[str, Any]] = []
        if row[5]:
            try:
                parsed = json.loads(row[5])
                if isinstance(parsed, list):
                    taxes = parsed
            except ValueError:
                taxes = []
        return TaxDebtHit(
            inn=row[0],
            org_name=row[1],
            total_debt_rub=float(row[2]),
            penalty_rub=float(row[3]),
            fine_rub=float(row[4]),
            taxes=taxes,
            report_date=row[6],
        )

    # --- bulk seeding from JSON ---

    async def load_seed(self, seed_path: Path | str) -> dict[str, int]:
        """Загрузить bundled-сидинг (data/registries_seed.json) в реестры.

        Возвращает количество загруженных записей по каждому реестру.
        """
        path = Path(seed_path)
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        ma = await self.upsert_mass_addresses(data.get("mass_addresses", []))
        md = await self.upsert_mass_directors(data.get("mass_directors", []))
        dq = await self.upsert_disqualified(data.get("disqualified_directors", []))
        await self.set_meta("seed_version", str(data.get("_meta", {}).get("version", "1")))
        return {"mass_addresses": ma, "mass_directors": md, "disqualified_directors": dq}
