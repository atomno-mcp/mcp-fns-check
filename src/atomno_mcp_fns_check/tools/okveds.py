"""Тул get_okveds — расшифровка кодов ОКВЭД-2.

На S2 справочник `data/okved_2_dict.json` — частичный bundled-сидинг (~50 кодов
из самых частых разделов и популярных групп). Полная выгрузка ~2500 кодов —
отдельная задача S2.x.

Алгоритм lookup:
  1. Точное совпадение `code` в `codes` справочника.
  2. Каскадный fallback: '64.19.5' → '64.19' → '64' (отрезаем по точкам / двум первым цифрам).
     Имя берётся от ближайшего родителя, но `code` сохраняется исходный.
  3. Раздел (буква) определяется через диапазоны top-2 (см. cards._okved_section_letter).
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from typing import Any

from ..schemas import OkvedInfo, OkvedReport
from ..sources.egrul import EgrulSearchHit
from .cards import _okved_section_letter


@lru_cache(maxsize=1)
def _load_dict() -> dict[str, Any]:
    """Прочитать встроенный JSON-справочник ОКВЭД-2 (single-load via lru_cache)."""
    with resources.files("atomno_mcp_fns_check.data").joinpath("okved_2_dict.json").open(
        encoding="utf-8"
    ) as f:
        return json.load(f)


def _is_licensable(code: str, dictionary: dict[str, Any] | None = None) -> tuple[bool, str | None]:
    d = dictionary or _load_dict()
    prefixes: list[str] = d.get("licensable_prefixes", [])
    authorities: dict[str, str] = d.get("license_authorities", {})

    matched = next((p for p in prefixes if code.startswith(p)), None)
    if matched is None:
        return False, None
    return True, authorities.get(matched) or authorities.get(matched[:2])


def lookup_okved(code: str) -> OkvedInfo:
    """Расшифровать один код ОКВЭД с каскадным fallback."""
    code = code.strip()
    d = _load_dict()
    codes: dict[str, str] = d.get("codes", {})
    sections: dict[str, str] = d.get("sections", {})

    name = codes.get(code)

    if name is None:
        candidate = code
        while "." in candidate:
            candidate = candidate.rsplit(".", 1)[0]
            if candidate in codes:
                name = codes[candidate]
                break
        if name is None and len(code) >= 2:
            name = codes.get(code[:2])

    section_letter = _okved_section_letter(code)
    section_name = sections.get(section_letter) if section_letter else None
    is_lic, authority = _is_licensable(code, d)

    return OkvedInfo(
        code=code,
        name=name,
        section=section_letter,
        section_name=section_name,
        is_licensable=is_lic,
        license_authority=authority,
    )


def build_okved_report(
    hit: EgrulSearchHit,
    *,
    additional_codes: list[str] | None = None,
) -> OkvedReport:
    """Собрать ответ для get_okveds.

    На S2 у нас есть только основной ОКВЭД из EgrulSearchHit. Дополнительные
    коды могут быть переданы извне (заглушка для будущего источника на S2.x,
    когда подключим парсинг полной выписки PDF/JSON).
    """
    main: OkvedInfo | None = None
    if hit.okved_code:
        main = lookup_okved(hit.okved_code)
        if not main.name and hit.okved_name:
            main = main.model_copy(update={"name": hit.okved_name})

    additional: list[OkvedInfo] = []
    seen: set[str] = {main.code} if main else set()
    for code in additional_codes or []:
        if code in seen:
            continue
        additional.append(lookup_okved(code))
        seen.add(code)

    total = (1 if main else 0) + len(additional)

    return OkvedReport(
        inn=hit.inn or None,
        ogrn=hit.ogrn or None,
        main_okved=main,
        additional_okveds=additional,
        total_count=total,
        history=None,
    )
