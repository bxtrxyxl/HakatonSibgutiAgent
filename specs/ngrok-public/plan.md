# План: публичная ссылка на приложение (Cloudflare + ngrok + SSH-туннель)

## Архитектура и поток данных
1. `start-public.bat` выставляет `NO_BROWSER=1`, чтобы `start.bat` не открывал
   локальную страницу; публичную ссылку открывает сам.
2. Проверка сервера: `Get-NetTCPConnection -LocalPort 8000`; если порт не слушается,
   `start.bat` запускается в отдельном окне, затем ожидание `GET /api/health` до 60 с.
3. Основной транспорт — Cloudflare quick tunnel:
   `cloudflared tunnel --url http://127.0.0.1:8000 --no-autoupdate --protocol http2
   --logfile %TEMP%\sibsutis_cloudflared.log --loglevel info`.
   `--protocol http2` обязателен: QUIC-регистрация на учебной сети не проходит.
   Если `cloudflared.exe` нет в корне, скрипт скачивает его с
   `https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe`
   (файл в `.gitignore`).
4. Ожидание: в логе появляется `Registered tunnel connection`, из него регуляркой
   берётся `https://[a-z0-9-]+\.trycloudflare\.com`, затем проверяется
   `GET <url>/api/health` (200). Только после этого URL считается готовым.
5. Резерв 1 — ngrok: `ngrok config check` → `NGROK_AUTHTOKEN` из `.env` (значение
   не печатается) → однократный ввод и `ngrok config add-authtoken`; URL берётся из
   `http://127.0.0.1:4040/api/tunnels` с фильтром `config.addr` = `:8000`.
6. Резерв 2 — SSH: `ssh -T -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30
   -o ExitOnForwardFailure=yes -R 80:localhost:8000 nokey@localhost.run`, PID в файл,
   адрес `https://<id>.lhr.life` из stdout-лога.
7. Браузер открывает найденный публичный URL (если не задан `NO_OPEN=1`). Страница и
   API на одном origin, поэтому `resolveApiBase()` отправляет `/api/chat`,
   `/api/overview`, `/api/databases` в тот же туннель — правки UI не нужны.
8. По нажатию клавиши скрипт завершает свои cloudflared/ngrok/ssh, сервер остаётся.

## Изменения API
Нет. Туннели прозрачны для FastAPI; CORS не требуется (same-origin).

## Изменения UI
Нет. `start.bat` получает только флаг `NO_BROWSER`.

## Риски и допущения
- Quick tunnel Cloudflare не даёт SLA и меняет адрес при перезапуске; для демо
  достаточно. Регистрация не нужна.
- Если Cloudflare заблокирован провайдером, используются ngrok и SSH-туннель; на
  проверенной сети ngrok забанен по IP (`ERR_NGROK_9040`), а localhost.run сбрасывает
  TLS, поэтому Cloudflare — основной путь.
- Первый запуск скачивает `cloudflared.exe` (~55 МБ); без интернета скрипт сообщает
  об ошибке и переходит к резервам.
- Публичная ссылка = публичный чат: без `ADMIN_TOKEN` любой может вызвать
  пересборку/бэкапы, а чат расходует кредиты LLM-провайдера. Скрипт предупреждает.
- `cloudflared.exe` и `ngrok.exe` не коммитятся (33–55 МБ).
