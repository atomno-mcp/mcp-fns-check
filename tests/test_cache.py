"""Тесты SQLiteCache: put/get/expiry/purge."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from atomno_mcp_fns_check.db.cache import SQLiteCache


@pytest.fixture
async def cache(cache_db) -> SQLiteCache:
    c = SQLiteCache(cache_db, default_ttl_hours=1)
    await c.init()
    return c


class TestCache:
    async def test_put_and_get(self, cache: SQLiteCache) -> None:
        key = SQLiteCache.make_key(inn="7707083893")
        record = await cache.put(key, "egrul", {"name": "Сбербанк"})
        assert record.payload == {"name": "Сбербанк"}

        loaded = await cache.get(key, "egrul")
        assert loaded is not None
        assert loaded.payload == {"name": "Сбербанк"}
        assert loaded.is_expired is False
        assert loaded.age_hours < 0.01

    async def test_get_missing_returns_none(self, cache: SQLiteCache) -> None:
        result = await cache.get("inn:0000000000", "egrul")
        assert result is None

    async def test_make_key_requires_argument(self) -> None:
        with pytest.raises(ValueError):
            SQLiteCache.make_key()

    async def test_make_key_ogrn(self) -> None:
        assert SQLiteCache.make_key(ogrn="1027700132195") == "ogrn:1027700132195"

    async def test_overwrite_on_conflict(self, cache: SQLiteCache) -> None:
        key = "inn:7707083893"
        await cache.put(key, "egrul", {"v": 1})
        await cache.put(key, "egrul", {"v": 2})
        record = await cache.get(key, "egrul")
        assert record is not None
        assert record.payload == {"v": 2}
        assert await cache.count() == 1

    async def test_multiple_sources_for_same_key(self, cache: SQLiteCache) -> None:
        key = "inn:7707083893"
        await cache.put(key, "egrul", {"src": "egrul"})
        await cache.put(key, "pb", {"src": "pb"})
        assert await cache.count() == 2

        e = await cache.get(key, "egrul")
        p = await cache.get(key, "pb")
        assert e is not None and e.payload == {"src": "egrul"}
        assert p is not None and p.payload == {"src": "pb"}

    async def test_delete(self, cache: SQLiteCache) -> None:
        key = "inn:7707083893"
        await cache.put(key, "egrul", {"n": "x"})
        assert await cache.delete(key, "egrul") is True
        assert await cache.delete(key, "egrul") is False
        assert await cache.get(key, "egrul") is None

    async def test_purge_expired(self, cache_db) -> None:
        c = SQLiteCache(cache_db, default_ttl_hours=24)
        await c.init()
        await c.put("inn:1", "egrul", {"v": 1}, ttl_hours=24)
        await c.put("inn:2", "egrul", {"v": 2}, ttl_hours=24)

        # Принудительно делаем одну запись просроченной через прямую SQL-правку.
        import aiosqlite

        past = (datetime.now(tz=timezone.utc) - timedelta(hours=1)).isoformat()
        async with aiosqlite.connect(str(cache_db)) as db:
            await db.execute(
                "UPDATE counterparty_cache SET expires_at = ? WHERE key = ?",
                (past, "inn:1"),
            )
            await db.commit()

        deleted = await c.purge_expired()
        assert deleted == 1
        assert await c.count() == 1

    async def test_init_idempotent(self, cache_db) -> None:
        c = SQLiteCache(cache_db)
        await c.init()
        await c.init()
        assert await c.count() == 0
