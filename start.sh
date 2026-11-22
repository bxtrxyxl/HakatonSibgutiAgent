#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
URL="http://${HOST}:${PORT}/"
VENV_PY=".venv/bin/python"

echo "===================================================="
echo "  SibSUTIS AI Assistant — Start"
echo "===================================================="

if [ ! -x "$VENV_PY" ]; then
  SYS_PY=""
  if command -v python3 >/dev/null 2>&1 && python3 -c "import sys; assert sys.version_info >= (3, 10)" >/dev/null 2>&1; then
    SYS_PY="python3"
  elif command -v python >/dev/null 2>&1 && python -c "import sys; assert sys.version_info >= (3, 10)" >/dev/null 2>&1; then
    SYS_PY="python"
  else
    echo "[ERROR] Python 3.10+ is required."
    exit 1
  fi
  echo "[1/4] Creating .venv ($SYS_PY)..."
  "$SYS_PY" -m venv .venv
fi

if ! "$VENV_PY" -c "import fastapi, uvicorn, openai, httpx, bs4, trafilatura, pypdf, numpy, dotenv, pydantic" >/dev/null 2>&1; then
  echo "[1/4] Installing dependencies from requirements.txt..."
  "$VENV_PY" -m pip install --disable-pip-version-check -r requirements.txt
else
  echo "[1/4] Environment and dependencies ready."
fi

if [ ! -f ".env" ] && [ -f ".env.example" ]; then
  cp ".env.example" ".env"
  echo "[2/4] Created .env from .env.example. Fill in your LLM/embedding keys in .env for AI answers."
elif [ -f ".env" ]; then
  echo "[2/4] Configuration .env found."
fi

if ! "$VENV_PY" -m src.ingest --check-kb >/dev/null 2>&1; then
  echo "[3/4] Knowledge base missing or broken - building from data/docs..."
  if "$VENV_PY" -c "from src import config, ingest; config.require_embeddings(); assert 'sk-replace-me' not in config.EMBED_API_KEY; ingest.embed_query('probe', timeout=6.0)" >/dev/null 2>&1; then
    "$VENV_PY" -m src.ingest --dir data/docs || true
  fi
  if ! "$VENV_PY" -m src.ingest --check-kb >/dev/null 2>&1; then
    echo "      Using local keyword index (FTS5)..."
    "$VENV_PY" -m src.ingest --dir data/docs --fts-only
    "$VENV_PY" -m src.ingest --check-kb
  fi
else
  echo "[3/4] Knowledge base ready."
fi

echo "[4/4] Starting server at $URL ..."
(
  sleep 2
  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$URL" >/dev/null 2>&1 || true
  elif command -v open >/dev/null 2>&1; then
    open "$URL" >/dev/null 2>&1 || true
  fi
) &

exec "$VENV_PY" -m uvicorn src.api:app --host "$HOST" --port "$PORT"
