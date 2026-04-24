"""get_directors_history — история смены руководителей и учредителей.

На S3 публичный egrul.nalog.ru исторических slice'ов не отдаёт: оттуда мы
получаем только текущего директора (поля `director_name`, `director_position`)
и текущего учредителя — только если он попал в карточку.

Полная история (с датами назначения / увольнения) лежит в Open Data ЕГРЮЛ
(форма 11-EGRUL, ежемесячные slice'ы с 2014-09 по сегодня), её мы будем
загружать ETL-job'ом на стадии R-5 / S5 (см. SPEC §4.4 и roadmap.json).

Поэтому на S3 функция возвращает:
  * один текущий период по директору (если данные есть в ЕГРЮЛ-хите),
  * пустую историю учредителей (egrul.nalog.ru их в search-result не отдаёт),
  * `data_completeness_warning` с явной отметкой неполноты.

Это полностью соответствует SPEC §3.3 («предупреждать о неполноте данных
для записей до 2020 года») и не вводит пользователя в заблуждение.
"""

from __future__ import annotations

from ..context import ServiceContext
from ..errors import ValidationError
from ..schemas import DirectorChange, DirectorsHistoryReport
from ..validators import is_valid_inn
from .check import _fetch_hit  # noqa: PLC2701  (внутреннее переиспользование)


async def get_directors_history(
    ctx: ServiceContext,
    inn: str,
    *,
    period: str | None = None,
) -> DirectorsHistoryReport:
    """Получить историю смены руководителей и учредителей по ИНН.

    На стадии S3 возвращает только текущий период по директору +
    предупреждение о неполноте. Параметр `period` зарезервирован под S5.
    """
    if not is_valid_inn(inn):
        raise ValidationError(f"Невалидный ИНН '{inn}'", details={"input": inn})

    hit, _ = await _fetch_hit(ctx, by="inn", value=inn)

    directors: list[DirectorChange] = []
    if hit.director_name or hit.director_position:
        directors.append(
            DirectorChange(
                full_name=hit.director_name,
                position=hit.director_position,
                is_current=True,
            )
        )

    return DirectorsHistoryReport(
        inn=inn,
        directors_history=directors,
        founders_history=[],
        total_director_changes=len(directors),
        total_founder_changes=0,
        data_completeness_warning=(
            "На стадии MVP (S3) возвращается только текущий руководитель из "
            "egrul.nalog.ru. Полная история (с датами назначения и увольнения) "
            "будет доступна после загрузки Open Data slice ЕГРЮЛ — стадия S5 "
            "(см. roadmap.json R-5)."
        ),
        sources_used=["egrul"],
    )
