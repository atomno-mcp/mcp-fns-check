# Changelog

Все существенные изменения публичного API пакета `atomno-mcp-fns-check`.

Формат — [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/), версионирование — [SemVer](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Smoke-архетипы контрагентов** (`tests/test_smoke_archetypes.py`, +11 тестов). 10 типовых сценариев через `check_contractor`: чистая юр.лицо / чистый ИП / ликвидирована / банкротство / дисквалифицированный руководитель / массовый адрес / массовый руководитель / no_reporting / крупные исполнительные производства / ФССП CAPTCHA graceful. Разделяемая fixture-factory (`tests/smoke_helpers.py`: `ScenarioResponses` + `mock_scenario` + 10 именованных архетипов + preload-хелперы для SQLite-реестров). Мета-тест гарантирует покрытие всех 4 значений `verdict_action`.
- **Маркер `py.typed`** (PEP 561) — IDE и mypy теперь увидят type-информацию пакета без установки отдельного typeshed.
- **PyPI-metadata полировка** в `pyproject.toml`: расширенные keywords (13 штук включая `model-context-protocol`, `due-diligence`, `kyc`, `egrip`, `efrsb`), classifiers уровня Beta с дополнительными Intended Audience (Financial, Legal), `Operating System :: OS Independent`, `Framework :: AsyncIO`, `Natural Language :: Russian`, `Typing :: Typed`, `Programming Language :: Python :: 3 :: Only`. Добавлена секция `[project.urls]` (Homepage / Repository / Issues / Changelog / Documentation). Добавлена секция `[tool.hatch.build.targets.sdist]` с явным include/exclude (tests+.env.example включены, `__pycache__`/`.venv`/`*.sqlite`/coverage исключены).

### Changed

- **Dependency constraints** ужесточены до MAJOR-lock против breaking changes в будущих версиях: `fastmcp>=3.0.0,<4.0.0` (было `>=0.2.0` — теоретически допускало fastmcp v4 с breaking API), `httpx>=0.27.0,<1.0.0`, `pydantic>=2.6.0,<3.0.0`, `aiosqlite>=0.20.0,<1.0.0`. Пакет установится только на протестированных major-версиях, update до v2/v4 зависимостей — через явный bump в следующих релизах.

### Fixed

- **КРИТИЧНО**: `.gitignore` — anchored `/data/`, `/db/`, `/logs/` (было `data/`, `db/`, `*.db` без anchor). Hatchling при сборке wheel уважает `.gitignore` как VCS-фильтр; неанкеренные правила исключали `src/atomno_mcp_fns_check/data/` (`okved_2_dict.json`, `registries_seed.json`) и `src/atomno_mcp_fns_check/db/` (`cache.py`, `registries.py`) из итогового wheel. Пакет после `pip install` из PyPI выбрасывал бы `ImportError` на импорте `from .db.cache import SQLiteCache` в `context.py`. Исправление верифицировано через `python -m build` + анализ содержимого wheel-артефакта + smoke-install в чистом venv.
- `.gitignore` — добавлены `*.sqlite`, `*.sqlite-journal`, `atomno_mcp_fns_check_cache*` (runtime создаёт именно `.sqlite`, а не `.sqlite3` — прежний pattern не срабатывал, пользователь наивно зафиксил бы локальную БД в git).
- Badge `tests` в README обновлён с 236 на 247.

## [0.1.0] — 2026-04-24

Первый публичный релиз. Семь тулзов покрывают полный чек-лист due diligence по российским контрагентам через открытые данные ФНС.

### Added

- **`check_contractor(identifier, include_extended_risks=True, lawsuits_threshold_rub=1_000_000)`** — главный агрегирующий тул. Принимает ИНН (10/12 цифр) или ОГРН (13/15), возвращает:
  - `card` — базовая карточка ЕГРЮЛ.
  - `legal_status` — жизненный статус с обогащением из ЕФРСБ.
  - `risks` — до 8 проверок риска со scoring 0..100.
  - `verdict_action` — детерминированный вердикт (`safe_to_proceed` / `manual_review_required` / `high_risk_do_not_proceed` / `impossible_contractor_defunct`).
  - `verdict_reason_ru` — краткое обоснование на русском.
  - `recommendations` — детерминированный список рекомендаций по каждому сработавшему флагу.
  - `sources` — метаданные опрошенных источников.
- **Шесть гранулярных тулзов**:
  - `check_inn(inn, include_extended=False)` — карточка по ИНН.
  - `check_ogrn(ogrn, include_extended=False)` — карточка по ОГРН/ОГРНИП.
  - `get_legal_status(inn, ogrn)` — статус контрагента (active/bankruptcy/liquidated/…) с обогащением из ЕФРСБ.
  - `get_okveds(inn, ogrn, include_history=False)` — коды ОКВЭД с расшифровкой по ОКВЭД-2.
  - `get_directors_history(inn, include_founders=True, depth_years=10)` — текущий руководитель (полная история — после Open Data slice ЕГРЮЛ в v0.5+).
  - `check_for_red_flags(inn, include_extended=True, lawsuits_threshold_rub=1_000_000)` — риск-чек: 4 базовые проверки (`mass_address`, `mass_director`, `disqualified_director`, `bankruptcy_records`) + 4 расширенные (`tax_debts`, `no_reporting`, `enforcement_proceedings`, `active_lawsuits`).
- **`ping()`** — диагностический health-тул, возвращает версию и путь к SQLite-кэшу.
- **CLI `atomno-mcp-fns-etl`** — загрузка Open Data ФНС (массовые адреса, массовые руководители, дисквалифицированные) в локальный SQLite-реестр. Dry-run по умолчанию, `--commit` для фактической записи.
- **SQLite-кэш** карточек с TTL (по умолчанию 168 часов = 7 суток). Повторный запрос по тому же ИНН/ОГРН не ходит в сеть.
- **Graceful degradation**: если ФССП отвечает CAPTCHA, КАД — antibot, pb.nalog.ru — 5xx, отчёт не валится — ошибки собираются в `risks.errors[]`, вердикт понижается до `manual_review_required`.
- **Маскирование ИНН руководителя** (`XXX*****YY`) в ответе — минимизация ПДн согласно SPEC §4.6.
- **Валидация контрольной цифры** ИНН (алгоритм ФНС по приказу МНС N БГ-3-09/178) и ОГРН/ОГРНИП (постановление Правительства N 438).

### Источники

Все источники — публичные, без токенов и регистрации:

- `egrul.nalog.ru` — ЕГРЮЛ/ЕГРИП.
- `bankrot.fedresurs.ru` — ЕФРСБ.
- `pb.nalog.ru` — Прозрачный бизнес ФНС.
- `fssp.gov.ru` — Банк данных исполнительных производств ФССП.
- `kad.arbitr.ru` — Картотека арбитражных дел.
- `nalog.gov.ru/opendata/` — Open Data ФНС (через ETL CLI).

### Тестовое покрытие

- 236 тестов, 85% coverage.
- Все внешние API замоканы (`respx`) — никаких живых HTTP-запросов в CI.

---

[Unreleased]: https://github.com/atomno-labs/mcp-fns-check/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/atomno-labs/mcp-fns-check/releases/tag/v0.1.0
