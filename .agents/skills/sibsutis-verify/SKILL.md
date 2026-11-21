---
name: sibsutis-verify
description: >-
  Run the full verification, KB rebuild, server restart, HTTP smoke test, UI check,
  and git commit/push checklist for the SibSUTIS Agent project. Use after editing
  any Python file, src/api.py, or web/index.html, and before committing changes.
---

# SibSUTIS Agent Verification Runbook

Run all commands from the workspace root using the project virtualenv (`.venv\Scripts\python.exe`).

## 1. Static Checks & Pytest (after any `.py` edit)

```powershell
.venv\Scripts\python.exe -m ruff check src/ tests/
.venv\Scripts\python.exe -m compileall -q src/
$env:PYTHONIOENCODING='utf-8'
.venv\Scripts\python.exe -m pytest -q
```

Notes:
- `pyproject.toml` targets Python 3.10 with rules `E, F, W, I, UP, B, RUF` (`B008` is active — use `Annotated[..., Header(...)] = None`, not `Header(...)` as default).
- Never weaken assertions or skip failing tests.

## 2. Mandatory KB Rebuild After Pytest

`tests/test_eval.py` inserts synthetic documents into `data/kb.sqlite`. Always rebuild from `data/docs` after running `pytest`:

```powershell
$env:PYTHONIOENCODING='utf-8'
.venv\Scripts\python.exe -m src.ingest --dir data/docs
```

Expected: `документов=9, чанков=183, ошибок=0` (or updated count if `data/docs` changed).

## 3. Restart Uvicorn & HTTP Smoke Check (after `src/api.py` or `web/index.html` edits)

Stop existing listener on port 8000:
```powershell
$conn = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($conn) {
    $conn | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { Stop-Process -Id $_ -Force }
}
```

Start fresh server as a daemon:
```powershell
$env:PYTHONIOENCODING='utf-8'
.venv\Scripts\python.exe -m uvicorn src.api:app --host 127.0.0.1 --port 8000
```

Verify endpoints (use UTF-8 bytes for Cyrillic JSON in PowerShell):
```powershell
$root = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/' -UseBasicParsing -TimeoutSec 5
$health = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/health' -TimeoutSec 5
$stats = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/stats' -TimeoutSec 5
$body = [System.Text.Encoding]::UTF8.GetBytes('{"session_id":"","message":"Какие документы нужны при поступлении?"}')
$chat = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/chat' -Method Post -ContentType 'application/json; charset=utf-8' -Body $body -TimeoutSec 30
```

Confirm: `/` is 200, `/api/health` is `ok`, `/api/stats` matches rebuilt KB, `/api/chat` returns non-empty `answer_markdown` and `sources`.

## 4. UI Verification (Browser Subagent)

Delegate to the `browser` subagent (`Model: "flash"`) on `http://127.0.0.1:8000/`:
- 0 console errors and 0 HTTP `>= 400` network errors.
- Any modified Settings / Database tab controls render cleanly in RU and EN.
- Sending a comparison question renders a Markdown `<table>` and clickable `target="_blank"` links under «Источники».

## 5. Finalize Docs, Commit & Push

1. Update `STATE.md` (`Done` / `Broken` / `Next`) with exact check metrics.
2. Run `git status --short` — verify NO `.env`, `.venv.lnk`, `logs/`, `data/kb.sqlite*`, `data/embeddings.npy`, `data/registry.json`, `data/stats.json`, `data/backups/`, `data/app_settings.json`.
3. Stage explicit files: `git add <file1> <file2> ...` (never `git add -f`).
4. Commit with Conventional Commits (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`).
5. Push: `git push origin main` and report `git log -1 --oneline`.
