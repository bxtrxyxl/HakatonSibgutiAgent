# Задачи: Автопарсинг сайта по расписанию

Выполнять строго по порядку, по одной логической задаче за шаг.

## Этап A. Контракт и документация
- [ ] Обновить `PLAN.md` (таблица вкладки «База данных» и раздел «Автообновление по расписанию») под `rebuild_kb`, журнал прогонов и аудит в `logs/events.jsonl`.
- [ ] Отметить старт этапа в `STATE.md`.

## Этап B. Бэкенд пайплайна и планировщика (`src/ingest.py`)
- [ ] Добавить `rebuild_kb` и `history` (лимит `AUTOUPDATE_HISTORY_LIMIT = 10`) в `_autoupdate_config()` и `configure_autoupdate()`.
- [ ] Добавить регистрацию callback-а перезагрузки `HybridRetriever` (`register_kb_reload_hook`), вызываемого после успешного `run_site_rebuild`.
- [ ] Расширить `run_autoupdate()`: добавить 5-й шаг `kb_rebuild` (при `rebuild_kb=True`) с проверкой `site_rebuild_cooldown()`, авто-бэкапом и откатом при деградации, расчётом `status` (`ok`/`partial`/`error`), `duration_ms` и сохранением в `history`.
- [ ] Добавить аудит-запись по итогам `run_autoupdate()` в `logs/events.jsonl` через `security.log_event()` с редактированием секретов.
- [ ] Вынести шаг тика планировщика в тестируемую функцию `_scheduler_tick()` и убедиться, что при активной фоновой задаче прогон пропускается без ошибки.

## Этап C. API (`src/api.py`)
- [ ] В `lifespan` при старте приложения регистрировать `_reset_retriever` через `register_kb_reload_hook` до `start_autoupdate_scheduler()`.
- [ ] Расширить `AutoUpdateRequest` опциональным полем `rebuild_kb: bool | None = None` и прокинуть его в `configure_autoupdate()`.

## Этап D. Интерфейс (`web/index.html`)
- [ ] Добавить переключатель `toggleAutoRebuildKb`, элемент журнала прогонов `#autoUpdateHistory` и RU/EN переводы в i18n-словарь.
- [ ] Обновить `renderAutoUpdate()`, `toggleAutoRebuildKb()`, `fetchAutoUpdate()` (обновлять базу/каталог/панель/cooldown при завершении `autoupdate`).

## Этап E. Автотесты (`tests/test_ingest.py`, `tests/test_api.py`)
- [ ] Добавить тесты полного цикла `run_autoupdate` с `kb_rebuild`, лимитом `history <= 10`, пропуском по `cooldown`, частичным успехом, откатом при деградации, тиком планировщика при занятости и аудит-логом без секретов.
- [ ] Добавить API-тест для `GET/POST /api/autoupdate` с полем `rebuild_kb` и `history` под `require_admin`.

## Этап F. Приёмка и фиксация
- [ ] Прогнать `ruff check src/ tests/`, `compileall -q src/`, `pytest -q` и при необходимости пересобрать базу из `data/docs`.
- [ ] Перезапустить `uvicorn`, проверить `/api/health`, `/api/autoupdate`, `/api/overview`, `/api/stats` и UI в браузере (0 консольных ошибок, RU/EN).
- [ ] Обновить `STATE.md`, `ARCHITECTURY.md` и `HANDOFF.md` (журнал), выполнить `git add`, коммит и `git push origin main`.
