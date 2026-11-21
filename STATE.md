# STATE

## Done
- Фоновый планировщик автообновления: `src/ingest.py` (`start_autoupdate_scheduler`, `run_autoupdate`, `job_status`, `start_job`), `src/api.py` (`/api/autoupdate`, `/api/autoupdate/run`, `/api/jobs/current`, lifespan).
- UI: секция «Автообновление по расписанию» в «Базе данных» — тумблер, интервал, «Обновить сейчас», бейдж статуса, прогресс-бар и автоопрос.
- `POST /api/site/refresh` переведён на фоновую задачу с прогрессом.
- Документация: `PLAN.md`, `README.md`, `ARCHITECTURY.md`.
- Проверки: `ruff` — 0 ошибок, `pytest` — 23 passed, база пересобрана (9 документов / 179 чанков), Playwright — 0 ошибок консоли.
- Коммит: `d1b85a9 feat: add scheduled catalog autoupdate with progress` (запушен в origin/main).

## Broken
- Нет.

## Next
- Добавить UI-блок и прогресс для полной пересборки из сайта (`POST /api/site/refresh` уже возвращает `job`).
- Добавить авторизацию и лимиты для публичного запуска.
