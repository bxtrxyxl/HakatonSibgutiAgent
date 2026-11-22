# Задачи: публичная ссылка на приложение через ngrok

- [x] Task 1: добавить `ngrok.exe`, `ngrok.zip` и логи туннеля в `.gitignore`.
- [x] Task 2: добавить `NGROK_AUTHTOKEN` в `.env.example` с комментарием.
- [x] Task 3: создать `start-public.bat` (проверка ngrok, authtoken, запуск сервера, ожидание `/api/health`, запуск туннеля, вывод и открытие URL).
- [x] Task 4: добавить предупреждение о публичном доступе и `ADMIN_TOKEN`.
- [x] Task 5: описать запуск в `README.md`.
- [x] Task 6: проверить скрипт (наличие ngrok, отсутствие authtoken, запущенный сервер, API 4040), обновить `STATE.md` и журнал `HANDOFF.md`.
- [x] Task 7: прогнать `ruff`, `compileall`, `pytest` и закоммитить.
