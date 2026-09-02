"""Обязательная тройка в ответах про человека: источник, дата, однофамилец."""

from __future__ import annotations

from datetime import datetime, timezone

HOMONYM_WARNING_RU = (
    "Совпадение по имени не доказывает, что это тот самый человек "
    "(возможен однофамилец). Нужна проверка по документам."
)

REGISTRY_DATE_UNPUBLISHED_RU = "Реестр свою дату обновления не публикует."

EFRSB_REGISTRY_RU = (
    "Единый федеральный реестр сведений о банкротстве (ЕФРСБ)"
)
EFRSB_URL = "https://bankrot.fedresurs.ru"

_MONTHS_RU = {
    1: "января",
    2: "февраля",
    3: "марта",
    4: "апреля",
    5: "мая",
    6: "июня",
    7: "июля",
    8: "августа",
    9: "сентября",
    10: "октября",
    11: "ноября",
    12: "декабря",
}


def format_queried_at_ru(when: datetime | str | None) -> str:
    if when is None:
        return "время запроса не сохранено"
    if isinstance(when, str):
        text = when.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return when
    else:
        dt = when
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return f"{dt.day} {_MONTHS_RU[dt.month]} {dt.year}, {dt:%H:%M} UTC"


def build_notice_ru(
    *,
    source_registry_ru: str,
    source_url: str,
    queried_at: datetime | str | None,
    data_as_of: str | None,
    has_match: bool,
    registry_date_note: str | None = None,
) -> str:
    when = format_queried_at_ru(queried_at)
    if data_as_of:
        registry_part = f"Данные перечня на {data_as_of}."
    elif registry_date_note:
        registry_part = f"Даты перечней: {registry_date_note}."
    else:
        registry_part = REGISTRY_DATE_UNPUBLISHED_RU
    text = (
        f"Запрошено в реестре «{source_registry_ru}» {when} "
        f"({source_url}). {registry_part}"
    )
    if has_match:
        text = f"{text} {HOMONYM_WARNING_RU}"
    return text
