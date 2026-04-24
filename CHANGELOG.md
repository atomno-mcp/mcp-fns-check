# Changelog

Все существенные изменения публичного API пакета `atomno-mcp-fns-check`.

Формат — [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/), версионирование — [SemVer](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] — 2026-04-25

**🚀 Опубликован на PyPI**: https://pypi.org/project/atomno-mcp-fns-check/0.1.0/
**🐙 GitHub**: https://github.com/atomno-labs/mcp-fns-check

### Качество релиза

- **247 тестов**, **85% coverage** (`pytest --cov` на полном наборе).
- Все внешние HTTP замоканы через `respx` — никаких живых запросов в CI.
- **10 smoke-архетипов** контрагентов (`tests/test_smoke_archetypes.py`) покрывают все 4 значения `verdict_action` (`safe_to_proceed` / `manual_review_required` / `high_risk_do_not_proceed` / `impossible_contractor_defunct`).
- Pre-release audit пройден: secrets scan, README public-audience check, `twine check`, wheel-contents audit, smoke-install из чистого venv.

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

### Metadata / упаковка

- **`py.typed`** (PEP 561) — IDE и mypy видят type-информацию без дополнительной установки.
- **`pyproject.toml`**: расширенные keywords (13), classifiers Beta, `Intended Audience :: Financial/Legal`, `Natural Language :: Russian`, `Typing :: Typed`. Секция `[project.urls]` (Homepage / Repository / Issues / Changelog / Documentation). Секция `[tool.hatch.build.targets.sdist]` с явным include (`src/**`, `tests/**`, `README.md`, `LICENSE`, `CHANGELOG.md`, `pyproject.toml`, `.env.example`) и exclude (caches, venv, sqlite, build artifacts).
- **Dependency constraints** MAJOR-lock: `fastmcp>=3.0.0,<4.0.0`, `httpx>=0.27.0,<1.0.0`, `pydantic>=2.6.0,<3.0.0`, `aiosqlite>=0.20.0,<1.0.0`.
- **Optional-dependency `release`** — `build` + `twine` для релизных сессий.

### Fixed (ещё в pre-release)

- **КРИТИЧНО**: `.gitignore` — anchored `/data/`, `/db/`, `/logs/` (было без anchor). Hatchling при сборке wheel уважает `.gitignore` как VCS-фильтр; неанкеренные правила исключали `src/atomno_mcp_fns_check/data/` (okved_2_dict.json, registries_seed.json) и `src/atomno_mcp_fns_check/db/` (cache.py, registries.py) из итогового wheel. Без фикса установка из PyPI упала бы на `ImportError`. Верифицировано через содержимое wheel + smoke-install в чистом venv.
- `.gitignore` — добавлены `*.sqlite`, `*.sqlite-journal`, `atomno_mcp_fns_check_cache*` (runtime создаёт `.sqlite`, не `.sqlite3`).

---

[Unreleased]: https://github.com/atomno-labs/mcp-fns-check/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/atomno-labs/mcp-fns-check/releases/tag/v0.1.0
