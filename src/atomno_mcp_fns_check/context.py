"""Сервис-контекст: общий держатель кэша и HTTP-клиентов.

На S2 контекст создаётся один раз при старте `server.main()` и переиспользуется
во всех тулзах. Это экономит соединения httpx (HTTP/2 connection pool)
и держит SQLite-кэш «горячим».

Тесты пользуются `ServiceContext.for_testing(...)` — там можно подсунуть
готовые моки клиентов и временный SQLite в tmp_path.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .db.cache import SQLiteCache
from .db.registries import RegistryStore
from .sources.efrsb import EfrsbClient
from .sources.egrul import EgrulClient
from .sources.fssp import FsspClient
from .sources.kad import KadClient
from .sources.pb_fns import PbFnsClient


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass
class ServiceContext:
    """Контейнер сервисов, передаётся в тулы по DI."""

    egrul: EgrulClient
    efrsb: EfrsbClient
    cache: SQLiteCache
    registries: RegistryStore | None = None
    pb_fns: PbFnsClient | None = None
    fssp: FsspClient | None = None
    kad: KadClient | None = None
    cache_ttl_hours: int = 168
    _entered: bool = field(default=False, init=False, repr=False)

    @classmethod
    def from_env(cls) -> "ServiceContext":
        """Собрать контекст из переменных окружения (см. .env.example)."""
        cache_db = os.environ.get(
            "MCP_FNS_CACHE_DB",
            str(Path.cwd() / "atomno_mcp_fns_check_cache.sqlite"),
        )
        registries_db = os.environ.get(
            "MCP_FNS_REGISTRIES_DB",
            str(Path(cache_db).with_suffix(".registries.sqlite")),
        )
        ttl = _env_int("MCP_FNS_CACHE_TTL_HOURS", 168)
        timeout = _env_float("MCP_FNS_HTTP_TIMEOUT", 15.0)
        ua = os.environ.get(
            "MCP_FNS_USER_AGENT",
            "atomno-mcp-fns-check/0.1 (+https://example.com/contact)",
        )
        return cls(
            egrul=EgrulClient(timeout=timeout, user_agent=ua),
            efrsb=EfrsbClient(timeout=min(timeout, 10.0), user_agent=ua),
            cache=SQLiteCache(cache_db, default_ttl_hours=ttl),
            registries=RegistryStore(registries_db),
            pb_fns=PbFnsClient(timeout=min(timeout, 10.0), user_agent=ua),
            fssp=FsspClient(timeout=min(timeout, 12.0), user_agent=ua),
            kad=KadClient(timeout=timeout, user_agent=ua),
            cache_ttl_hours=ttl,
        )

    @classmethod
    def for_testing(
        cls,
        *,
        egrul: EgrulClient,
        efrsb: EfrsbClient,
        cache: SQLiteCache,
        registries: RegistryStore | None = None,
        pb_fns: PbFnsClient | None = None,
        fssp: FsspClient | None = None,
        kad: KadClient | None = None,
        cache_ttl_hours: int = 168,
    ) -> "ServiceContext":
        return cls(
            egrul=egrul,
            efrsb=efrsb,
            cache=cache,
            registries=registries,
            pb_fns=pb_fns,
            fssp=fssp,
            kad=kad,
            cache_ttl_hours=cache_ttl_hours,
        )

    async def __aenter__(self) -> "ServiceContext":
        if self._entered:
            return self
        await self.egrul.__aenter__()
        await self.efrsb.__aenter__()
        await self.cache.init()
        if self.registries is not None:
            await self.registries.init()
        if self.pb_fns is not None:
            await self.pb_fns.__aenter__()
        if self.fssp is not None:
            await self.fssp.__aenter__()
        if self.kad is not None:
            await self.kad.__aenter__()
        self._entered = True
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if not self._entered:
            return
        await self.egrul.__aexit__(*exc_info)
        await self.efrsb.__aexit__(*exc_info)
        if self.pb_fns is not None:
            await self.pb_fns.__aexit__(*exc_info)
        if self.fssp is not None:
            await self.fssp.__aexit__(*exc_info)
        if self.kad is not None:
            await self.kad.__aexit__(*exc_info)
        self._entered = False
