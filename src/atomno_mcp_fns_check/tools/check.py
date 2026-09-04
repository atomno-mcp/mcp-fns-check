"""High-level бизнес-логика тулзов check_inn / check_ogrn / get_okveds.

Каждая функция:
    1. Валидирует вход.
    2. Смотрит SQLite-кэш (источник `egrul_hit`).
    3. Если miss — запрашивает ЕГРЮЛ.
    4. Кладёт сырой хит в кэш на TTL ServiceContext.cache_ttl_hours.
    5. Преобразует хит в публичную модель (CounterpartyCard / OkvedReport).
"""

from __future__ import annotations

from dataclasses import asdict, replace
from typing import Literal

from ..context import ServiceContext
from ..db.cache import SQLiteCache
from ..errors import McpFnsError, NotFoundError, ValidationError
from ..schemas import CounterpartyCard, OkvedReport
from ..sources.egrul import EgrulSearchHit
from ..validators import is_valid_inn, is_valid_ogrn
from .cards import hit_to_card
from .okveds import build_okved_report

CACHE_SOURCE = "egrul_hit"


def _hit_from_cache_payload(payload: dict) -> EgrulSearchHit:
    return EgrulSearchHit(
        inn=payload.get("inn", ""),
        ogrn=payload.get("ogrn", ""),
        kpp=payload.get("kpp"),
        name_full=payload.get("name_full"),
        name_short=payload.get("name_short"),
        address=payload.get("address"),
        okved_code=payload.get("okved_code"),
        okved_name=payload.get("okved_name"),
        director_name=payload.get("director_name"),
        director_position=payload.get("director_position"),
        registration_date=payload.get("registration_date"),
        liquidation_date=payload.get("liquidation_date"),
        is_individual=bool(payload.get("is_individual", False)),
        raw=payload.get("raw") or {},
    )


async def _fetch_hit(
    ctx: ServiceContext,
    *,
    by: Literal["inn", "ogrn"],
    value: str,
) -> tuple[EgrulSearchHit, float | None]:
    """Получить хит из кэша или из ЕГРЮЛ. Возвращает (hit, cache_age_hours_or_None)."""
    key = SQLiteCache.make_key(**{by: value})
    cached = await ctx.cache.get(key, CACHE_SOURCE)
    if cached is not None and not cached.is_expired:
        return _hit_from_cache_payload(cached.payload), cached.age_hours

    if by == "inn":
        hits = await ctx.egrul.search_by_inn(value)
    else:
        hits = await ctx.egrul.search_by_ogrn(value)

    if not hits:
        raise NotFoundError(
            "Контрагент не найден в ЕГРЮЛ/ЕГРИП.",
            details={by: value},
        )

    hit = hits[0]
    payload = asdict(hit)
    await ctx.cache.put(key, CACHE_SOURCE, payload, ttl_hours=ctx.cache_ttl_hours)
    return hit, 0.0


async def check_inn(ctx: ServiceContext, inn: str, *, include_extended: bool = False) -> CounterpartyCard:
    """Полная карточка по ИНН. Параметр include_extended на S2 не используется (S2.x)."""
    if not is_valid_inn(inn):
        raise ValidationError(f"Невалидный ИНН '{inn}'", details={"input": inn})
    hit, age = await _fetch_hit(ctx, by="inn", value=inn)
    return hit_to_card(hit, cache_age_hours=age)


async def check_ogrn(ctx: ServiceContext, ogrn: str, *, include_extended: bool = False) -> CounterpartyCard:
    """Полная карточка по ОГРН/ОГРНИП."""
    if not is_valid_ogrn(ogrn):
        raise ValidationError(f"Невалидный ОГРН '{ogrn}'", details={"input": ogrn})
    hit, age = await _fetch_hit(ctx, by="ogrn", value=ogrn)
    return hit_to_card(hit, cache_age_hours=age)


async def get_okveds(
    ctx: ServiceContext,
    *,
    inn: str | None = None,
    ogrn: str | None = None,
    include_history: bool = False,
) -> OkvedReport:
    """Расшифровка ОКВЭД основной + дополнительные.

    Если поиск ЕГРЮЛ не отдал код, пробуем карточку «Прозрачного бизнеса».
    Пустой список без `warnings` не возвращаем: отсутствие кода в выдаче
    поиска неотличимо от «у компании нет видов деятельности».
    """
    if not inn and not ogrn:
        raise ValidationError("Нужно передать хотя бы один из 'inn' или 'ogrn'.")
    if inn is not None and not is_valid_inn(inn):
        raise ValidationError(f"Невалидный ИНН '{inn}'", details={"input": inn})
    if ogrn is not None and not is_valid_ogrn(ogrn):
        raise ValidationError(f"Невалидный ОГРН '{ogrn}'", details={"input": ogrn})

    by: Literal["inn", "ogrn"] = "inn" if inn else "ogrn"
    value = inn or ogrn
    assert value is not None
    hit, _ = await _fetch_hit(ctx, by=by, value=value)
    warnings: list[str] = []
    if include_history:
        warnings.append(
            "История изменений ОКВЭД в этом режиме недоступна: поиск ЕГРЮЛ "
            "отдаёт только текущий основной код, если отдаёт его вообще."
        )

    if not hit.okved_code and ctx.pb_fns is not None and hit.inn:
        try:
            card = await ctx.pb_fns.get_company_card(hit.inn)
        except McpFnsError as exc:
            warnings.append(
                "Запасной запрос в «Прозрачный бизнес» не удался: "
                f"{exc.message_ru}"
            )
        else:
            if card is not None and card.okved:
                hit = replace(
                    hit,
                    okved_code=card.okved,
                    okved_name=card.okved_name or hit.okved_name,
                )
            elif card is None:
                warnings.append(
                    "«Прозрачный бизнес» не нашёл карточку по этому ИНН — "
                    "коды ОКВЭД из этого источника взять не удалось."
                )
            else:
                warnings.append(
                    "Карточка «Прозрачного бизнеса» есть, но кода ОКВЭД в ней нет."
                )

    if not hit.okved_code:
        warnings.append(
            "Источник не вернул код ОКВЭД в карточке поиска. Пустой список — "
            "не доказательство, что видов деятельности нет: у действующей "
            "организации они почти всегда есть."
        )
    return build_okved_report(hit, additional_codes=None, warnings=warnings)
