"""ETL-скрипт обновления локальных реестров atomno-mcp-fns-check из Open Data ФНС.

Загружает CSV-выгрузки трёх реестров и складывает в локальный SQLite
(`RegistryStore`). По умолчанию работает в режиме `--dry-run` — только
парсит входной файл и печатает статистику. Для записи в БД нужен
явный флаг `--commit`.

Поддерживаемые реестры (`--registry`):

  * `mass_addresses`  — Сведения об адресах массовой регистрации
                        юридических лиц (Open Data ФНС, набор 11-MA / "ulm").
                        Источник:
                        https://www.nalog.gov.ru/opendata/7707329152-massreg/
  * `mass_directors`  — Сведения о лицах, являющихся руководителями /
                        учредителями нескольких юридических лиц
                        (Open Data, "ulm-mass" / 11-MD).
                        https://www.nalog.gov.ru/opendata/7707329152-massuchredt/
  * `disqualified`    — Реестр дисквалифицированных лиц (открытые данные).
                        https://service.nalog.ru/disqualified.do

Поскольку колонки CSV в Open Data ФНС менялись (формат 1.0 → 1.2 → 1.3),
скрипт принимает явный mapping колонок через `--columns key=column,...`,
либо угадывает по эвристикам (русским именам).

Это однопоточный CLI, не сервис — ETL по cron планируется отдельно
(в SPEC §3.2: «mcp-fns-etl: раз в неделю обновляет Open Data реестры»).

Запуск:

    python -m atomno_mcp_fns_check.scripts.etl_registries \\
        --registry mass_addresses \\
        --source ./fns_open_data/ulm.csv \\
        --commit

    python -m atomno_mcp_fns_check.scripts.etl_registries \\
        --registry mass_directors \\
        --source ./fns_open_data/uchredt.csv \\
        --columns inn=ИНН,full_name=ФИО,companies_count=КолВоОрг \\
        --dry-run

Никогда не делает write-операций, кроме как в локальный SQLite.
Никаких сетевых запросов из коробки — `--source` принимает локальный путь.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import os
import sys
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from atomno_mcp_fns_check.db.registries import RegistryStore

logger = logging.getLogger("atomno_mcp_fns_check.etl")

# Эвристики угадывания колонок (по lower-case/тримминг русских названий из 11-MA / 11-MD).
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
}


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


def build_records(
    registry: str,
    source: Path,
    *,
    columns: dict[str, str] | None = None,
    delimiter: str = ";",
    encoding: str = "utf-8-sig",
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Собрать список словарей-записей под формат `RegistryStore` из CSV.

    Публичная функция — используется и из CLI, и из тестов.
    """
    records: list[dict[str, Any]] = []
    column_map: dict[str, int] | None = None

    for headers, row in _iter_rows(source, delimiter=delimiter, encoding=encoding):
        if column_map is None:
            column_map = _build_column_map(registry, headers, columns)
            if not column_map:
                raise SystemExit(
                    f"Не удалось определить колонки для реестра '{registry}'. "
                    f"Headers: {headers}. Передайте --columns явно."
                )
        if registry == "mass_addresses":
            address = _row_value(row, column_map.get("address"))
            count = _to_int(_row_value(row, column_map.get("registered_entities_count")) or "")
            if not address or count is None:
                continue
            records.append(
                {
                    "address": address,
                    "registered_entities_count": count,
                    "fns_inclusion_date": _row_value(
                        row, column_map.get("fns_inclusion_date")
                    ),
                }
            )
        elif registry == "mass_directors":
            inn = _row_value(row, column_map.get("director_inn"))
            full_name = _row_value(row, column_map.get("full_name"))
            count = _to_int(_row_value(row, column_map.get("companies_count")) or "")
            if not inn or count is None:
                continue
            records.append(
                {
                    "director_inn": inn,
                    "full_name": full_name,
                    "companies_count": count,
                    "fns_inclusion_date": _row_value(
                        row, column_map.get("fns_inclusion_date")
                    ),
                }
            )
        elif registry == "disqualified":
            person_inn = _row_value(row, column_map.get("person_inn"))
            full_name = _row_value(row, column_map.get("full_name"))
            if not person_inn and not full_name:
                continue
            records.append(
                {
                    "person_inn": person_inn,
                    "full_name": full_name,
                    "disqualification_date": _row_value(
                        row, column_map.get("disqualification_date")
                    ),
                    "disqualification_until": _row_value(
                        row, column_map.get("disqualification_until")
                    ),
                    "reason": _row_value(row, column_map.get("reason")),
                }
            )
        else:
            raise SystemExit(f"Неизвестный реестр: {registry}")

        if limit is not None and len(records) >= limit:
            break

    return records


async def commit_records(
    registry: str, records: list[dict[str, Any]], db_path: Path, *, source_label: str
) -> int:
    """Записать собранные records в RegistryStore и обновить meta."""
    store = RegistryStore(db_path)
    await store.init()
    if registry == "mass_addresses":
        n = await store.upsert_mass_addresses(records)
    elif registry == "mass_directors":
        n = await store.upsert_mass_directors(records)
    elif registry == "disqualified":
        n = await store.upsert_disqualified(records)
    else:
        raise SystemExit(f"Неизвестный реестр: {registry}")
    await store.set_meta(
        f"{registry}.last_etl",
        datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
    )
    await store.set_meta(f"{registry}.last_etl_source", source_label)
    await store.set_meta(f"{registry}.last_etl_count", str(n))
    return n


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "ETL Open Data ФНС → локальный SQLite реестров atomno-mcp-fns-check. "
            "По умолчанию dry-run; для записи в БД нужен --commit."
        ),
    )
    p.add_argument(
        "--registry",
        required=True,
        choices=("mass_addresses", "mass_directors", "disqualified"),
    )
    p.add_argument("--source", required=True, help="Путь к CSV-файлу Open Data.")
    p.add_argument(
        "--db",
        default=os.environ.get(
            "MCP_FNS_REGISTRIES_DB",
            str(Path.cwd() / "atomno_mcp_fns_check_cache.registries.sqlite"),
        ),
        help="Путь к SQLite-файлу registries (по умолчанию из MCP_FNS_REGISTRIES_DB).",
    )
    p.add_argument(
        "--columns",
        default=None,
        help=(
            "Явный mapping колонок: 'key=col,key=col'. "
            "Если не задан — используется эвристика по русским заголовкам."
        ),
    )
    p.add_argument("--delimiter", default=";", help="Разделитель CSV (по умолчанию ';').")
    p.add_argument("--encoding", default="utf-8-sig", help="Кодировка CSV.")
    p.add_argument(
        "--limit", type=int, default=None, help="Ограничить число обрабатываемых строк."
    )
    p.add_argument(
        "--commit",
        action="store_true",
        default=False,
        help="Записать в БД. Без флага — dry-run (только парсинг и статистика).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _build_arg_parser().parse_args(argv)

    source = Path(args.source).expanduser().resolve()
    if not source.is_file():
        print(f"ERROR: исходный файл не найден: {source}", file=sys.stderr)
        return 2

    columns = _parse_columns_arg(args.columns)

    logger.info(
        "ETL %s: source=%s, delimiter=%r, encoding=%s, limit=%s, mode=%s",
        args.registry,
        source,
        args.delimiter,
        args.encoding,
        args.limit,
        "commit" if args.commit else "dry-run",
    )

    records = build_records(
        args.registry,
        source,
        columns=columns,
        delimiter=args.delimiter,
        encoding=args.encoding,
        limit=args.limit,
    )

    if not records:
        print("WARN: 0 записей собрано. Проверьте --columns / --delimiter.", file=sys.stderr)
        return 1

    print(f"OK: распарсено {len(records)} записей из {source.name}")
    sample = records[: min(3, len(records))]
    print(f"Sample (first {len(sample)}):")
    for rec in sample:
        print(f"  {rec}")

    if args.commit:
        db = Path(args.db).expanduser().resolve()
        db.parent.mkdir(parents=True, exist_ok=True)
        n = asyncio.run(
            commit_records(args.registry, records, db, source_label=str(source))
        )
        print(f"COMMIT: записано в {db} → {n} строк (registry={args.registry})")
        return 0

    print("DRY-RUN: запись в БД не выполнялась (для записи передайте --commit).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
