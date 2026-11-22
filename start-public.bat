@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "PORT=8000"
set "NGROK=%~dp0ngrok.exe"
set "VENV_PY=.venv\Scripts\python.exe"
set "NGROK_LOG=%TEMP%\sibsutis_ngrok.log"
set "URL_FILE=%TEMP%\sibsutis_public_url.txt"
set "STATE_FILE=%TEMP%\sibsutis_admin_state.txt"

echo ====================================================
echo   SibSUTIS AI Assistant - Public link (ngrok)
echo ====================================================
echo.

if exist "%NGROK%" goto :have_ngrok
echo [ERROR] ngrok.exe was not found in the project folder:
echo   %~dp0
echo Download it from https://ngrok.com/download and put ngrok.exe next to start-public.bat.
pause
exit /b 1

:have_ngrok
echo [1/4] Checking ngrok configuration...
"%NGROK%" config check >nul 2>&1
if not errorlevel 1 goto :token_ready
if not exist ".env" goto :token_prompt
if not exist "%VENV_PY%" goto :token_prompt
"%VENV_PY%" -c "from dotenv import dotenv_values; print(dotenv_values('.env').get('NGROK_AUTHTOKEN') or '')" > "%TEMP%\sibsutis_ngrok_token.txt" 2>nul
set "NGROK_TOKEN="
set /p NGROK_TOKEN=<"%TEMP%\sibsutis_ngrok_token.txt"
del "%TEMP%\sibsutis_ngrok_token.txt" >nul 2>&1
if defined NGROK_TOKEN goto :save_token

:token_prompt
echo       No ngrok authtoken yet. Get a free one at:
echo         https://dashboard.ngrok.com/get-started/your-authtoken
echo       Tip: put NGROK_AUTHTOKEN=... into .env to skip this prompt next time.
set "NGROK_TOKEN="
set /p NGROK_TOKEN=      Paste authtoken and press Enter: 
if not defined NGROK_TOKEN goto :token_missing

:save_token
"%NGROK%" config add-authtoken "%NGROK_TOKEN%" >nul 2>&1
if errorlevel 1 goto :token_missing
set "NGROK_TOKEN="
echo       Authtoken saved to the ngrok config in your user profile.
goto :start_server

:token_missing
echo [ERROR] ngrok cannot open a public link without an authtoken.
pause
exit /b 1

:token_ready
echo       ngrok is configured.

:start_server
echo [2/4] Checking the local server on http://127.0.0.1:%PORT%/ ...
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>&1
if not errorlevel 1 goto :server_ready
echo       Starting the app in a separate window...
start "SibSUTIS server" /d "%~dp0" cmd /c start.bat
powershell -NoProfile -Command "for ($i=0; $i -lt 120; $i++) { try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:%PORT%/api/health' -UseBasicParsing -TimeoutSec 2; if ($r.StatusCode -eq 200) { exit 0 } } catch {}; Start-Sleep -Milliseconds 500 }; exit 1"
if errorlevel 1 goto :server_failed

:server_ready
echo       Server is up.
if not exist "%VENV_PY%" goto :start_tunnel
"%VENV_PY%" -c "from src import config; print('set' if config.ADMIN_TOKEN else 'empty')" > "%STATE_FILE%" 2>nul
set "ADMIN_STATE=unknown"
set /p ADMIN_STATE=<"%STATE_FILE%"
del "%STATE_FILE%" >nul 2>&1

:start_tunnel
echo [3/4] Looking for an existing tunnel on port %PORT%...
if exist "%URL_FILE%" del "%URL_FILE%" >nul 2>&1
powershell -NoProfile -Command "try { $t = Invoke-RestMethod -Uri 'http://127.0.0.1:4040/api/tunnels' -TimeoutSec 2; $u = ($t.tunnels | Where-Object { $_.config.addr -match '%PORT%$' } | Select-Object -First 1).public_url; if ($u) { [IO.File]::WriteAllText('%URL_FILE%', $u); exit 0 } } catch {}; exit 1"
if not errorlevel 1 goto :tunnel_ready

echo       Opening a new ngrok tunnel...
taskkill /f /im ngrok.exe >nul 2>&1
if exist "%NGROK_LOG%" del "%NGROK_LOG%" >nul 2>&1
start "" /b "%NGROK%" http %PORT% --log "%NGROK_LOG%"
powershell -NoProfile -Command "for ($i=0; $i -lt 60; $i++) { try { $t = Invoke-RestMethod -Uri 'http://127.0.0.1:4040/api/tunnels' -TimeoutSec 2; $u = ($t.tunnels | Where-Object { $_.config.addr -match '%PORT%$' } | Select-Object -First 1).public_url; if ($u) { [IO.File]::WriteAllText('%URL_FILE%', $u); exit 0 } } catch {}; Start-Sleep -Milliseconds 500 }; exit 1"
if errorlevel 1 goto :tunnel_failed

:tunnel_ready
set "PUBLIC_URL="
set /p PUBLIC_URL=<"%URL_FILE%"
del "%URL_FILE%" >nul 2>&1
if not defined PUBLIC_URL goto :tunnel_failed
echo [4/4] Public link is ready.
echo.
echo   %PUBLIC_URL%
echo.
echo   Notes:
echo    - Free ngrok shows a "Visit Site" warning on the first visit: click it.
echo    - The link changes every restart of the tunnel.
echo    - Anyone with the link can chat, so it spends your LLM credits.
if /i "%ADMIN_STATE%"=="empty" echo    - WARNING: ADMIN_TOKEN is empty in .env, so rebuild and backup endpoints are open to the internet.
echo.
start "" "%PUBLIC_URL%"
echo   The local server keeps running. Press any key to stop the tunnel...
pause >nul
taskkill /f /im ngrok.exe >nul 2>&1
exit /b 0

:server_failed
echo [ERROR] The server did not become healthy. Check the "SibSUTIS server" window.
pause
exit /b 1

:tunnel_failed
echo [ERROR] ngrok did not report a public URL.
if exist "%NGROK_LOG%" powershell -NoProfile -Command "Get-Content -Path '%NGROK_LOG%' -Tail 15"
pause
exit /b 1
