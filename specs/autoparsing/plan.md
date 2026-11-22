# План реализации: Автопарсинг сайта по расписанию

## 1. Порядок изменений по файлам

1. **Документация (до кода):**
   - `PLAN.md`: дополнить раздел «Автообновление по расписанию» и таблицу вкладки «База данных» полями `rebuild_kb` (`toggleAutoRebuildKb`), журналом прогонов и событием в `logs/events.jsonl`.
   - `STATE.md`: зафиксировать старт/итоги этапа в `Done` / `Next`.

2. **Логика планировщика и пайплайна (`src/ingest.py`):**
   - Константа `AUTOUPDATE_HISTORY_LIMIT = 10`.
   - Обновить `_autoupdate_config()` и `configure_autoupdate(enabled, interval_hours, rebuild_kb=None)`:
     - чтение/сохранение `rebuild_kb: bool` (по умолчанию `False`, обратная совместимость если ключ отсутствует);
     - чтение `history: list[dict[str, Any]]` (нормализовано до 10 записей).
   - Вынести тело пересборки сайта в вспомогательную функцию без повторного вызова `discover_site_sources` при передаче `skip_discover=True` (или запускать шаг `kb_rebuild` через общую логику `run_site_rebuild(..., discover=False)` внутри `run_autoupdate`), чтобы ссылки не скачивались дважды за один прогон.
   - В `run_autoupdate(progress=None, *, on_kb_rebuilt=None)`:
     - Шаги 1–3 (`programs`, `achievements`, `min_scores`) и шаг 4 (`site_sources`).
     - Шаг 5 (`kb_rebuild`, только если `cfg["rebuild_kb"]` истинно):
       - проверить `site_rebuild_cooldown()`; если `not allowed`, добавить шаг `{"key": "kb_rebuild", "title": "База знаний с сайта", "status": "skipped", "reason": "cooldown", "next_at": ...}`;
       - иначе выполнить пересборку с авто-бэкапом и откатом при `< 50%` документов; при успехе вызвать `on_kb_rebuilt()` (для сброса `HybridRetriever`) и отметить `changed: True`.
     - Посчитать итоговый `status` (`"ok" | "partial" | "error"`), длительность `duration_ms`, записать компактную запись в `history` (`[:AUTOUPDATE_HISTORY_LIMIT]`), сохранить `last_run`, `last_status`, `last_summary`, `history` в `data/app_settings.json`.
     - Записать событие в `logs/events.jsonl` через `security.log_event({...})` (безопасно через импорт внутри функции или модуля без циклического импорта).
   - Убедиться, что `_scheduler_loop()` передаёт callback для `_reset_retriever()` (или в `src/api.py` зарегистрировать единый `_autoupdate_task(progress)` для ручного/планировщика через callback `_set_kb_reload_hook` или сброс `agent.retriever` при смене `kb.sqlite` / завершении задачи).
     - *Чистое решение без циклических импортов:* в `src/ingest.py` добавить опциональный hook `_KB_RELOAD_HOOK: Callable[[], None] | None` (регистрируется из `src/api.py` при старте `lifespan` -> `_reset_retriever`), вызываемый после успешного `kb_rebuild` в `run_autoupdate` и `run_site_rebuild`.

3. **FastAPI-эндпоинты (`src/api.py`):**
   - В `_lifespan` регистрировать `_reset_retriever` как hook для `ingest` и вызывать `start_autoupdate_scheduler()`.
   - В `AutoUpdateRequest` добавить `rebuild_kb: bool | None = Field(default=None)` для полной обратной совместимости старых клиентов `{enabled, interval_hours}`.
   - Передавать `request.rebuild_kb` в `configure_autoupdate(...)`.
   - `POST /api/autoupdate/run` остаётся под `require_admin` и возвращает `409` при активной фоновой задаче.

4. **Интерфейс (`web/index.html`):**
   - В блоке «Автообновление по расписанию» добавить:
     - вторую строку-переключатель `toggleAutoRebuildKb` (включать полную пересборку БЗ с сайта при автообновлении);
     - компактный список последних прогонов `#autoUpdateHistory` (до 5 последних записей: время, статус `ok`/`partial`/`error`, изменённые шаги);
     - RU/EN переводы для всех новых подписей и статусов (`dbAutoRebuildKb`, `dbAutoRebuildKbDesc`, `dbAutoHistoryTitle`, `dbAutoPartial`, `dbAutoError`, `dbAutoSkippedCooldown`).
   - В `renderAutoUpdate()`: обновлять `toggleAutoRebuildKb`, бейдж статуса (`is-ok` / `is-run` / `is-err`), историю прогонов.
   - В `fetchAutoUpdate()`: при переходе `running -> done/error` перезагружать `fetchDatabaseView()`, `fetchProgramCatalog()`, `fetchSiteCooldown()`, `tryFetchBackendOverview()`.

5. **Тесты (`tests/test_ingest.py` и `tests/test_api.py`):**
   - `tests/test_ingest.py`:
     - `test_autoupdate_full_cycle_with_kb_rebuild_and_history`: мок-строители каталогов + `run_urls`, проверка вызова `create_backup`, `on_kb_rebuilt`, обновления `history` (с проверкой лимита 10) и записи события в `logs/events.jsonl` без секретов;
     - `test_autoupdate_partial_success_and_cooldown_skip`: ошибка одного каталога + активный суточный лимит -> остальные шаги `ok`, `kb_rebuild` `skipped`, итоговый статус `partial`;
     - `test_autoupdate_rollback_on_degraded_kb`: возврат 0 / `<50%` документов -> `restore_backup`, откат `last_site_rebuild_at`;
     - `test_scheduler_skips_when_job_running`: проверка, что при `_JOB_STATE["status"] == "running"` планировщик не роняет цикл и не запускает дубль.
   - `tests/test_api.py`:
     - `test_api_autoupdate_rebuild_kb_toggle`: `GET/POST /api/autoupdate` сохраняет `rebuild_kb` и возвращает `history` (под `require_admin`).

6. **Верификация (`sibsutis-verify`):**
   - `ruff`, `compileall`, `pytest`, пересборка `data/docs`, рестарт `uvicorn`, HTTP smoke, UI-прогон в браузере (RU/EN, переключатель, отсутствие консольных ошибок), `STATE.md`, коммит, `git push origin main`.

---

## 2. Схемы данных (контракт)

### `data/app_settings.json` → `autoupdate`
```json
{
  "enabled": false,
  "interval_hours": 24,
  "rebuild_kb": false,
  "last_run": "2026-11-22T03:00:00+00:00",
  "last_status": "ok",
  "last_summary": {
    "started_at": "2026-11-22T03:00:00+00:00",
    "finished_at": "2026-11-22T03:00:40+00:00",
    "duration_ms": 40120,
    "status": "ok",
    "changed": ["Каталог направлений"],
    "steps": []
  },
  "history": [
    {
      "started_at": "2026-11-22T03:00:00+00:00",
      "finished_at": "2026-11-22T03:00:40+00:00",
      "duration_ms": 40120,
      "status": "ok",
      "changed": ["Каталог направлений"],
      "errors_count": 0,
      "kb_rebuilt": false
    }
  ]
}
```
