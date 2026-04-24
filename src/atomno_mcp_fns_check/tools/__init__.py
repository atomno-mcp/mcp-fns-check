"""Бизнес-логика MCP-тулзов.

S2: check_inn / check_ogrn / get_legal_status / get_okveds.
S3: get_directors_history, check_for_red_flags (4 из 8 проверок).
S4: enforcement_proceedings, no_reporting, tax_debts, active_lawsuits.
"""

from .cards import hit_to_card, mask_inn, parse_date
from .check import check_inn, check_ogrn, get_okveds
from .contractor import check_contractor
from .directors_history import get_directors_history
from .legal_status import get_legal_status
from .okveds import build_okved_report, lookup_okved
from .red_flags import check_for_red_flags

__all__ = [
    "build_okved_report",
    "check_contractor",
    "check_for_red_flags",
    "check_inn",
    "check_ogrn",
    "get_directors_history",
    "get_legal_status",
    "get_okveds",
    "hit_to_card",
    "lookup_okved",
    "mask_inn",
    "parse_date",
]
