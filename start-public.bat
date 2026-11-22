@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "PORT=8000"
set "NGROK=%~dp0ngrok.exe"
set "CF=%~dp0cloudflared.exe"
set "VENV_PY=.venv\Scripts\python.exe"
set "CF_LOG=%TEMP%\sibsutis_cloudflared.log"
set "NGROK_LOG=%TEMP%\sibsutis_ngrok.log"
set "SSH_LOG=%TEMP%\sibsutis_ssh_tunnel.log"
set "SSH_ERR=%TEMP%\sibsutis_ssh_tunnel.err"
set "SSH_PID=%TEMP%\sibsutis_ssh_tunnel.pid"
set "URL_FILE=%TEMP%\sibsutis_public_url.txt"
set "STATE_FILE=%TEMP%\sibsutis_admin_state.txt"
rem start.bat must not open the local page: we open the public link instead.
set "NO_BROWSER=1"

echo ====================================================
echo   SibSUTIS AI Assistant - Public link
echo ====================================================
echo.

echo [1/4] Checking the local server on http://127.0.0.1:%PORT%/ ...
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>&1
if not errorlevel 1 goto :server_ready
echo       Starting the app in a separate window...
start "SibSUTIS server" /d "%~dp0" cmd /c start.bat
powershell -NoProfile -Command "for ($i=0; $i -lt 120; $i++) { try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:%PORT%/api/health' -UseBasicParsing -TimeoutSec 2; if ($r.StatusCode -eq 200) { exit 0 } } catch {}; Start-Sleep -Milliseconds 500 }; exit 1"
if errorlevel 1 goto :server_failed

:server_ready
echo       Server is up.
set "ADMIN_STATE=unknown"
if not exist "%VENV_PY%" goto :try_cloudflared
"%VENV_PY%" -c "from src import config; print('set' if config.ADMIN_TOKEN else 'empty')" > "%STATE_FILE%" 2>nul
if exist "%STATE_FILE%" set /p ADMIN_STATE=<"%STATE_FILE%"
del "%STATE_FILE%" >nul 2>&1

:try_cloudflared
if exist "%CF%" goto :run_cloudflared
echo [2/4] cloudflared.exe not found - downloading it once (about 55 MB)...
powershell -NoProfile -Command "try { Invoke-WebRequest -Uri 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe' -OutFile '%CF%' -UseBasicParsing -TimeoutSec 300; exit 0 } catch { exit 1 }"
if errorlevel 1 goto :try_ngrok
if not exist "%CF%" goto :try_ngrok
echo       cloudflared is ready.

:run_cloudflared
echo [2/4] Opening a public link with Cloudflare...
if exist "%URL_FILE%" del "%URL_FILE%" >nul 2>&1
if exist "%CF_LOG%" del "%CF_LOG%" >nul 2>&1
taskkill /f /im cloudflared.exe >nul 2>&1
start "" /b "%CF%" tunnel --url http://127.0.0.1:%PORT% --no-autoupdate --protocol http2 --logfile "%CF_LOG%" --loglevel info
powershell -NoProfile -Command "for ($i=0; $i -lt 180; $i++) { if (Test-Path '%CF_LOG%') { $txt = Get-Content '%CF_LOG%' -Raw -ErrorAction SilentlyContinue; if ($txt -match 'Registered tunnel connection') { $m = [regex]::Matches($txt, 'https://[a-z0-9-]+\.trycloudflare\.com') | ForEach-Object { $_.Value } | Select-Object -First 1; if ($m) { [IO.File]::WriteAllText('%URL_FILE%', $m); exit 0 } } }; Start-Sleep -Milliseconds 500 }; exit 1"
if errorlevel 1 goto :cf_failed
set "PUBLIC_URL="
set /p PUBLIC_URL=<"%URL_FILE%"
echo       Tunnel registered: %PUBLIC_URL%
echo       Waiting for the public address to answer (up to 2 minutes)...
rem A fresh hostname is not in DNS yet. An early lookup can be cached as
rem "does not exist" for minutes, so give the edge a moment and flush the cache.
ipconfig /flushdns >nul 2>&1
powershell -NoProfile -Command "Start-Sleep -Seconds 3; for ($i=0; $i -lt 120; $i++) { $c = & curl.exe -s -o NUL --max-time 10 -w '%%{http_code}' '%PUBLIC_URL%/api/health' 2>$null; if ($c -eq '200') { exit 0 }; Start-Sleep -Milliseconds 1000 }; exit 1"
if not errorlevel 1 goto :tunnel_ready
echo       curl could not confirm the link - trying PowerShell...
powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; ipconfig /flushdns > $null 2>&1; for ($i=0; $i -lt 60; $i++) { try { $r = Invoke-WebRequest -Uri '%PUBLIC_URL%/api/health' -UseBasicParsing -TimeoutSec 10; if ($r.StatusCode -eq 200) { exit 0 } } catch {}; Start-Sleep -Milliseconds 1000 }; exit 1"
if not errorlevel 1 goto :tunnel_ready
echo       The link is not confirmed yet. If the page does not open, wait a minute and refresh.

:cf_failed
echo       Cloudflare did not open a tunnel. Trying ngrok...
taskkill /f /im cloudflared.exe >nul 2>&1

:try_ngrok
if not exist "%NGROK%" goto :ssh_tunnel
echo [2/4] Checking the ngrok configuration...
"%NGROK%" config check >nul 2>&1
if not errorlevel 1 goto :ngrok_ready
if not exist ".env" goto :ngrok_prompt
if not exist "%VENV_PY%" goto :ngrok_prompt
"%VENV_PY%" -c "from dotenv import dotenv_values; print(dotenv_values('.env').get('NGROK_AUTHTOKEN') or '')" > "%TEMP%\sibsutis_ngrok_token.txt" 2>nul
set "NGROK_TOKEN="
set /p NGROK_TOKEN=<"%TEMP%\sibsutis_ngrok_token.txt"
del "%TEMP%\sibsutis_ngrok_token.txt" >nul 2>&1
if defined NGROK_TOKEN goto :ngrok_save

:ngrok_prompt
echo       No ngrok authtoken yet. Get a free one at:
echo         https://dashboard.ngrok.com/get-started/your-authtoken
echo       Tip: put NGROK_AUTHTOKEN=... into .env to skip this prompt next time.
set "NGROK_TOKEN="
set /p NGROK_TOKEN=      Paste authtoken and press Enter: 
if not defined NGROK_TOKEN goto :ssh_tunnel

:ngrok_save
"%NGROK%" config add-authtoken "%NGROK_TOKEN%" >nul 2>&1
set "NGROK_TOKEN="
if errorlevel 1 goto :ssh_tunnel

:ngrok_ready
echo [2/4] Opening a public link with ngrok...
if exist "%URL_FILE%" del "%URL_FILE%" >nul 2>&1
if exist "%NGROK_LOG%" del "%NGROK_LOG%" >nul 2>&1
taskkill /f /im ngrok.exe >nul 2>&1
start "" /b "%NGROK%" http %PORT% --log "%NGROK_LOG%"
powershell -NoProfile -Command "for ($i=0; $i -lt 40; $i++) { try { $t = Invoke-RestMethod -Uri 'http://127.0.0.1:4040/api/tunnels' -TimeoutSec 2; $u = ($t.tunnels | Where-Object { $_.config.addr -match '%PORT%$' } | Select-Object -First 1).public_url; if ($u) { [IO.File]::WriteAllText('%URL_FILE%', $u); exit 0 } } catch {}; Start-Sleep -Milliseconds 500 }; exit 1"
if not errorlevel 1 goto :tunnel_ready
echo       ngrok could not open a tunnel.
if not exist "%NGROK_LOG%" goto :ssh_tunnel
findstr /c:"ERR_NGROK_9040" "%NGROK_LOG%" >nul 2>&1
if not errorlevel 1 echo       Reason: ngrok blocks this IP address (VPN, proxy or provider network).
findstr /c:"ERR_NGROK_105" "%NGROK_LOG%" >nul 2>&1
if not errorlevel 1 echo       Reason: the authtoken looks wrong - get a fresh one from the ngrok dashboard.
findstr /c:"ERR_NGROK_108" "%NGROK_LOG%" >nul 2>&1
if not errorlevel 1 echo       Reason: the free ngrok plan allows one running agent - close other ngrok windows.
taskkill /f /im ngrok.exe >nul 2>&1

:ssh_tunnel
echo [2/4] Opening a free SSH tunnel via localhost.run...
where ssh >nul 2>&1
if errorlevel 1 goto :no_tunnel
if exist "%URL_FILE%" del "%URL_FILE%" >nul 2>&1
if exist "%SSH_LOG%" del "%SSH_LOG%" >nul 2>&1
if exist "%SSH_ERR%" del "%SSH_ERR%" >nul 2>&1
powershell -NoProfile -Command "$p = Start-Process -FilePath 'ssh' -ArgumentList @('-T','-o','StrictHostKeyChecking=accept-new','-o','ServerAliveInterval=30','-o','ExitOnForwardFailure=yes','-R','80:localhost:%PORT%','nokey@localhost.run') -RedirectStandardOutput '%SSH_LOG%' -RedirectStandardError '%SSH_ERR%' -WindowStyle Hidden -PassThru; [IO.File]::WriteAllText('%SSH_PID%', [string]$p.Id)"
powershell -NoProfile -Command "for ($i=0; $i -lt 60; $i++) { if (Test-Path '%SSH_LOG%') { $m = Select-String -Path '%SSH_LOG%' -Pattern 'https://[a-z0-9.-]+\.lhr\.life' -AllMatches | ForEach-Object { $_.Matches.Value } | Select-Object -First 1; if ($m) { [IO.File]::WriteAllText('%URL_FILE%', $m); exit 0 } }; Start-Sleep -Milliseconds 500 }; exit 1"
if errorlevel 1 goto :no_tunnel

:tunnel_ready
set "PUBLIC_URL="
set /p PUBLIC_URL=<"%URL_FILE%"
del "%URL_FILE%" >nul 2>&1
if not defined PUBLIC_URL goto :no_tunnel
echo [4/4] Public link is ready.
echo.
echo   %PUBLIC_URL%
echo.
echo   Notes:
echo    - Open the link from any device. Keep this window open while the link is needed.
echo    - The address changes after a restart of the tunnel.
echo    - Anyone with the link can chat, so it spends your LLM credits.
if /i "%ADMIN_STATE%"=="empty" echo    - WARNING: ADMIN_TOKEN is empty in .env, so rebuild and backup endpoints are open to the internet.
echo.
if defined NO_OPEN goto :link_printed
start "" "%PUBLIC_URL%"
:link_printed
echo   The local server keeps running. Press any key to stop the public link...
pause >nul
goto :cleanup

:server_failed
echo [ERROR] The server did not become healthy. Check the "SibSUTIS server" window.
pause
exit /b 1

:no_tunnel
echo [ERROR] Could not create a public link.
echo   cloudflared: see %CF_LOG%
echo   ngrok:       see %NGROK_LOG%
echo   ssh:         see %SSH_LOG% and %SSH_ERR%
echo   If the network blocks all of them, use a phone hotspot and run this file again.
pause
exit /b 1

:cleanup
taskkill /f /im cloudflared.exe >nul 2>&1
taskkill /f /im ngrok.exe >nul 2>&1
if not exist "%SSH_PID%" exit /b 0
set "SSH_CHILD="
set /p SSH_CHILD=<"%SSH_PID%"
del "%SSH_PID%" >nul 2>&1
if not defined SSH_CHILD exit /b 0
taskkill /f /pid %SSH_CHILD% >nul 2>&1
exit /b 0
