"""SQLite-кэш карточек контрагентов с TTL.

Используется на S1 как fallback вместо PostgreSQL (см. SPEC §7.1 День 1-2).
Schema:
    counterparty_cache(
        key TEXT,            -- 'inn:7707083893' или 'ogrn:1027700132195'
        source TEXT,         -- 'egrul', 'pb', 'efrsb', ...
        cached_at TEXT,      -- ISO-8601 UTC
        expires_at TEXT,     -- ISO-8601 UTC
        payload_json TEXT,
        PRIMARY KEY (key, source)
    )

Ключ ассиметричный: для одного ИНН могут быть отдельные записи от ЕГРЮЛ,
от ЕФРСБ и от Прозрачного Бизнеса с разными TTL.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

DEFAULT_TTL_HOURS = 24 * 7


@dataclass(slots=True)
class CacheRecord:
    key: str
    source: str
    cached_at: datetime
    expires_at: datetime
    payload: dict[str, Any]

    @property
    def is_expired(self) -> bool:
        return datetime.now(tz=timezone.utc) >= self.expires_at

    @property
    def age_hours(self) -> float:
        delta = datetime.now(tz=timezone.utc) - self.cached_at
        return delta.total_seconds() / 3600.0


_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS counterparty_cache (
    key          TEXT NOT NULL,
    source       TEXT NOT NULL,
    cached_at    TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (key, source)
);
CREATE INDEX IF NOT EXISTS idx_cache_expires
    ON counterparty_cache(expires_at);
"""


class SQLiteCache:
    """Async SQLite-кэш через aiosqlite.

    Использование:
        cache = SQLiteCache(Path("./cache.sqlite"), default_ttl_hours=168)
        await cache.init()
        await cache.put("inn:7707083893", "egrul", {"name": "..."})
        record = await cache.get("inn:7707083893", "egrul")
    """

    def __init__(self, db_path: Path | str, *, default_ttl_hours: int = DEFAULT_TTL_HOURS) -> None:
        self._db_path = str(db_path)
        self._default_ttl = timedelta(hours=default_ttl_hours)
        self._initialised = False

    async def init(self) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(_CREATE_SQL)
            await db.commit()
        self._initialised = True

    @staticmethod
    def make_key(*, inn: str | None = None, ogrn: str | None = None) -> str:
        if inn:
            return f"inn:{inn}"
        if ogrn:
            return f"ogrn:{ogrn}"
        raise ValueError("make_key requires either 'inn' or 'ogrn'")

    async def get(self, key: str, source: str) -> CacheRecord | None:
        await self._ensure_init()
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT cached_at, expires_at, payload_json FROM counterparty_cache "
                "WHERE key = ? AND source = ?",
                (key, source),
            ) as cur:
                row = await cur.fetchone()

        if row is None:
            return None

        cached_at = datetime.fromisoformat(row[0])
        expires_at = datetime.fromisoformat(row[1])
        try:
            payload = json.loads(row[2])
        except json.JSONDecodeError:
            return None

        return CacheRecord(
            key=key,
            source=source,
            cached_at=cached_at,
            expires_at=expires_at,
            payload=payload,
        )

    async def put(
        self,
        key: str,
        source: str,
        payload: dict[str, Any],
        *,
        ttl_hours: int | None = None,
    ) -> CacheRecord:
        await self._ensure_init()
        now = datetime.now(tz=timezone.utc)
        ttl = timedelta(hours=ttl_hours) if ttl_hours is not None else self._default_ttl
        expires = now + ttl

        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT INTO counterparty_cache (key, source, cached_at, expires_at, payload_json) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(key, source) DO UPDATE SET "
                "cached_at = excluded.cached_at, "
                "expires_at = excluded.expires_at, "
                "payload_json = excluded.payload_json",
                (
                    key,
                    source,
                    now.isoformat(),
                    expires.isoformat(),
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            await db.commit()

        return CacheRecord(
            key=key,
            source=source,
            cached_at=now,
            expires_at=expires,
            payload=payload,
        )

    async def delete(self, key: str, source: str) -> bool:
        await self._ensure_init()
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "DELETE FROM counterparty_cache WHERE key = ? AND source = ?",
                (key, source),
            )
            await db.commit()
            return cur.rowcount > 0

    async def purge_expired(self) -> int:
        await self._ensure_init()
        now_iso = datetime.now(tz=timezone.utc).isoformat()
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "DELETE FROM counterparty_cache WHERE expires_at <= ?",
                (now_iso,),
            )
            await db.commit()
            return cur.rowcount

    async def count(self) -> int:
        await self._ensure_init()
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM counterparty_cache") as cur:
                row = await cur.fetchone()
                return int(row[0]) if row else 0

    async def _ensure_init(self) -> None:
        if not self._initialised:
            await self.init()
