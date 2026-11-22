@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "HOST=127.0.0.1"
set "PORT=8000"
set "URL=http://%HOST%:%PORT%/"
set "VENV_PY=.venv\Scripts\python.exe"

echo ====================================================
echo   SibSUTIS AI Assistant - Start
echo ====================================================
echo.

if exist "%VENV_PY%" goto :check_deps

set "SYS_PY="
where py >nul 2>&1
if errorlevel 1 goto :try_python
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 set "SYS_PY=py -3"

:try_python
if defined SYS_PY goto :create_venv
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 set "SYS_PY=python"

:create_venv
if defined SYS_PY goto :do_venv
echo [ERROR] Python 3.10+ not found.
echo Install Python 3.10+ from https://www.python.org/downloads/ with "Add Python to PATH".
pause
exit /b 1

:do_venv
echo [1/4] Creating .venv via %SYS_PY%...
%SYS_PY% -m venv .venv
if not errorlevel 1 goto :check_deps
echo [ERROR] Failed to create .venv.
pause
exit /b 1

:check_deps
"%VENV_PY%" -c "import fastapi, uvicorn, openai, httpx, bs4, trafilatura, pypdf, numpy, dotenv, pydantic, multipart" >nul 2>&1
if not errorlevel 1 goto :deps_ok
echo [1/4] Installing dependencies from requirements.txt...
"%VENV_PY%" -m pip install --disable-pip-version-check -r requirements.txt
if not errorlevel 1 goto :check_env
echo [ERROR] Failed to install dependencies.
pause
exit /b 1

:deps_ok
echo [1/4] Environment and dependencies ready.

:check_env
if exist ".env" goto :env_ok
if not exist ".env.example" goto :env_missing
copy /y ".env.example" ".env" >nul
echo [2/4] Created .env from .env.example (fill LLM_* and EMBED_* keys in .env for AI replies).
goto :check_kb

:env_missing
echo [!] Neither .env nor .env.example was found.
goto :check_kb

:env_ok
echo [2/4] Found .env configuration.

:check_kb
"%VENV_PY%" -m src.ingest --check-kb >nul 2>&1
if not errorlevel 1 goto :kb_ready

echo [3/4] Knowledge base missing or broken - building it from data\docs...
"%VENV_PY%" -c "from src import config, ingest; config.require_embeddings(); assert 'sk-replace-me' not in config.EMBED_API_KEY; ingest.embed_query('probe', timeout=6.0)" >nul 2>&1
if errorlevel 1 goto :kb_fts
"%VENV_PY%" -m src.ingest --dir data/docs
"%VENV_PY%" -m src.ingest --check-kb >nul 2>&1
if not errorlevel 1 goto :kb_done

:kb_fts
echo       Using local keyword index (FTS5) - works without embeddings API...
"%VENV_PY%" -m src.ingest --dir data/docs --fts-only
"%VENV_PY%" -m src.ingest --check-kb
if not errorlevel 1 goto :kb_done
echo [ERROR] Could not build the knowledge base from data\docs.
pause
exit /b 1

:kb_done
echo [3/4] Knowledge base built.
goto :start_server

:kb_ready
echo [3/4] Knowledge base ready.

:start_server
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>&1
if errorlevel 1 goto :launch_uvicorn
echo [4/4] Server is already running at %URL%. Opening browser...
start "" "%URL%"
exit /b 0

:launch_uvicorn
echo [4/4] Starting server at %URL% (press Ctrl+C or close window to stop)...
start "" /b powershell -NoProfile -Command "for ($i=0; $i -lt 30; $i++) { try { $r = Invoke-WebRequest -Uri 'http://%HOST%:%PORT%/api/health' -UseBasicParsing -TimeoutSec 2; if ($r.StatusCode -eq 200) { Start-Process '%URL%'; break } } catch {}; Start-Sleep -Milliseconds 500 }" >nul 2>&1
"%VENV_PY%" -m uvicorn src.api:app --host %HOST% --port %PORT%
if not errorlevel 1 exit /b 0
echo.
echo [ERROR] Server exited with code %errorlevel%.
pause
exit /b 1
