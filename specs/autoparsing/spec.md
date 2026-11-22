# Спецификация: Автопарсинг сайта по расписанию

## 1. Цель и границы

Расширить существующий механизм `autoupdate` (`src/ingest.py`, `src/api.py`, `web/index.html`) от обновления только структурированных каталогов до полноценного, управляемого по расписанию цикла обновления данных СибГУТИ — **включая опциональную переиндексацию текстовой базы знаний (`kb.sqlite` + `embeddings.npy`)**, журнал прогонов, автоматическую инвалидацию кэша правой панели и аудит без секретов.

**Не входит в объём:**
- Новый брокер задач (Celery/Redis), внешняя БД или отказ от `DB_LOCK` / `data/app_settings.json`.
- Автоматическая смена структуры курируемых `.md` в `data/docs/` (переиндексация сайта собирает страницы в `kb.sqlite` через `run_urls` с резервной копией и откатом при деградации).
- Параллельное выполнение фоновых задач (сохраняется инвариант «одна задача за раз», иначе `409`).

---

## 2. Требования по разделам

### 2.1. Расписание и конфигурация (`data/app_settings.json`)
- В блоке `autoupdate` файла `data/app_settings.json` хранятся:
  - `enabled: bool` (по умолчанию `false`);
  - `interval_hours: int` (`1..168`, в UI: `6 / 12 / 24 / 72 / 168`);
  - `rebuild_kb: bool` (по умолчанию `false` — безопасный старт: переиндексация страниц сайта в `kb.sqlite` включается явно);
  - `last_run: str` (ISO UTC), `last_status: str` (`"ok" | "partial" | "error" | ""`), `last_summary: dict`;
  - `history: list[dict]` — последние **10** записей прогонов (`started_at`, `finished_at`, `duration_ms`, `status`, `changed`, `errors_count`, `rebuild_kb_status`), самые свежие сверху.
- Планировщик в `lifespan` (`_scheduler_loop`) проверяет расписание каждые 30 секунд; если `enabled && due` и нет запущенной фоновой задачи (`_JOB_STATE["status"] != "running"`), стартует задачу `autoupdate`; если другая задача уже выполняется — прогон тихо пропускается до следующего тика (без ошибки).

### 2.2. Полный цикл автообновления (`run_autoupdate`)
Порядок шагов в одном прогоне под `DB_LOCK`:
1. `programs` → `build_program_catalog()` (`data/programs.json`), сравнение по `_catalog_body_hash`.
2. `achievements` → `build_achievements_catalog()` (`data/achievements.json`), сравнение по `_catalog_body_hash`.
3. `min_scores` → `build_min_scores_catalog()` (`data/min_scores.json`), сравнение по `_catalog_body_hash`.
4. `site_sources` → `discover_site_sources()` (`data/site_sources.json`), сравнение по `_file_hash`.
5. `kb_rebuild` *(только если включено `rebuild_kb: true`)*:
   - Проверяет `site_rebuild_cooldown()`: если прошло меньше 24 ч, шаг получает `status: "skipped"`, `reason: "cooldown"`, не роняя остальные шаги.
   - При допустимом запуске: создаёт резервную копию (`create_backup`), пересобирает базу из `data/site_sources.json` (или использует обновлённый `run_site_rebuild` с контролем деградации: если собрано `0` или `< 50%` прежнего числа документов — откат `restore_backup`, ошибка шага, сброс `last_site_rebuild_at`).
   - При успешной переиндексации обновляется `kb.sqlite` + `embeddings.npy`, сбрасывается `HybridRetriever` в агенте (`_reset_retriever()`).
6. Инвалидация кэша правой панели: изменение любого из `kb.sqlite`, `programs.json`, `min_scores.json`, `achievements.json` меняет `kb_fingerprint`, поэтому следующий `GET /api/overview` (или вызов `refresh_overview()`) автоматически пересчитает `data/overview_cache.json`.

### 2.3. Устойчивость и частичный успех
- Сбой любого отдельного шага (например, временная недоступность `portal.sibsutis.ru`) фиксируется в `steps[]` с `status: "error"`, `reason` (через `_error_reason(exc)`) и текстом ошибки, но **не прерывает** последующие независимые шаги.
- Итоговый `status` прогона:
  - `"ok"`, если все выполненные шаги завершились `ok` или `skipped` (при `cooldown`);
  - `"partial"`, если часть шагов завершилась `ok`, но есть `error`;
  - `"error"`, если упали все выполненные шаги.
- Одновременно выполняется только одна фоновая задача (`start_job` под `_JOB_GUARD` и `DB_LOCK`); ручной запуск `POST /api/autoupdate/run` при активной задаче возвращает `409`.

### 2.4. Наблюдаемость и безопасность
- `GET /api/autoupdate` возвращает текущие настройки (`enabled`, `interval_hours`, `rebuild_kb`, `last_run`, `next_run`, `due`, `last_status`, `last_summary`, `history`, `job`).
- `GET /api/jobs/current` отражает прогресс (`progress`, `step`, `kind: "autoupdate"`).
- По завершении прогона (успех, частичный успех или ошибка) пишется аудит-событие в `logs/events.jsonl` (`session_id: "autoupdate"`, `status`, `latency_ms`, `changed`, `error` через `security.redact()`, **без секретов и токенов**).
- В `web/index.html` (вкладка «База данных», блок «Автообновление по расписанию»):
  - переключатель `toggleAutoRebuildKb` («Пересобирать базу знаний с сайта», RU/EN) с пояснением про суточный лимит и авто-откат;
  - отображение `next_run`, статуса последнего прогона (`ok` / `partial` / `error`), списка изменившихся источников и компактного журнала последних прогонов (до 5 последних строк в UI);
  - после завершения задачи `autoupdate` фронтенд обновляет `fetchDatabaseView()`, `fetchProgramCatalog()`, `fetchSiteCooldown()` и `tryFetchBackendOverview()`.
- Все изменяющие маршруты (`/api/autoupdate`, `/api/autoupdate/run`) остаются защищены `require_admin`.

---

## 3. Критерии приёмки (DoD)
1. `ruff check src/ tests/` — 0 ошибок; `compileall -q src/` — без ошибок.
2. Новые юнит- и API-тесты покрывают: настройку `rebuild_kb`, пропуск `scheduler` при активной задаче, пропуск шага `kb_rebuild` по суточному лимиту, частичный успех при ошибке одного источника, откат при деградации KB, ограничение `history` (<= 10), запись в `logs/events.jsonl` без секретов и авто-инвалидацию `overview_cache.json`.
3. Все существующие и новые детерминированные тесты зелёные (минимум текущие 44 проверки с учётом `test_eval.py` при доступном внешнем API).
4. После `pytest` база пересобрана из `data/docs` (9 документов / 183 чанка / 0 ошибок), в браузере блок автообновления работает в RU и EN без консольных ошибок.
