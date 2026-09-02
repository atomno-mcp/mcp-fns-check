"""Заглушки источников в НАСТОЯЩЕМ формате ответа.

Правило: тест не имеет права выдумывать структуру ответа источника. У нас уже
был случай, когда тесты были зелёными, потому что проверяли собственную ошибку
разбора. Поэтому здесь — только форматы, снятые живыми запросами 02.08.2026
(сохранены в `tests/fixtures/`).

  * `service.nalog.ru/disqualified-proc.json` — POST, JSON, поля по-русски;
  * `is-go.fssp.gov.ru/ajax_search` — GET, JSONP `({"data":"<html>", ...})`;
  * `pb.nalog.ru/search-proc.json` — POST в два шага (в проверках риска
    больше не участвует, оставлен для тестов самого клиента).

Здесь же — наполнение локальных реестров так, чтобы они считались свежими:
устаревший реестр по правилу честности даёт «не проверено», и без явной даты
выгрузки любой «чистый» сценарий превратился бы в ошибку.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

import httpx
import respx

from atomno_mcp_fns_check.db.registries import RegistryStore
from atomno_mcp_fns_check.sources.disqualified import DISQ_BASE_URL, DISQ_SEARCH_PATH
from atomno_mcp_fns_check.sources.fssp import FSSP_BASE_URL, FSSP_SEARCH_PATH

DISQ_URL = f"{DISQ_BASE_URL}{DISQ_SEARCH_PATH}"
FSSP_URL = f"{FSSP_BASE_URL}{FSSP_SEARCH_PATH}"

#: Полный объём реестра дисквалифицированных на 02.08.2026 (поле `КолЗап`).
DISQ_REGISTRY_TOTAL = 8563

JSON_HEADERS = {"content-type": "application/json; charset=utf-8"}
JSONP_HEADERS = {"content-type": "text/javascript; charset=utf-8"}


# --- реестр дисквалифицированных лиц ФНС ---


def disqualified_row(
    *,
    company_inn: str,
    full_name: str = "АБАЗЯН МАРИНА АНДРЕЕВНА",
    position: str = "ГЕНЕРАЛЬНЫЙ ДИРЕКТОР",
    company_name: str = 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ГЕРААРТ"',
    date_from: str = "12.08.2025 00:00:00",
    date_until: str = "11.08.2099 00:00:00",
) -> dict[str, Any]:
    """Строка реестра ровно в тех полях, что отдаёт источник."""
    return {
        "ROW_NUM": 1,
        "ДатаФорм": "01.08.2026 22:24:06",
        "КолЗап": DISQ_REGISTRY_TOTAL,
        "НомЗап": "252300073066",
        "ФИО": full_name,
        "ДатаРожд": "17.10.1990 00:00:00",
        "МестоРожд": "АБХАЗСКАЯ АССР ГУДАУТСКИЙ РАЙОН С. ПСЫРЦХА",
        "НаимОрг": company_name,
        "ИННОрг": company_inn,
        "Должность": position,
        "КвалификацияТекст": "Ч.5 СТ. 14.25 КОАП РФ",
        "НаимОргПрот": "МИФНС РОССИИ № 16 ПО КРАСНОДАРСКОМУ КРАЮ",
        "ФИОСуд": "СУШКОВ Е Ю",
        "ДолжностьСуд": "МИРОВОЙ СУДЬЯ",
        "ДисквСрок": "1 г 0 м 0 д",
        "ДатаНачДискв": date_from,
        "ДатаКонДискв": date_until,
        "row_cnt": 1,
    }


def disqualified_payload(rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    rows = rows or []
    return {
        "dtQueryEnd": "02.08.2026 15:41:21",
        "pageCount": 1,
        "dtQueryBegin": "02.08.2026 15:41:20",
        "data": rows,
        "validPageSizes": [10, 25, 50],
        "reportRowLimit": 0,
        "pageSize": 25,
        "queryCount": True,
        "rowLimit": 100000,
        "queryHash": 1360209002,
        "queryTime": 737,
        "rowCount": len(rows),
        "page": 1,
    }


def mock_disqualified_clean(m: respx.MockRouter) -> respx.Route:
    """Реестр ответил: по этой организации записей нет."""
    return m.post(DISQ_URL).mock(
        return_value=httpx.Response(200, json=disqualified_payload(), headers=JSON_HEADERS)
    )


def mock_disqualified_hit(m: respx.MockRouter, **row_kwargs) -> respx.Route:
    """Реестр ответил: в управлении организации дисквалифицированное лицо."""
    return m.post(DISQ_URL).mock(
        return_value=httpx.Response(
            200,
            json=disqualified_payload([disqualified_row(**row_kwargs)]),
            headers=JSON_HEADERS,
        )
    )


def mock_disqualified_unavailable(m: respx.MockRouter, status: int = 503) -> respx.Route:
    return m.post(DISQ_URL).mock(return_value=httpx.Response(status, text=""))


# --- Банк данных исполнительных производств ФССП ---


def _jsonp(data_html: str, *, err: str = "", e: str = "") -> str:
    return "(" + json.dumps({"data": data_html, "err": err, "e": e}, ensure_ascii=False) + ");"


FSSP_EMPTY_HTML = (
    '<div class="iss"><div class="results">'
    '<div class="empty">По вашему запросу ничего не найдено</div>'
    "</div></div>"
)

FSSP_CAPTCHA_HTML = (
    '<div id="captcha-popup" data-ajax="false"><div class="head"></div>'
    '<form class="body content" method="get" id="ncapcha" '
    'action="https://is-go.fssp.gov.ru">'
    "<h2>Введите код с картинки:</h2>"
    '<div class="context"><img src="data:image/png;base64,iVBORw0KGgo="></div>'
    "</form></div>"
)

FSSP_INTERNAL_ERROR_HTML = (
    '<div class="iss"><div class="results" style="margin:10px 0;">'
    '<div class="empty">Внутренняя ошибка</div>'
    "</div></div>"
)


def fssp_result_html(cases: list[dict[str, Any]]) -> str:
    """Таблица результатов ФССП в разметке формы `iss`."""
    rows = [
        "<tr><th>№ ИП</th><th>Должник</th><th>Предмет исполнения, сумма</th></tr>"
    ]
    for case in cases:
        rows.append(
            "<tr>"
            f"<td>{case['number']} от {case.get('date', '01.02.2024')}</td>"
            f"<td>{case.get('debtor', 'ООО «РОМАШКА», ИНН 7707083893, г. Москва')}</td>"
            f"<td>Иные взыскания имущественного характера: "
            f"{case.get('amount_text', '0,00')} руб.</td>"
            "</tr>"
        )
    return '<div class="iss"><table class="results"><tbody>' + "".join(rows) + "</tbody></table></div>"


def mock_fssp_clean(m: respx.MockRouter) -> respx.Route:
    """ФССП ответил: открытых производств нет."""
    return m.get(FSSP_URL).mock(
        return_value=httpx.Response(200, text=_jsonp(FSSP_EMPTY_HTML), headers=JSONP_HEADERS)
    )


def mock_fssp_cases(m: respx.MockRouter, cases: list[dict[str, Any]]) -> respx.Route:
    return m.get(FSSP_URL).mock(
        return_value=httpx.Response(
            200, text=_jsonp(fssp_result_html(cases)), headers=JSONP_HEADERS
        )
    )


def mock_fssp_captcha(m: respx.MockRouter) -> respx.Route:
    """Настоящее поведение источника на 02.08.2026: требует код с картинки."""
    return m.get(FSSP_URL).mock(
        return_value=httpx.Response(200, text=_jsonp(FSSP_CAPTCHA_HTML), headers=JSONP_HEADERS)
    )


def mock_fssp_internal_error(m: respx.MockRouter) -> respx.Route:
    return m.get(FSSP_URL).mock(
        return_value=httpx.Response(
            200, text=_jsonp(FSSP_INTERNAL_ERROR_HTML), headers=JSONP_HEADERS
        )
    )


# --- локальные реестры открытых данных ФНС ---


async def seed_fresh_registry(
    store: RegistryStore,
    registry: str,
    *,
    rows: int,
    days_old: int = 3,
    source_url: str = "https://file.nalog.ru/opendata/test.zip",
) -> None:
    """Пометить реестр как загруженный свежей выгрузкой ФНС.

    Без этого проверка по правилу честности вернёт «не проверено»: реестр
    без даты выгрузки считается неизвестной свежести.
    """
    data_as_of = (date.today() - timedelta(days=days_old)).isoformat()
    await store.record_load(
        registry, rows=rows, data_as_of=data_as_of, source_url=source_url
    )


async def seed_stale_registry(
    store: RegistryStore,
    registry: str,
    *,
    rows: int,
    days_old: int = 900,
) -> None:
    """Пометить реестр как давно не обновлявшийся — так было на боевом сервере."""
    data_as_of = (date.today() - timedelta(days=days_old)).isoformat()
    await store.record_load(
        registry,
        rows=rows,
        data_as_of=data_as_of,
        source_url="https://file.nalog.ru/opendata/old.zip",
    )
