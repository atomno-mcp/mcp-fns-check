"""Тесты загрузчика открытых данных ФНС в локальные реестры.

Куски файлов в тестах ниже — настоящие, скопированы из выгрузок ФНС,
скачанных 02.08.2026 (`masaddress`, `massleaders`, `massfounders`,
`disqualifiedpersons`, `debtam`). Выдуманных заголовков и полей здесь нет:
ФНС озаглавливает файлы как `G1;G2;...`, и раскладку колонок мы держим в
`_FNS_LAYOUTS`. Если формат поменяется — тест должен упасть.
"""

from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path

import httpx
import pytest
import respx

from atomno_mcp_fns_check.db.registries import RegistryStore
from atomno_mcp_fns_check.scripts.etl_registries import (
    FNS_OPENDATA_PAGE,
    build_records,
    commit_records,
    commit_tax_debts,
    iter_debtam_records,
    main,
    resolve_latest_file,
)

FIXTURES = Path(__file__).parent / "fixtures"


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


# --- настоящий формат файлов ФНС (заголовки G1;G2;...) ---


def test_real_fns_mass_address_layout(tmp_path):
    """Кусок настоящего `masaddress`: адрес собирается из восьми колонок."""
    src = _write_csv(
        tmp_path / "masaddress.csv",
        "G1;G2;G3;G4;G5;G6;G7;G8;G9\n"
        "АДЫГЕЯ РЕСП;;МАЙКОП Г;;ГАГАРИНА УЛ;7;;;17\n"
        "АДЫГЕЯ РЕСП;ТАХТАМУКАЙСКИЙ Р-Н;;ЯБЛОНОВСКИЙ ПГТ;ЛЕНИНА УЛ;39;А;;15\n",
    )
    records = build_records("mass_addresses", src, fns_slug="masaddress")
    assert records == [
        {
            "address": "АДЫГЕЯ РЕСП МАЙКОП Г ГАГАРИНА УЛ 7",
            "registered_entities_count": 17,
            "fns_inclusion_date": None,
        },
        {
            "address": "АДЫГЕЯ РЕСП ТАХТАМУКАЙСКИЙ Р-Н ЯБЛОНОВСКИЙ ПГТ ЛЕНИНА УЛ 39 А",
            "registered_entities_count": 15,
            "fns_inclusion_date": None,
        },
    ]


def test_real_fns_mass_leaders_layout(tmp_path):
    """Кусок настоящего `massleaders`: ИНН, фамилия/имя/отчество, число фирм."""
    src = _write_csv(
        tmp_path / "massleaders.csv",
        "G1;G2;G3;G4;G5\n774315346780;СМЕРЕЦКИЙ;АЛЕКСЕЙ;АЛЕКСАНДРОВИЧ;10\n",
    )
    records = build_records("mass_directors", src, fns_slug="massleaders")
    assert records == [
        {
            "director_inn": "774315346780",
            "full_name": "СМЕРЕЦКИЙ АЛЕКСЕЙ АЛЕКСАНДРОВИЧ",
            "companies_count": 10,
            "fns_inclusion_date": None,
            "role": "leader",
        }
    ]


def test_real_fns_mass_founders_get_founder_role(tmp_path):
    """Учредитель не должен отчитываться как руководитель."""
    src = _write_csv(
        tmp_path / "massfounders.csv",
        "G1;G2;G3;G4;G5\n550300914931;ВАРНАВСКИЙ;ПЕТР;ВЛАДИМИРОВИЧ;20\n",
    )
    records = build_records("mass_directors", src, fns_slug="massfounders")
    assert records[0]["role"] == "founder"
    assert records[0]["companies_count"] == 20


def test_real_fns_disqualified_entities_layout(tmp_path):
    """Набор `disqualifiedpersons` — это организации, а не физлица."""
    src = _write_csv(
        tmp_path / "disqualifiedpersons.csv",
        "G1;G2;G3;G4;G5\n"
        'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "КАРТЕН";1175749002263;'
        "5751060080;575101001;302030,ОРЛОВСКАЯ ОБЛАСТЬ\n",
    )
    records = build_records(
        "disqualified_entities", src, fns_slug="disqualifiedpersons"
    )
    assert records == [
        {
            "company_inn": "5751060080",
            "company_ogrn": "1175749002263",
            "company_name": 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "КАРТЕН"',
            "company_kpp": "575101001",
            "address_raw": "302030,ОРЛОВСКАЯ ОБЛАСТЬ",
        }
    ]


def test_unknown_fns_layout_fails_loudly(tmp_path):
    """Файл ФНС без описанной раскладки — ошибка, а не тихий ноль записей."""
    src = _write_csv(tmp_path / "x.csv", "G1;G2\nа;б\n")
    with pytest.raises(SystemExit) as exc:
        build_records("mass_addresses", src, fns_slug="неизвестный_набор")
    assert "раскладка" in str(exc.value).lower()


# --- недоимка: настоящий XML из архива ФНС ---


def _debtam_zip(tmp_path: Path) -> Path:
    archive = tmp_path / "debtam.zip"
    sample = (FIXTURES / "debtam_sample.xml").read_bytes()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("VO_OTKRDAN_6_9965_9965_20260725_0d2c3369.xml", sample)
    return archive


def test_debtam_parses_real_xml(tmp_path):
    records = list(iter_debtam_records(_debtam_zip(tmp_path)))
    by_inn = {r["inn"]: r for r in records}

    assert "5506193814" in by_inn
    cefey = by_inn["5506193814"]
    assert "ЦЕФЕЙ" in cefey["org_name"]
    # 14190.00 + 2769.00 + 9885.00
    assert cefey["total_debt_rub"] == pytest.approx(26844.0)
    assert cefey["report_date"] == "2026-07-01"
    names = {t["name"] for t in cefey["taxes"]}
    assert "Налог на добавленную стоимость" in names


def test_debtam_keeps_penalties_separate(tmp_path):
    records = {r["inn"]: r for r in iter_debtam_records(_debtam_zip(tmp_path))}
    alliance = records["5907051214"]
    assert alliance["penalty_rub"] == pytest.approx(12733.24)
    assert alliance["total_debt_rub"] == pytest.approx(31260.1)


def test_debtam_commit_writes_rows(tmp_path):
    db = tmp_path / "reg.sqlite"
    written = asyncio.run(
        commit_tax_debts(_debtam_zip(tmp_path), db, source_label="test", data_as_of=None)
    )
    assert written == 2

    async def _check():
        store = RegistryStore(db)
        await store.init()
        hit = await store.lookup_tax_debt("5506193814")
        assert hit is not None
        assert hit.total_debt_rub == pytest.approx(26844.0)
        assert await store.count_rows("tax_debts") == 2

    asyncio.run(_check())


# --- поиск свежего файла на сайте ФНС ---


_FNS_PAGE_HTML = """
<html><body>
<a href="https://file.nalog.ru/opendata/7707329152-debtam/data-20260725-structure-20180801.zip">Скачать</a>
<a href="https://file.nalog.ru/opendata/7707329152-debtam/data-20260625-structure-20180801.zip">Предыдущий</a>
</body></html>
"""


def test_resolve_latest_file_takes_newest():
    with respx.mock() as m:
        m.get(FNS_OPENDATA_PAGE.format(slug="debtam")).mock(
            return_value=httpx.Response(200, text=_FNS_PAGE_HTML)
        )
        url, data_as_of = resolve_latest_file("debtam")

    assert url.endswith("data-20260725-structure-20180801.zip")
    assert data_as_of.isoformat() == "2026-07-25"


def test_resolve_latest_file_without_links_fails_loudly():
    with respx.mock() as m:
        m.get(FNS_OPENDATA_PAGE.format(slug="debtam")).mock(
            return_value=httpx.Response(200, text="<html><body>нет ссылок</body></html>")
        )
        with pytest.raises(SystemExit) as exc:
            resolve_latest_file("debtam")
    assert "debtam" in str(exc.value)


# --- CLI ---


def test_cli_preview_by_default(tmp_path, capsys):
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
    assert "ПРЕДПРОСМОТР" in captured.out
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
    assert "ЗАПИСАНО" in captured.out
    assert db.exists()


def test_cli_missing_source_file_fails_loudly(tmp_path):
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "--registry",
                "mass_addresses",
                "--source",
                str(tmp_path / "nope.csv"),
            ]
        )
    assert "не найден" in str(exc.value)


def test_cli_without_source_and_without_download_returns_error(tmp_path, capsys):
    rc = main(["--registry", "mass_addresses", "--db", str(tmp_path / "reg.sqlite")])
    assert rc == 2
    assert "--source" in capsys.readouterr().err


def test_cli_zero_records_fails_instead_of_writing_empty(tmp_path):
    """Пустой разбор — это ошибка загрузки, а не «реестр пуст»."""
    src = _write_csv(
        tmp_path / "addr.csv",
        "Адрес;Количество\n;\n",
    )
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "--registry",
                "mass_addresses",
                "--source",
                str(src),
                "--db",
                str(tmp_path / "reg.sqlite"),
            ]
        )
    assert "0 записей" in str(exc.value)


def test_cli_status_reports_empty_registries(tmp_path, capsys):
    """`--status` должен показывать пустые реестры как непригодные."""
    rc = main(["--status", "--db", str(tmp_path / "empty.sqlite")])
    out = capsys.readouterr().out
    assert rc != 0, "Пустые реестры — повод для ненулевого кода возврата"
    assert "mass_addresses" in out
    assert "не загружен" in out or "пуст" in out


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
