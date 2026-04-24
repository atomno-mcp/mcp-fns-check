"""Тесты ETL Open Data ФНС → SQLite registries."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from atomno_mcp_fns_check.db.registries import RegistryStore
from atomno_mcp_fns_check.scripts.etl_registries import (
    build_records,
    commit_records,
    main,
)


def _write_csv(path: Path, content: str, encoding: str = "utf-8-sig") -> Path:
    path.write_text(content, encoding=encoding)
    return path


# --- mass_addresses ---


def test_build_records_mass_addresses_heuristic(tmp_path):
    src = _write_csv(
        tmp_path / "addr.csv",
        "Адрес;Количество;ДатаВключения\n"
        "127015, г Москва, ул Бумажная, д 1;47;2024-01-15\n"
        "115035, г Москва, ул Балчуг, д 7;12;2023-06-01\n",
    )
    records = build_records("mass_addresses", src)
    assert len(records) == 2
    assert records[0]["address"].startswith("127015")
    assert records[0]["registered_entities_count"] == 47
    assert records[0]["fns_inclusion_date"] == "2024-01-15"


def test_build_records_mass_addresses_explicit_columns(tmp_path):
    src = _write_csv(
        tmp_path / "addr2.csv",
        "addr;qty\n"
        "ул А, 1;5\n",
    )
    records = build_records(
        "mass_addresses",
        src,
        columns={"address": "addr", "registered_entities_count": "qty"},
    )
    assert records == [
        {"address": "ул А, 1", "registered_entities_count": 5, "fns_inclusion_date": None}
    ]


def test_build_records_skips_invalid_rows(tmp_path):
    src = _write_csv(
        tmp_path / "addr3.csv",
        "Адрес;Количество\n"
        ";5\n"
        "ул Б, 2;\n"
        "ул В, 3;abc\n"
        "ул Г, 4;42\n",
    )
    records = build_records("mass_addresses", src)
    assert records == [
        {"address": "ул Г, 4", "registered_entities_count": 42, "fns_inclusion_date": None}
    ]


# --- mass_directors ---


def test_build_records_mass_directors(tmp_path):
    src = _write_csv(
        tmp_path / "dir.csv",
        "ИНН;ФИО;Количество\n"
        "770700000099;ИВАНОВ ИВАН ИВАНОВИЧ;87\n",
    )
    records = build_records("mass_directors", src)
    assert len(records) == 1
    assert records[0]["director_inn"] == "770700000099"
    assert records[0]["companies_count"] == 87


# --- disqualified ---


def test_build_records_disqualified(tmp_path):
    src = _write_csv(
        tmp_path / "dq.csv",
        "ИНН;ФИО;ДатаДисквал;ДатаОкончания;Основание\n"
        "504700000056;СИДОРОВ С.С.;2024-02-10;2099-02-10;ст. 14.25 КоАП\n",
    )
    records = build_records("disqualified", src)
    assert len(records) == 1
    assert records[0]["person_inn"] == "504700000056"
    assert records[0]["disqualification_until"] == "2099-02-10"


# --- limit ---


def test_build_records_respects_limit(tmp_path):
    src = _write_csv(
        tmp_path / "addr.csv",
        "Адрес;Количество\n"
        + "\n".join(f"ул {i}, 1;{i+5}" for i in range(50))
        + "\n",
    )
    records = build_records("mass_addresses", src, limit=10)
    assert len(records) == 10


# --- commit ---


def test_commit_writes_to_registry(tmp_path):
    src = _write_csv(
        tmp_path / "addr.csv",
        "Адрес;Количество\nул К, 1;9\n",
    )
    db = tmp_path / "reg.sqlite"
    records = build_records("mass_addresses", src)
    n = asyncio.run(commit_records("mass_addresses", records, db, source_label=str(src)))
    assert n == 1

    async def _check():
        store = RegistryStore(db)
        await store.init()
        hit = await store.lookup_mass_address("ул К, 1")
        assert hit is not None
        assert hit.registered_entities_count == 9
        meta = await store.get_meta("mass_addresses.last_etl_count")
        assert meta == "1"

    asyncio.run(_check())


# --- CLI ---


def test_cli_dry_run_default(tmp_path, capsys):
    src = _write_csv(
        tmp_path / "addr.csv",
        "Адрес;Количество\nул Z, 1;7\n",
    )
    rc = main(
        [
            "--registry",
            "mass_addresses",
            "--source",
            str(src),
            "--db",
            str(tmp_path / "should_not_exist.sqlite"),
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "DRY-RUN" in captured.out
    # БД не должна быть создана.
    assert not (tmp_path / "should_not_exist.sqlite").exists()


def test_cli_commit_writes_db(tmp_path, capsys):
    src = _write_csv(
        tmp_path / "addr.csv",
        "Адрес;Количество\nул Y, 1;3\n",
    )
    db = tmp_path / "reg.sqlite"
    rc = main(
        [
            "--registry",
            "mass_addresses",
            "--source",
            str(src),
            "--db",
            str(db),
            "--commit",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "COMMIT" in captured.out
    assert db.exists()


def test_cli_missing_source_returns_error(tmp_path, capsys):
    rc = main(
        [
            "--registry",
            "mass_addresses",
            "--source",
            str(tmp_path / "nope.csv"),
        ]
    )
    assert rc == 2


def test_cli_zero_records_returns_warn(tmp_path, capsys):
    src = _write_csv(
        tmp_path / "addr.csv",
        "Адрес;Количество\n;\n",
    )
    rc = main(
        [
            "--registry",
            "mass_addresses",
            "--source",
            str(src),
        ]
    )
    assert rc == 1


def test_cli_unknown_columns_raises(tmp_path):
    src = _write_csv(
        tmp_path / "addr.csv",
        "Адрес;Количество\nул, 1;5\n",
    )
    with pytest.raises(SystemExit):
        main(
            [
                "--registry",
                "mass_addresses",
                "--source",
                str(src),
                "--columns",
                "address=НесуществуетКолонка",
            ]
        )
