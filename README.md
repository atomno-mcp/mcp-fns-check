<!-- mcp-name: io.github.atomno-mcp/mcp-fns-check -->

# atomno-mcp-fns-check

> MCP-сервер для проверки российских контрагентов (юридические лица и ИП) через публичные данные ФНС: ЕГРЮЛ/ЕГРИП, ЕФРСБ, «Прозрачный бизнес», ФССП, КАД.

![build](https://img.shields.io/badge/build-local-informational)
![version](https://img.shields.io/badge/version-0.1.0-blue)
![license](https://img.shields.io/badge/license-MIT-green)
![mcp](https://img.shields.io/badge/MCP-compatible-brightgreen)
![tests](https://img.shields.io/badge/tests-265%20passed-brightgreen)
![coverage](https://img.shields.io/badge/coverage-86%25-brightgreen)

Готов к подключению в Claude Desktop, Cursor, Claude Code, Cline и любой другой клиент, совместимый с Model Context Protocol (MCP).

---

## Зачем

AI-агент (Claude, Cursor, etc.) обычно ничего не знает о российских контрагентах: ЕГРЮЛ не индексируется поисковиками нормально, данные в Прозрачном бизнесе ФНС — за POST-запросами и CAPTCHA, ЕФРСБ отдаёт HTML. Этот MCP-сервер даёт агенту **семь тулзов**, через которые он за один вызов получит полную картину:

- Кто это: наименование, адрес, ОКВЭД, руководитель.
- Жив ли: действующее, в ликвидации, банкротство, ликвидировано, реорганизация.
- Безопасно ли с ним работать: массовый адрес, массовый руководитель, дисквалификация, банкротство, налоговые долги, непредставление отчётности, исполнительные производства, арбитражные дела.

Главный тул — `check_contractor(identifier)` — принимает ИНН или ОГРН и возвращает **агрегированный отчёт с вердиктом** (`safe_to_proceed` / `manual_review_required` / `high_risk_do_not_proceed` / `impossible_contractor_defunct`) и список конкретных рекомендаций.

---

## Быстрый старт

### Установка

```bash
pip install atomno-mcp-fns-check
```

Или через `uv` / `pipx`:

```bash
uv pip install atomno-mcp-fns-check
# или
pipx install atomno-mcp-fns-check
```

### Проверка работы

```bash
atomno-mcp-fns-check --version
# → atomno-mcp-fns-check 0.1.1

atomno-mcp-fns-check --help
# → полный список флагов: --transport / --host / --port / --log-level
```

По умолчанию пакет запускается как stdio-MCP-сервер: агент общается с ним через stdin/stdout JSON-RPC. Напрямую из шелла вы его не «потыкаете» — подключите к MCP-клиенту. Для сетевых сценариев доступен флаг `--transport {http,sse,streamable-http}` с `--host`/`--port`.

---

## Подключение к MCP-клиентам

### Cursor

Отредактируйте `mcp.json` (Cursor → Settings → Cursor Settings → MCP):

```json
{
  "mcpServers": {
    "fns-check": {
      "command": "atomno-mcp-fns-check"
    }
  }
}
```

Перезапустите Cursor. В чате спросите: «Проверь контрагента ИНН 7707083893» — агент сам вызовет `check_contractor`.

### Claude Desktop

Отредактируйте `claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`):

```json
{
  "mcpServers": {
    "fns-check": {
      "command": "atomno-mcp-fns-check"
    }
  }
}
```

Перезапустите Claude Desktop.

### Claude Code (CLI)

```bash
claude mcp add fns-check atomno-mcp-fns-check
```

### Cline (VS Code)

В `cline_mcp_settings.json`:

```json
{
  "mcpServers": {
    "fns-check": {
      "command": "atomno-mcp-fns-check",
      "disabled": false,
      "autoApprove": []
    }
  }
}
```

---

## Тулзы

| Тул | Назначение | Вход | Источники |
|---|---|---|---|
| **`check_contractor`** | **Главный.** Полная проверка по одному идентификатору + детерминированный вердикт и рекомендации | `identifier: str` (ИНН 10/12 или ОГРН 13/15) | все 5 |
| `check_inn` | Базовая карточка ЕГРЮЛ | `inn: str` | egrul.nalog.ru |
| `check_ogrn` | Базовая карточка по ОГРН/ОГРНИП | `ogrn: str` | egrul.nalog.ru |
| `get_legal_status` | Жизненный статус с обогащением | `inn` или `ogrn` | ЕГРЮЛ + ЕФРСБ |
| `get_okveds` | Коды ОКВЭД с расшифровкой | `inn` или `ogrn` | ЕГРЮЛ + словарь ОКВЭД-2 |
| `get_directors_history` | Текущий руководитель (+ история по мере Open Data) | `inn: str` | ЕГРЮЛ |
| `check_for_red_flags` | 8 проверок риска (4 базовые + 4 расширенные) | `inn: str` | все 5 |

Используемые публичные источники:

- **egrul.nalog.ru** — ЕГРЮЛ/ЕГРИП, карточка контрагента.
- **bankrot.fedresurs.ru** — ЕФРСБ (Единый федеральный реестр сведений о банкротстве).
- **pb.nalog.ru** — Прозрачный бизнес ФНС (налоговые долги, непредставление отчётности).
- **fssp.gov.ru** — Банк данных исполнительных производств ФССП.
- **kad.arbitr.ru** — Картотека арбитражных дел.
- Локальные срезы реестров ФНС — массовые адреса, массовые руководители, дисквалифицированные лица (загружаются скриптом `atomno-mcp-fns-etl` из Open Data ФНС).

### Пример ответа `check_contractor`

```json
{
  "identifier": "7707083893",
  "identifier_type": "inn",
  "inn": "7707083893",
  "ogrn": "1027700132195",
  "card": {
    "name": {"full": "ПАО СБЕРБАНК", "short": "СБЕРБАНК"},
    "status": "active",
    "address": {"full": "117997, Г.Москва, УЛ. ВАВИЛОВА, Д. 19", "is_mass_address": false},
    "director": {"full_name": "Греф Г. О.", "position": "Президент"},
    "okved_main": {"code": "64.19", "name": "Денежное посредничество прочее"}
  },
  "legal_status": {"status": "active", "status_label_ru": "Действующее", "sources_checked": ["egrul", "efrsb"]},
  "risks": {"overall_risk_level": "low", "overall_risk_score": 0, "flags": [], "errors": []},
  "verdict_action": "safe_to_proceed",
  "verdict_reason_ru": "Статус «Действующее», уровень риска — low (score 0/100). Препятствий к заключению сделки по открытым источникам не найдено.",
  "recommendations": [
    "По открытым источникам препятствий к заключению сделки не обнаружено. Соблюдайте стандартные меры должной осмотрительности (ст. 54.1 НК РФ): копия устава, приказ на руководителя, договор."
  ],
  "sources": {"sources_queried": ["efrsb", "egrul", "fssp", "kad", "pb_fns", "registries"]},
  "tier": "open",
  "checked_at": "2026-04-24T20:15:00Z"
}
```

### Поведение при сбоях источников

- ЕГРЮЛ — единственный **blocking**-источник. Если он недоступен, `check_contractor` поднимает `SourceUnavailableError` (агент получит человекочитаемое сообщение).
- Остальные источники подмешиваются **best-effort**: CAPTCHA на ФССП, antibot на КАД, 5xx на pb.nalog.ru — всё складывается в `risks.errors[]` и НЕ валит отчёт. Верхнеуровневый вердикт становится `manual_review_required`.

---

## Конфигурация

Все настройки — через переменные окружения. Никаких креденшелов не требуется (источники публичные).

| Переменная | Описание | По умолчанию |
|---|---|---|
| `MCP_FNS_CACHE_DB` | Путь к SQLite-файлу кэша карточек | `./atomno_mcp_fns_check_cache.sqlite` |
| `MCP_FNS_REGISTRIES_DB` | Путь к SQLite-файлу реестров (массовые адреса/руководители/дисквалификации) | `<cache>.registries.sqlite` |
| `MCP_FNS_CACHE_TTL_HOURS` | TTL кэшированных карточек, часов | `168` (7 суток) |
| `MCP_FNS_HTTP_TIMEOUT` | Таймаут HTTP, секунд | `15` |
| `MCP_FNS_USER_AGENT` | User-Agent HTTP-клиента | `atomno-mcp-fns-check/0.1 (+https://github.com/atomno-mcp/mcp-fns-check)` |
| `MCP_FNS_LOG_LEVEL` | Уровень логирования (DEBUG/INFO/WARNING/ERROR) | `INFO` |

Шаблон — `.env.example`.

---

## Локальные реестры ФНС

Реестры массовых адресов / руководителей / дисквалифицированных лиц — это CSV/XML-выгрузки Open Data ФНС. Пакет идёт со встроенным мини-сидом (`registries_seed.json`, синтетические тестовые записи) — его достаточно, чтобы тулзы работали «из коробки» и показывали флаги на тестовых ИНН.

Для production-проверок обновите реестры полными срезами через CLI `atomno-mcp-fns-etl`:

```bash
atomno-mcp-fns-etl --registry mass_addresses --source ./fns_open_data/ulm.csv --commit
atomno-mcp-fns-etl --registry mass_directors --source ./fns_open_data/uchredt.csv --commit
atomno-mcp-fns-etl --registry disqualified --source ./fns_open_data/disqualified.csv --commit
```

Источники Open Data:

- `mass_addresses` → [nalog.gov.ru/opendata/7707329152-massreg/](https://www.nalog.gov.ru/opendata/7707329152-massreg/)
- `mass_directors` → [nalog.gov.ru/opendata/7707329152-massuchredt/](https://www.nalog.gov.ru/opendata/7707329152-massuchredt/)
- `disqualified` → [service.nalog.ru/disqualified.do](https://service.nalog.ru/disqualified.do)

По умолчанию CLI работает в `--dry-run` (парсит и печатает sample); для записи нужен явный `--commit`. Meta-поля `<registry>.last_etl`, `<registry>.last_etl_source`, `<registry>.last_etl_count` сохраняются автоматически — используйте их для cron-мониторинга свежести данных.

---

## Разработка

```bash
git clone https://github.com/atomno-mcp/mcp-fns-check
cd mcp-fns-check
python -m venv .venv
source .venv/bin/activate    # Linux/macOS
# .venv/Scripts/activate     # Windows
pip install -e ".[dev]"
pytest -v --cov=src/atomno_mcp_fns_check
```

Внешние API в тестах **никогда не вызываются напрямую** — только через `respx` (мокинг httpx) + локальные фикстуры в `tests/fixtures/`.

---

## Ограничения

- **Нет history для руководителей** — ФНС не отдаёт историю смены через search-API; полная история появится после загрузки Open Data slice ЕГРЮЛ (планируется в v0.5+).
- **ФССП / КАД иногда блокируют** CAPTCHA / antibot. В этом случае проверка падает в `errors[]`, а общий вердикт становится `manual_review_required`.
- **Прозрачный бизнес** отдаёт только факт («есть задолженность» / «нет отчётности»), без суммы. Сумму надо запрашивать в ИФНС.

Pro-tier (hosted backend в `atomno-mcp-fns-check-server` — закрытый бэк) убирает эти ограничения через: кэш Redis 24h, ротация прокси для обхода CAPTCHA, полный срез Open Data ЕГРЮЛ, batch-проверки до 100 ИНН, AI-summary через LLM. Сам backend не опубликован.

---

## Безопасность и юридический статус

- Все источники — **публично открытые данные ФНС** и связанных реестров. Использование легально по 149-ФЗ «Об информации».
- Юридические лица и ИП **не подпадают** под 152-ФЗ (О персональных данных).
- ФИО физических лиц-руководителей публикуются ФНС в ЕГРЮЛ открыто; в outbound-ответах ИНН физлица-руководителя маскируется (формат `XXX*****YY`).
- Никаких write-операций ни в один внешний API.
- Никаких credential'ов / токенов не требуется — источники полностью публичные.

---

## Дисклеймер

Сервис — **агрегатор и удобный интерфейс над публичными данными ФНС**. Не аффилирован с ФНС России, ЕФРСБ, КАД, ФССП. Используется на ваш риск.

Информация в ответах сервиса **не заменяет** полноценной юридической или финансовой оценки. Решение о заключении договора с контрагентом принимаете вы.

---

## Лицензия

MIT — см. `LICENSE`.

---

## Ссылки

- GitHub: [atomno-mcp/mcp-fns-check](https://github.com/atomno-mcp/mcp-fns-check)
- Больше MCP-серверов под брендом atomno: [каталог atomno-mcp.ru](https://atomno-mcp.ru/)
- MCP-спецификация: [modelcontextprotocol.io](https://modelcontextprotocol.io)
