"""Локальное хранилище: SQLite-кэш карточек + реестры Open Data ФНС."""

from .cache import CacheRecord, SQLiteCache
from .registries import (
    DisqualifiedHit,
    MassAddressHit,
    MassDirectorHit,
    RegistryStore,
    normalise_address,
)

__all__ = [
    "CacheRecord",
    "DisqualifiedHit",
    "MassAddressHit",
    "MassDirectorHit",
    "RegistryStore",
    "SQLiteCache",
    "normalise_address",
]
