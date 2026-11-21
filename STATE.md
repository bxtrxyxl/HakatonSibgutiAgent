# STATE

## Done
- Фоновый планировщик автообновления: `src/ingest.py` (`start_autoupdate_scheduler`, `run_autoupdate`, `job_status`, `start_job`), `src/api.py` (`/api/autoupdate`, `/api/autoupdate/run`, `/api/jobs/current`, lifespan).
- UI: секция «Автообновление по расписанию» в «Базе данных» — тумблер, интервал, «Обновить сейчас», бейдж статуса, прогресс-бар и автоопрос.
- `POST /api/site/refresh` переведён на фоновую задачу с прогрессом.
- UI-блок «Полная пересборка с сайта»: `rebuildSiteBtn`, `siteRebuildBadge`, прогресс из `GET /api/jobs/current`, авто-копия и откат при пустой сборке (`run_site_rebuild`).
- Документация: `PLAN.md`, `README.md`, `ARCHITECTURY.md`.
- Проверки: `ruff` — 0 ошибок, `compileall` — ок, `pytest` — 23 passed, база пересобрана (9 документов / 179 чанков), Playwright — 0 ошибок консоли.
- Коммиты: `d1b85a9`, `2172930` и текущий (полная пересборка с сайта) запушены в `origin/main`.

## Broken
- Нет.

## Next
- Авторизация и лимиты для публичного запуска тяжёлых задач.
- Ограничение частоты полной пересборки (не чаще раза в сутки).
