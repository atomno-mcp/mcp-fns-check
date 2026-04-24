"""Тесты SQLite-реестров (db/registries.py)."""

from __future__ import annotations

import pytest

from atomno_mcp_fns_check.db.registries import RegistryStore, normalise_address


class TestNormaliseAddress:
    def test_strips_postal_index(self):
        assert (
            normalise_address("127015, г Москва, ул Бумажный проезд, д 14 стр 2")
            == "г москва ул бумажный проезд д 14 стр 2"
        )

    def test_lower_and_strip_punct(self):
        assert normalise_address("Г.МОСКВА, УЛ. ВАВИЛОВА, Д. 19") == "г москва ул вавилова д 19"

    def test_collapses_spaces(self):
        assert normalise_address("  ул   Тверская   1  ") == "ул тверская 1"

    def test_empty(self):
        assert normalise_address("") == ""


class TestMassAddresses:
    async def test_upsert_and_lookup(self, tmp_path):
        store = RegistryStore(tmp_path / "reg.sqlite")
        await store.init()

        n = await store.upsert_mass_addresses(
            [
                {
                    "address": "127015, г Москва, ул Бумажная, д 1",
                    "fns_inclusion_date": "2024-01-15",
                    "registered_entities_count": 47,
                }
            ]
        )
        assert n == 1

        hit = await store.lookup_mass_address("127015 Г.МОСКВА УЛ. БУМАЖНАЯ Д. 1")
        assert hit is not None
        assert hit.registered_entities_count == 47
        assert hit.fns_inclusion_date == "2024-01-15"

    async def test_lookup_miss(self, tmp_path):
        store = RegistryStore(tmp_path / "r.sqlite")
        await store.init()
        assert await store.lookup_mass_address("ул Несуществующая, 999") is None

    async def test_upsert_skips_no_address(self, tmp_path):
        store = RegistryStore(tmp_path / "r.sqlite")
        await store.init()
        assert await store.upsert_mass_addresses([{"foo": "bar"}]) == 0

    async def test_upsert_overrides(self, tmp_path):
        store = RegistryStore(tmp_path / "r.sqlite")
        await store.init()
        await store.upsert_mass_addresses(
            [{"address": "ул А, 1", "registered_entities_count": 10}]
        )
        await store.upsert_mass_addresses(
            [{"address": "ул А, 1", "registered_entities_count": 99}]
        )
        hit = await store.lookup_mass_address("ул А, 1")
        assert hit is not None
        assert hit.registered_entities_count == 99


class TestMassDirectors:
    async def test_upsert_and_lookup_by_inn(self, tmp_path):
        store = RegistryStore(tmp_path / "r.sqlite")
        await store.init()
        await store.upsert_mass_directors(
            [{"director_inn": "770700000099", "full_name": "ИВАНОВ И.И.", "companies_count": 87}]
        )
        hit = await store.lookup_mass_director("770700000099")
        assert hit is not None
        assert hit.companies_count == 87

    async def test_lookup_miss(self, tmp_path):
        store = RegistryStore(tmp_path / "r.sqlite")
        await store.init()
        assert await store.lookup_mass_director("000000000000") is None


class TestDisqualified:
    async def test_lookup_by_inn(self, tmp_path):
        store = RegistryStore(tmp_path / "r.sqlite")
        await store.init()
        await store.upsert_disqualified(
            [
                {
                    "person_inn": "504700000056",
                    "full_name": "СИДОРОВ С.С.",
                    "disqualification_date": "2024-02-10",
                    "disqualification_until": "2027-02-10",
                    "reason": "ст. 14.25 КоАП",
                }
            ]
        )
        hits = await store.lookup_disqualified(inn="504700000056")
        assert len(hits) == 1
        assert hits[0].disqualification_until == "2027-02-10"

    async def test_lookup_by_name_case_insensitive(self, tmp_path):
        store = RegistryStore(tmp_path / "r.sqlite")
        await store.init()
        await store.upsert_disqualified(
            [
                {
                    "person_inn": None,
                    "full_name": "Кузнецова Елена Владимировна",
                    "disqualification_date": "2025-08-04",
                }
            ]
        )
        hits = await store.lookup_disqualified(full_name="КУЗНЕЦОВА ЕЛЕНА ВЛАДИМИРОВНА")
        assert len(hits) == 1

    async def test_lookup_no_args_returns_empty(self, tmp_path):
        store = RegistryStore(tmp_path / "r.sqlite")
        await store.init()
        assert await store.lookup_disqualified() == []


class TestSeed:
    async def test_load_bundled_seed(self, tmp_path):
        from importlib.resources import files

        seed = files("atomno_mcp_fns_check.data").joinpath("registries_seed.json")
        store = RegistryStore(tmp_path / "r.sqlite")
        await store.init()

        with seed.open("r", encoding="utf-8") as f:
            import json

            data = json.load(f)
            tmp_seed = tmp_path / "seed.json"
            tmp_seed.write_text(json.dumps(data), encoding="utf-8")

        counts = await store.load_seed(tmp_seed)
        assert counts["mass_addresses"] >= 1
        assert counts["mass_directors"] >= 1
        assert counts["disqualified_directors"] >= 1
        assert await store.get_meta("seed_version") is not None


class TestMeta:
    async def test_meta_set_get(self, tmp_path):
        store = RegistryStore(tmp_path / "r.sqlite")
        await store.set_meta("foo", "bar")
        assert await store.get_meta("foo") == "bar"

    async def test_meta_missing(self, tmp_path):
        store = RegistryStore(tmp_path / "r.sqlite")
        assert await store.get_meta("nope") is None


@pytest.fixture
async def seeded_store(tmp_path):
    """Реестр со sample-данными, готовый к lookup-тестам."""
    store = RegistryStore(tmp_path / "r.sqlite")
    await store.init()
    await store.upsert_mass_addresses(
        [{"address": "ул Бумажная, 1", "registered_entities_count": 50}]
    )
    await store.upsert_mass_directors(
        [{"director_inn": "770700000099", "full_name": "ИВАНОВ ИВАН", "companies_count": 87}]
    )
    await store.upsert_disqualified(
        [
            {
                "person_inn": "504700000056",
                "full_name": "СИДОРОВ С.С.",
                "disqualification_date": "2024-02-10",
                "disqualification_until": "2027-02-10",
            }
        ]
    )
    return store


class TestSeededLookup:
    async def test_address_hit(self, seeded_store):
        hit = await seeded_store.lookup_mass_address("УЛ БУМАЖНАЯ 1")
        assert hit is not None and hit.registered_entities_count == 50

    async def test_director_hit(self, seeded_store):
        hit = await seeded_store.lookup_mass_director("770700000099")
        assert hit is not None and hit.companies_count == 87

    async def test_disqualified_hit(self, seeded_store):
        hits = await seeded_store.lookup_disqualified(inn="504700000056")
        assert len(hits) == 1
