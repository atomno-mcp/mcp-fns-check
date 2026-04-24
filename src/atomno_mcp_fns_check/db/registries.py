"""Локальные реестры из Open Data ФНС: массовые адреса, массовые руководители,
дисквалифицированные лица.

На S3 это отдельный SQLite-файл (по умолчанию рядом с основным кэшем,
суффикс `.registries.sqlite`). В реальном продакшене эти таблицы будет
обновлять отдельный ETL-job (см. SPEC §4.4 и стадия R-4 / S4).

Lookup делается на нормализованных значениях:
  - ИНН — как есть, 10 или 12 цифр.
  - Адрес — нормализуется: lower-case, схлопнутые пробелы, удалённые точки и
    запятые, удалённые ведущие 6 цифр индекса. Это компенсирует разнобой
    написания адресов в ЕГРЮЛ и Open Data.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
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
    fns_inclusion_date TEXT
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

CREATE TABLE IF NOT EXISTS registry_meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


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


@dataclass(slots=True)
class DisqualifiedHit:
    person_inn: str | None
    full_name: str | None
    disqualification_date: str
    disqualification_until: str | None
    reason: str | None


_ADDR_INDEX_RE = re.compile(r"^\s*\d{6}\s*[, ]?\s*")
_NON_LETTER_DIGIT_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_MULTI_SPACE_RE = re.compile(r"\s+")


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


class RegistryStore:
    """Async SQLite-обёртка над тремя реестрами Open Data ФНС."""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = str(db_path)
        self._initialised = False

    async def init(self) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(_CREATE_SQL)
            await db.commit()
        self._initialised = True

    async def _ensure_init(self) -> None:
        if not self._initialised:
            await self.init()

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
        return len(rows)

    async def lookup_mass_address(self, address: str) -> MassAddressHit | None:
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
        if not row:
            return None
        return MassAddressHit(
            address_normalized=row[0],
            address_raw=row[1],
            fns_inclusion_date=row[2],
            registered_entities_count=int(row[3]),
        )

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
                )
            )
        if not rows:
            return 0
        async with aiosqlite.connect(self._db_path) as db:
            await db.executemany(
                "INSERT INTO mass_directors("
                "director_inn, full_name, full_name_lower, companies_count, fns_inclusion_date"
                ") VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(director_inn) DO UPDATE SET "
                "full_name = excluded.full_name, "
                "full_name_lower = excluded.full_name_lower, "
                "companies_count = excluded.companies_count, "
                "fns_inclusion_date = excluded.fns_inclusion_date",
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
            "SELECT director_inn, full_name, companies_count, fns_inclusion_date "
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
