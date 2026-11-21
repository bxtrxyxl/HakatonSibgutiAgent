# STATE

## Done
- Контакты: в `data/docs/Институты/instituty-i-kafedry.md` добавлены ректорат, проректоры, дирекции ИВТ/ИБ/ИТ/ИЗО и адреса корпусов; в `priemnaya-kampaniya-2026.md` — документы в дирекцию института до 1 сентября.
- `SITE_PAGES` расширен: общежития, оплата за общежитие, портал профилей, итоги приёма 2026, стипендии 2026/2027, приказ о плате за проживание. `data/site_sources.json` — 77 источников.
- `discover_site_sources()` при недоступности портала/страницы приложений сохраняет ранее собранные профили и PDF вместо падения; `run_site_rebuild()` откатывается, если собрано меньше половины прежнего числа документов.
- Добавлен алиас `GET /health` (вне OpenAPI), чтобы внешние пинги не давали 404.
- UI: уточнён текст о восстановлении базы из копии (RU/EN).
- Документация: `PARSING.md`, `PLAN.md`, `STATE.md`.
- Авторизация и лимиты для тяжёлых задач: в `src/config.py`, `.env.example`, `src/security.py`, `src/ingest.py`, `src/api.py` добавлены `ADMIN_TOKEN` (`X-Admin-Token` / `Authorization: Bearer`, локальный режим при пустом значении, `401` при неверном/отсутствующем токене), скользящий rate limit (`ADMIN_RATE_LIMIT_MAX=10`, `ADMIN_RATE_LIMIT_WINDOW_SEC=60`, `429` + `Retry-After`), редактирование токена в логах и эндпоинт `POST /api/admin/verify`.
- UI: в «Настройки → База данных» добавлена карточка «Админ-доступ» (`adminAuthBadge`, `adminTokenInput`, `applyAdminTokenBtn`, `clearAdminTokenBtn`, `adminAuthNote`, RU/EN); токен хранится только в памяти / `sessionStorage` (`sibsutis_admin_token`), не в `localStorage`.
- Проверки: `ruff` — 0 ошибок, `compileall` — ок, `pytest` — 27 passed, база пересобрана (9 документов / 183 чанка, 0 ошибок), `/`, `/api/health`, `/api/databases`, `/api/stats`, `/api/chat` — 200; Playwright (UI) — 0 консольных и HTTP-ошибок, карточка «Админ-доступ» проверена в RU/EN, таблица сравнения и кликабельные источники в чате рендерятся корректно.

## Broken
- Нет.

## Next
- Ограничение частоты полной пересборки (не чаще раза в сутки).
- Устойчивость сбора (аккуратный backoff/повтор для `fetch_url` и логирование причины недоступности в `stats.json`).
