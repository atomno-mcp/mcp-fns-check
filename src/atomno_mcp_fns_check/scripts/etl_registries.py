"""ETL открытых данных ФНС → локальные реестры atomno-mcp-fns-check.

Скрипт умеет сам находить свежий файл набора на сайте ФНС, скачивать его и
раскладывать по таблицам SQLite (`RegistryStore`). По умолчанию работает в
режиме предпросмотра — только разбирает файл и печатает статистику; для
записи нужен явный флаг `--commit`.

Наборы открытых данных (проверены 02.08.2026, все скачиваются свободно):

  ┌───────────────────────┬────────────────────────┬──────────────────────────┐
  │ Реестр                │ Набор ФНС              │ Что внутри               │
  ├───────────────────────┼────────────────────────┼──────────────────────────┤
  │ mass_addresses        │ masaddress             │ адреса массовой          │
  │                       │                        │ регистрации              │
  │ mass_directors        │ massleaders +          │ массовые руководители    │
  │                       │ massfounders           │ и учредители             │
  │ disqualified_entities │ disqualifiedpersons    │ организации, где в       │
  │                       │                        │ управлении есть          │
  │                       │                        │ дисквалифицированный     │
  │ tax_debts             │ debtam                 │ недоимка, пени, штрафы   │
  └───────────────────────┴────────────────────────┴──────────────────────────┘

Важно про формат CSV ФНС: в файлах нет осмысленных заголовков — первая
строка выглядит как `G1;G2;G3;...`. Поэтому колонки определяются по
официальной раскладке набора (см. `_FNS_LAYOUTS`), а старая эвристика по
русским заголовкам сохранена для ручных выгрузок.

Про свежесть. Скрипт записывает в реестр дату самих данных ФНС
(`<реестр>.data_as_of`), адрес файла и время загрузки. Проверки риска
опираются на эту дату: устаревший реестр обязан давать «не проверено»,
а не «нарушений не найдено».

Запуск::

    # всё сразу: найти свежие файлы, скачать, загрузить
    python -m atomno_mcp_fns_check.scripts.etl_registries --all --download --commit

    # один реестр из уже скачанного файла
    python -m atomno_mcp_fns_check.scripts.etl_registries \\
        --registry mass_addresses --source ./fns/masaddress.csv --commit

    # посмотреть, что лежит в базе и насколько оно свежее
    python -m atomno_mcp_fns_check.scripts.etl_registries --status

Как часто запускать: `debtam` ФНС обновляет ежемесячно (25-го числа),
остальные наборы — «по мере изменений». Разумное расписание — раз в неделю;
повторный запуск идемпотентен (записи перезаписываются по ключу).
"""

from __future__ import annotations

import argparse
import asyncio
import codecs
import csv
import io
import logging
import os
import re
import sys
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import httpx

from atomno_mcp_fns_check.db.registries import RegistryStore

logger = logging.getLogger("atomno_mcp_fns_check.etl")

FNS_OPENDATA_PAGE = "https://www.nalog.gov.ru/opendata/7707329152-{slug}/"
DEFAULT_USER_AGENT = "atomno-mcp-fns-check/0.5 (+https://atomno-mcp.ru)"
DOWNLOAD_TIMEOUT = 600.0

_FILE_LINK_RE = re.compile(
    r'href="([^"]*?/opendata/[^"]*?data-(\d{8})[^"]*?\.(?:zip|csv))"', re.I
)


@dataclass(frozen=True, slots=True)
class DatasetSpec:
    """Описание набора открытых данных ФНС."""

    slug: str
    registry: str
    title_ru: str
    kind: str  # "csv" | "debtam_zip"


#: Какие наборы ФНС наполняют какие реестры. Порядок важен для `--all`:
#: массовые руководители и учредители кладутся в одну таблицу.
DATASETS: tuple[DatasetSpec, ...] = (
    DatasetSpec("masaddress", "mass_addresses", "Адреса массовой регистрации", "csv"),
    DatasetSpec("massleaders", "mass_directors", "Массовые руководители", "csv"),
    DatasetSpec("massfounders", "mass_directors", "Массовые учредители", "csv"),
    DatasetSpec(
        "disqualifiedpersons",
        "disqualified_entities",
        "Организации с дисквалифицированными в управлении",
        "csv",
    ),
    DatasetSpec("debtam", "tax_debts", "Недоимка, пени и штрафы", "debtam_zip"),
)

DATASETS_BY_SLUG: dict[str, DatasetSpec] = {d.slug: d for d in DATASETS}

#: Официальная раскладка колонок CSV-наборов ФНС (файлы озаглавлены G1..Gn).
#: Значение — либо индекс колонки, либо список индексов (для сборного адреса).
_FNS_LAYOUTS: dict[str, dict[str, Any]] = {
    "masaddress": {
        "address_parts": [0, 1, 2, 3, 4, 5, 6, 7],
        "registered_entities_count": 8,
    },
    "massleaders": {
        "director_inn": 0,
        "name_parts": [1, 2, 3],
        "companies_count": 4,
    },
    "massfounders": {
        "director_inn": 0,
        "name_parts": [1, 2, 3],
        "companies_count": 4,
        "role": "founder",
    },
    "disqualifiedpersons": {
        "company_name": 0,
        "company_ogrn": 1,
        "company_inn": 2,
        "company_kpp": 3,
        "address_raw": 4,
    },
}

# Эвристики угадывания колонок для ручных выгрузок с человеческими заголовками.
_HEURISTICS: dict[str, dict[str, list[str]]] = {
    "mass_addresses": {
        "address": ["адрес", "адресмр", "addressfull", "fulladdress"],
        "registered_entities_count": [
            "количество",
            "коликоюл",
            "колво_юл",
            "юлколво",
            "qty",
            "count",
        ],
        "fns_inclusion_date": [
            "датавключения",
            "дата_включения",
            "datein",
            "date_in",
            "inclusiondate",
        ],
    },
    "mass_directors": {
        "director_inn": ["инн", "innfl", "fl_inn"],
        "full_name": ["фио", "fullname", "fl_fio"],
        "companies_count": [
            "количество",
            "коликоюл",
            "колворог",
            "колво_юл",
            "qty",
            "count",
        ],
        "fns_inclusion_date": ["датавключения", "дата_включения", "datein"],
    },
    "disqualified": {
        "person_inn": ["инн", "innfl"],
        "full_name": ["фио", "fullname", "fl_fio"],
        "disqualification_date": ["датадисквал", "datestart", "datefrom"],
        "disqualification_until": ["датаокончания", "dateend", "dateto"],
        "reason": ["основание", "причина", "reason", "ground"],
    },
    "disqualified_entities": {
        "company_inn": ["инн", "иннюл"],
        "company_ogrn": ["огрн"],
        "company_name": ["наименование", "наимюл", "name"],
        "company_kpp": ["кпп"],
        "address_raw": ["адрес", "address"],
    },
}


# --- поиск и скачивание файлов ФНС ---


def _parse_file_date(digits: str, url: str) -> date | None:
    """Разобрать дату из имени файла.

    ФНС использует два формата в разных хранилищах: `data.nalog.ru` — ДДММГГГГ,
    `file.nalog.ru` — ГГГГММДД. Определяем по хосту, с запасным перебором.
    """
    orders = (
        [("ymd",), ("dmy",)] if "file.nalog.ru" in url else [("dmy",), ("ymd",)]
    )
    for (order,) in orders:
        try:
            if order == "dmy":
                return date(int(digits[4:8]), int(digits[2:4]), int(digits[0:2]))
            return date(int(digits[0:4]), int(digits[4:6]), int(digits[6:8]))
        except ValueError:
            continue
    return None


def resolve_latest_file(slug: str, *, user_agent: str = DEFAULT_USER_AGENT) -> tuple[str, date]:
    """Найти на странице набора ФНС самый свежий файл данных.

    Возвращает (адрес файла, дата данных). Поднимает `SystemExit` с понятным
    текстом, если страница не открылась или ссылок на файлы нет — молча
    «пропускать» набор нельзя, иначе загрузка тихо не состоится.
    """
    page = FNS_OPENDATA_PAGE.format(slug=slug)
    try:
        with httpx.Client(
            timeout=60.0, headers={"User-Agent": user_agent}, follow_redirects=True
        ) as client:
            response = client.get(page)
    except httpx.HTTPError as exc:
        raise SystemExit(f"Не удалось открыть страницу набора {slug}: {exc}") from exc
    if response.status_code != 200:
        raise SystemExit(f"Страница набора {slug} вернула HTTP {response.status_code}: {page}")

    best_url: str | None = None
    best_date: date | None = None
    for raw_url, digits in _FILE_LINK_RE.findall(response.text):
        url = raw_url
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/"):
            url = "https://www.nalog.gov.ru" + url
        parsed = _parse_file_date(digits, url)
        if parsed is None:
            continue
        if best_date is None or parsed > best_date:
            best_url, best_date = url, parsed

    if best_url is None or best_date is None:
        raise SystemExit(
            f"На странице набора {slug} не найдено ни одной ссылки на файл данных: {page}"
        )
    return best_url, best_date


def download_file(url: str, dest: Path, *, user_agent: str = DEFAULT_USER_AGENT) -> Path:
    """Скачать файл набора ФНС в `dest` (потоково, чтобы не держать в памяти)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with httpx.Client(
        timeout=DOWNLOAD_TIMEOUT, headers={"User-Agent": user_agent}, follow_redirects=True
    ) as client:
        with client.stream("GET", url) as response:
            if response.status_code != 200:
                raise SystemExit(f"Скачивание {url} вернуло HTTP {response.status_code}")
            with tmp.open("wb") as fh:
                for chunk in response.iter_bytes(chunk_size=1 << 20):
                    fh.write(chunk)
    tmp.replace(dest)
    return dest


# --- разбор CSV ---


def _normalise_header(s: str) -> str:
    return "".join(ch.lower() for ch in s if ch.isalnum())


def _build_column_map(
    registry: str, headers: list[str], explicit: dict[str, str] | None
) -> dict[str, int]:
    """Сопоставить логические поля → индексы колонок CSV."""
    norm_headers = [_normalise_header(h) for h in headers]
    mapping: dict[str, int] = {}

    if explicit:
        for field_name, col_name in explicit.items():
            try:
                idx = headers.index(col_name)
            except ValueError:
                norm_col = _normalise_header(col_name)
                if norm_col in norm_headers:
                    idx = norm_headers.index(norm_col)
                else:
                    raise SystemExit(
                        f"--columns: колонка '{col_name}' "
                        f"для поля '{field_name}' не найдена в CSV. Доступны: {headers}"
                    ) from None
            mapping[field_name] = idx

    heur = _HEURISTICS.get(registry, {})
    for field_name, candidates in heur.items():
        if field_name in mapping:
            continue
        for cand in candidates:
            if cand in norm_headers:
                mapping[field_name] = norm_headers.index(cand)
                break

    return mapping


def _parse_columns_arg(raw: str | None) -> dict[str, str] | None:
    if not raw:
        return None
    out: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise SystemExit(f"--columns: пара '{pair}' должна быть в формате key=value")
        k, v = pair.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _to_int(v: str) -> int | None:
    if not v:
        return None
    v = v.strip().replace("\xa0", "").replace(" ", "")
    if not v:
        return None
    try:
        return int(float(v))
    except ValueError:
        return None


def _detect_encoding(source: Path) -> str:
    """Определить кодировку файла ФНС (utf-8 либо windows-1251).

    Проверяем начало файла инкрементальным декодером: обычный `bytes.decode`
    спотыкается о многобайтовый символ, разрезанный границей куска, и ошибочно
    объявляет файл однобайтовым.
    """
    head = source.open("rb").read(200_000)
    for enc in ("utf-8-sig", "cp1251"):
        decoder = codecs.getincrementaldecoder(enc)()
        try:
            decoder.decode(head, False)
        except UnicodeDecodeError:
            continue
        return enc
    return "cp1251"


def _iter_rows(
    source: Path, *, delimiter: str, encoding: str
) -> Iterator[tuple[list[str], list[str]]]:
    """Yield (headers, row) пар из CSV, headers повторяются на каждый row."""
    with source.open("r", encoding=encoding, newline="") as fh:
        reader = csv.reader(fh, delimiter=delimiter)
        try:
            headers = next(reader)
        except StopIteration:
            return
        for row in reader:
            yield headers, row


def _row_value(row: list[str], idx: int | None) -> str | None:
    if idx is None or idx >= len(row):
        return None
    v = row[idx].strip()
    return v or None


def _is_fns_layout(headers: list[str]) -> bool:
    """Файл ФНС озаглавлен как G1;G2;G3... — осмысленных названий колонок нет."""
    return bool(headers) and all(re.fullmatch(r"G\d+", h.strip()) for h in headers if h.strip())


def _join_parts(row: list[str], indexes: list[int]) -> str:
    parts = [(row[i].strip() if i < len(row) else "") for i in indexes]
    return " ".join(p for p in parts if p)


def build_records(
    registry: str,
    source: Path,
    *,
    columns: dict[str, str] | None = None,
    delimiter: str = ";",
    encoding: str | None = "utf-8-sig",
    limit: int | None = None,
    fns_slug: str | None = None,
) -> list[dict[str, Any]]:
    """Собрать список словарей-записей под формат `RegistryStore` из CSV.

    Если файл озаглавлен как `G1;G2;...` (так выкладывает ФНС), колонки
    определяются по официальной раскладке набора `fns_slug`. Иначе работает
    старая эвристика по русским заголовкам плюс явный `--columns`.
    """
    records: list[dict[str, Any]] = []
    column_map: dict[str, int] | None = None
    layout: dict[str, Any] | None = None
    if encoding is None:
        encoding = _detect_encoding(source)

    for headers, row in _iter_rows(source, delimiter=delimiter, encoding=encoding):
        if column_map is None and layout is None:
            if _is_fns_layout(headers):
                layout = _FNS_LAYOUTS.get(fns_slug or "")
                if layout is None:
                    raise SystemExit(
                        f"Файл {source.name} в формате ФНС (колонки {headers}), но "
                        f"раскладка для набора '{fns_slug}' не описана. "
                        "Добавьте её в _FNS_LAYOUTS."
                    )
            else:
                column_map = _build_column_map(registry, headers, columns)
                if not column_map:
                    raise SystemExit(
                        f"Не удалось определить колонки для реестра '{registry}'. "
                        f"Headers: {headers}. Передайте --columns явно."
                    )

        record = (
            _record_from_layout(registry, row, layout)
            if layout is not None
            else _record_from_map(registry, row, column_map or {})
        )
        if record is not None:
            records.append(record)

        if limit is not None and len(records) >= limit:
            break

    return records


def _record_from_layout(
    registry: str, row: list[str], layout: dict[str, Any]
) -> dict[str, Any] | None:
    """Собрать запись из строки файла ФНС по официальной раскладке колонок."""
    if registry == "mass_addresses":
        address = _join_parts(row, layout["address_parts"])
        count = _to_int(_row_value(row, layout["registered_entities_count"]) or "")
        if not address or count is None:
            return None
        return {
            "address": address,
            "registered_entities_count": count,
            "fns_inclusion_date": None,
        }
    if registry == "mass_directors":
        inn = _row_value(row, layout["director_inn"])
        count = _to_int(_row_value(row, layout["companies_count"]) or "")
        if not inn or count is None:
            return None
        return {
            "director_inn": inn,
            "full_name": _join_parts(row, layout["name_parts"]) or None,
            "companies_count": count,
            "fns_inclusion_date": None,
            "role": layout.get("role", "leader"),
        }
    if registry == "disqualified_entities":
        inn = _row_value(row, layout["company_inn"])
        if not inn:
            return None
        return {
            "company_inn": inn,
            "company_ogrn": _row_value(row, layout["company_ogrn"]),
            "company_name": _row_value(row, layout["company_name"]),
            "company_kpp": _row_value(row, layout["company_kpp"]),
            "address_raw": _row_value(row, layout["address_raw"]),
        }
    raise SystemExit(f"Раскладка ФНС не поддержана для реестра: {registry}")


def _record_from_map(
    registry: str, row: list[str], column_map: dict[str, int]
) -> dict[str, Any] | None:
    """Собрать запись по карте колонок (ручные выгрузки с заголовками)."""
    if registry == "mass_addresses":
        address = _row_value(row, column_map.get("address"))
        count = _to_int(_row_value(row, column_map.get("registered_entities_count")) or "")
        if not address or count is None:
            return None
        return {
            "address": address,
            "registered_entities_count": count,
            "fns_inclusion_date": _row_value(row, column_map.get("fns_inclusion_date")),
        }
    if registry == "mass_directors":
        inn = _row_value(row, column_map.get("director_inn"))
        count = _to_int(_row_value(row, column_map.get("companies_count")) or "")
        if not inn or count is None:
            return None
        return {
            "director_inn": inn,
            "full_name": _row_value(row, column_map.get("full_name")),
            "companies_count": count,
            "fns_inclusion_date": _row_value(row, column_map.get("fns_inclusion_date")),
        }
    if registry == "disqualified":
        person_inn = _row_value(row, column_map.get("person_inn"))
        full_name = _row_value(row, column_map.get("full_name"))
        if not person_inn and not full_name:
            return None
        return {
            "person_inn": person_inn,
            "full_name": full_name,
            "disqualification_date": _row_value(row, column_map.get("disqualification_date")),
            "disqualification_until": _row_value(row, column_map.get("disqualification_until")),
            "reason": _row_value(row, column_map.get("reason")),
        }
    if registry == "disqualified_entities":
        inn = _row_value(row, column_map.get("company_inn"))
        if not inn:
            return None
        return {
            "company_inn": inn,
            "company_ogrn": _row_value(row, column_map.get("company_ogrn")),
            "company_name": _row_value(row, column_map.get("company_name")),
            "company_kpp": _row_value(row, column_map.get("company_kpp")),
            "address_raw": _row_value(row, column_map.get("address_raw")),
        }
    raise SystemExit(f"Неизвестный реестр: {registry}")


# --- разбор набора debtam (XML в архиве) ---


def _to_float(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return float(value.replace(",", ".").strip())
    except ValueError:
        return 0.0


def iter_debtam_records(archive: Path) -> Iterator[dict[str, Any]]:
    """Прочитать набор `debtam`: архив с сотнями XML-файлов ФНС.

    Формат (ТипИнф="ОТКРДАННЫЕ6")::

        <Документ ДатаДок="25.07.2026" ДатаСост="01.07.2026">
          <СведНП НаимОрг="ООО ..." ИННЮЛ="5506193814"/>
          <СведНедоим НаимНалог="Страховые взносы" СумНедНалог="14190.00"
                      СумПени="0.00" СумШтраф="0.00" ОбщСумНедоим="14190.00"/>
        </Документ>

    Одна организация может встречаться несколькими документами — суммируем.
    """
    with zipfile.ZipFile(archive) as zf:
        for name in zf.namelist():
            if not name.lower().endswith(".xml"):
                continue
            payload = zf.read(name)
            try:
                root = ET.parse(io.BytesIO(payload)).getroot()  # noqa: S314 — файл ФНС, не пользовательский ввод
            except ET.ParseError as exc:
                logger.warning("debtam: файл %s не разобрался (%s), пропускаю", name, exc)
                continue
            for doc in root.iter("Документ"):
                np = doc.find("СведНП")
                if np is None:
                    continue
                inn = (np.get("ИННЮЛ") or np.get("ИННФЛ") or "").strip()
                if not inn:
                    continue
                taxes: list[dict[str, Any]] = []
                total = penalty = fine = 0.0
                for item in doc.findall("СведНедоим"):
                    debt = _to_float(item.get("СумНедНалог"))
                    pen = _to_float(item.get("СумПени"))
                    fin = _to_float(item.get("СумШтраф"))
                    common = _to_float(item.get("ОбщСумНедоим")) or (debt + pen + fin)
                    total += common
                    penalty += pen
                    fine += fin
                    taxes.append(
                        {
                            "name": item.get("НаимНалог"),
                            "debt": debt,
                            "penalty": pen,
                            "fine": fin,
                        }
                    )
                if not taxes:
                    continue
                yield {
                    "inn": inn,
                    "org_name": np.get("НаимОрг"),
                    "total_debt_rub": round(total, 2),
                    "penalty_rub": round(penalty, 2),
                    "fine_rub": round(fine, 2),
                    "taxes": taxes,
                    "report_date": _iso_date(doc.get("ДатаСост") or doc.get("ДатаДок")),
                }


def _iso_date(value: str | None) -> str | None:
    """ДД.ММ.ГГГГ → ГГГГ-ММ-ДД."""
    if not value:
        return None
    parts = value.split(".")
    if len(parts) != 3:
        return None
    try:
        return date(int(parts[2]), int(parts[1]), int(parts[0])).isoformat()
    except ValueError:
        return None


async def commit_tax_debts(
    archive: Path,
    db_path: Path,
    *,
    source_label: str,
    data_as_of: date | None,
    batch_size: int = 5000,
) -> int:
    """Загрузить набор `debtam` в таблицу `tax_debts` пачками."""
    store = RegistryStore(db_path)
    await store.init()
    batch: list[dict[str, Any]] = []
    merged: dict[str, dict[str, Any]] = {}
    written = 0

    for record in iter_debtam_records(archive):
        inn = record["inn"]
        existing = merged.get(inn)
        if existing is None:
            merged[inn] = record
        else:
            existing["total_debt_rub"] = round(
                existing["total_debt_rub"] + record["total_debt_rub"], 2
            )
            existing["penalty_rub"] = round(existing["penalty_rub"] + record["penalty_rub"], 2)
            existing["fine_rub"] = round(existing["fine_rub"] + record["fine_rub"], 2)
            existing["taxes"].extend(record["taxes"])

        if len(merged) >= batch_size * 4:
            batch = list(merged.values())
            written += await store.upsert_tax_debts(batch)
            merged.clear()

    if merged:
        written += await store.upsert_tax_debts(list(merged.values()))

    await store.record_load(
        "tax_debts",
        rows=await store.count_rows("tax_debts"),
        data_as_of=data_as_of.isoformat() if data_as_of else None,
        source_url=source_label,
    )
    return written


async def commit_records(
    registry: str,
    records: list[dict[str, Any]],
    db_path: Path,
    *,
    source_label: str,
    data_as_of: date | None = None,
) -> int:
    """Записать собранные records в RegistryStore и обновить служебные отметки."""
    store = RegistryStore(db_path)
    await store.init()
    if registry == "mass_addresses":
        n = await store.upsert_mass_addresses(records)
    elif registry == "mass_directors":
        n = await store.upsert_mass_directors(records)
    elif registry == "disqualified":
        n = await store.upsert_disqualified(records)
    elif registry == "disqualified_entities":
        n = await store.upsert_disqualified_entities(records)
    else:
        raise SystemExit(f"Неизвестный реестр: {registry}")
    await store.set_meta(
        f"{registry}.last_etl",
        datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
    )
    await store.set_meta(f"{registry}.last_etl_source", source_label)
    await store.set_meta(f"{registry}.last_etl_count", str(n))
    await store.record_load(
        registry,
        rows=await store.count_rows(registry),
        data_as_of=data_as_of.isoformat() if data_as_of else None,
        source_url=source_label,
    )
    return n


async def print_status(db_path: Path) -> int:
    """Показать состояние всех реестров: объём, дата данных, свежесть.

    Код возврата 1, если хотя бы один реестр пуст или устарел — по нему
    расписание может поднять тревогу. Проверки в это время отвечают
    «не проверено», а не «нарушений не найдено».
    """
    store = RegistryStore(db_path)
    await store.init()
    print(f"База реестров: {db_path}")
    print(f"{'реестр':<24}{'строк':>10}  {'данные ФНС':<12}{'возраст':>9}  состояние")
    unusable = 0
    for registry in ("mass_addresses", "mass_directors", "disqualified_entities", "tax_debts"):
        st = await store.registry_status(registry)
        if st.is_training_sample:
            state = "учебный набор — проверка отвечает «не проверено»"
            unusable += 1
        elif st.is_empty:
            state = "не загружен — проверка отвечает «не проверено»"
            unusable += 1
        elif st.is_stale:
            state = f"устарел (порог {st.stale_after_days} дн.) — «не проверено»"
            unusable += 1
        else:
            state = "актуален"
        age = f"{st.age_days} дн." if st.age_days is not None else "—"
        print(f"{registry:<24}{st.rows:>10}  {st.data_as_of_ru:<12}{age:>9}  {state}")
    if unusable:
        print(f"\nНепригодных к проверке реестров: {unusable}. Нужна загрузка (--all --download --commit).")
        return 1
    return 0


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "ETL открытых данных ФНС → локальный SQLite реестров atomno-mcp-fns-check. "
            "По умолчанию предпросмотр; для записи в БД нужен --commit."
        ),
    )
    p.add_argument(
        "--registry",
        choices=(
            "mass_addresses",
            "mass_directors",
            "disqualified",
            "disqualified_entities",
            "tax_debts",
        ),
        help="Какой реестр загружать. Не нужен при --all / --status.",
    )
    p.add_argument(
        "--dataset",
        choices=tuple(DATASETS_BY_SLUG),
        help="Набор ФНС (нужен при --download, чтобы знать раскладку колонок).",
    )
    p.add_argument("--source", help="Путь к уже скачанному файлу открытых данных.")
    p.add_argument(
        "--download",
        action="store_true",
        default=False,
        help="Найти свежий файл на сайте ФНС и скачать его перед загрузкой.",
    )
    p.add_argument(
        "--all",
        action="store_true",
        default=False,
        help="Обработать все наборы ФНС подряд (обычно вместе с --download --commit).",
    )
    p.add_argument(
        "--status",
        action="store_true",
        default=False,
        help="Показать состояние реестров (объём, дата данных, свежесть) и выйти.",
    )
    p.add_argument(
        "--db",
        default=os.environ.get(
            "MCP_FNS_REGISTRIES_DB",
            str(Path.cwd() / "atomno_mcp_fns_check_cache.registries.sqlite"),
        ),
        help="Путь к SQLite-файлу реестров (по умолчанию из MCP_FNS_REGISTRIES_DB).",
    )
    p.add_argument(
        "--cache-dir",
        default=os.environ.get("MCP_FNS_OPENDATA_DIR", str(Path.cwd() / "fns_open_data")),
        help="Куда складывать скачанные файлы ФНС.",
    )
    p.add_argument(
        "--columns",
        default=None,
        help=(
            "Явный mapping колонок: 'key=col,key=col'. "
            "Нужен только для ручных выгрузок с человеческими заголовками."
        ),
    )
    p.add_argument("--delimiter", default=";", help="Разделитель CSV (по умолчанию ';').")
    p.add_argument(
        "--encoding",
        default=None,
        help="Кодировка CSV. По умолчанию определяется автоматически (utf-8 / windows-1251).",
    )
    p.add_argument(
        "--limit", type=int, default=None, help="Ограничить число обрабатываемых строк."
    )
    p.add_argument(
        "--commit",
        action="store_true",
        default=False,
        help="Записать в БД. Без флага — предпросмотр (только разбор и статистика).",
    )
    return p


def _process_dataset(spec: DatasetSpec, args: argparse.Namespace) -> int:
    """Обработать один набор ФНС. Возвращает число записанных строк."""
    db = Path(args.db).expanduser().resolve()
    db.parent.mkdir(parents=True, exist_ok=True)
    data_as_of: date | None = None
    source_label: str

    if args.source and not args.all:
        source = Path(args.source).expanduser().resolve()
        if not source.is_file():
            raise SystemExit(f"Исходный файл не найден: {source}")
        source_label = str(source)
    else:
        url, data_as_of = resolve_latest_file(spec.slug)
        source_label = url
        cache_dir = Path(args.cache_dir).expanduser().resolve()
        source = cache_dir / f"{spec.slug}{Path(url).suffix}"
        if source.is_file():
            logger.info("%s: файл уже скачан (%s)", spec.slug, source)
        else:
            logger.info("%s: качаю %s", spec.slug, url)
            download_file(url, source)
        logger.info(
            "%s: файл %s, %.1f МБ, данные ФНС от %s",
            spec.slug,
            source.name,
            source.stat().st_size / 1024 / 1024,
            data_as_of.isoformat(),
        )

    if spec.kind == "debtam_zip":
        if not args.commit:
            sample = []
            for record in iter_debtam_records(source):
                sample.append(record)
                if len(sample) >= 3:
                    break
            print(f"ПРЕДПРОСМОТР {spec.slug}: первые записи")
            for rec in sample:
                print(f"  {rec['inn']} {rec['org_name']} → {rec['total_debt_rub']} ₽")
            return 0
        written = asyncio.run(
            commit_tax_debts(
                source, db, source_label=source_label, data_as_of=data_as_of
            )
        )
        print(f"ЗАПИСАНО {spec.registry}: {written} организаций (источник {source_label})")
        return written

    records = build_records(
        spec.registry,
        source,
        columns=_parse_columns_arg(args.columns),
        delimiter=args.delimiter,
        encoding=args.encoding,
        limit=args.limit,
        fns_slug=spec.slug,
    )
    if not records:
        raise SystemExit(f"{spec.slug}: 0 записей разобрано — проверьте формат файла.")

    print(f"{spec.slug}: разобрано {len(records)} записей из {source.name}")
    for rec in records[: min(3, len(records))]:
        print(f"  {rec}")

    if not args.commit:
        print("ПРЕДПРОСМОТР: запись в базу не выполнялась (для записи передайте --commit).")
        return 0

    n = asyncio.run(
        commit_records(
            spec.registry, records, db, source_label=source_label, data_as_of=data_as_of
        )
    )
    print(f"ЗАПИСАНО {spec.registry}: {n} строк (источник {source_label})")
    return n


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _build_arg_parser().parse_args(argv)

    db = Path(args.db).expanduser().resolve()

    if args.status:
        return asyncio.run(print_status(db))

    if args.all:
        started = datetime.now(tz=timezone.utc)
        total = 0
        for spec in DATASETS:
            print(f"\n=== {spec.slug} → {spec.registry} ({spec.title_ru}) ===")
            total += _process_dataset(spec, args)
        elapsed = (datetime.now(tz=timezone.utc) - started).total_seconds()
        print(f"\nИТОГО записано строк: {total}; заняло {elapsed:.0f} с")
        # Печатаем состояние, но не валим успешную загрузку из-за устаревших
        # наборов: ФНС по части реестров не публикует свежие файлы с 2021 года.
        asyncio.run(print_status(db))
        return 0

    if not args.registry and not args.dataset:
        print("ERROR: укажите --registry/--dataset, либо --all, либо --status", file=sys.stderr)
        return 2

    if args.dataset:
        spec = DATASETS_BY_SLUG[args.dataset]
    else:
        matching = [d for d in DATASETS if d.registry == args.registry]
        if not matching:
            print(f"ERROR: для реестра {args.registry} нет набора ФНС", file=sys.stderr)
            return 2
        spec = matching[0]

    if not args.source and not args.download:
        print(
            "ERROR: нужен либо --source <файл>, либо --download (скачать свежий файл ФНС)",
            file=sys.stderr,
        )
        return 2

    _process_dataset(spec, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
