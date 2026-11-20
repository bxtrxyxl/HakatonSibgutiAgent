"""Конфигурация проекта: пути, переменные окружения, константы чанкинга.

Модуль не падает при импорте, если `.env` ещё не заполнен: обязательные
переменные проверяются явно через `require_llm()` / `require_embeddings()`.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# --- Пути -----------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
LOGS_DIR = BASE_DIR / "logs"
WEB_DIR = BASE_DIR / "web"
TESTS_DIR = BASE_DIR / "tests"

KB_PATH = DATA_DIR / "kb.sqlite"
EMBEDDINGS_PATH = DATA_DIR / "embeddings.npy"
REGISTRY_PATH = DATA_DIR / "registry.json"
STATS_PATH = DATA_DIR / "stats.json"
SEED_URLS_PATH = DATA_DIR / "seed_urls.json"
EVENTS_LOG = LOGS_DIR / "events.jsonl"

for _directory in (DATA_DIR, RAW_DIR, LOGS_DIR, WEB_DIR):
    _directory.mkdir(parents=True, exist_ok=True)

load_dotenv(BASE_DIR / ".env")


# --- Переменные окружения -------------------------------------------------

def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


LLM_BASE_URL = _env("LLM_BASE_URL")
LLM_API_KEY = _env("LLM_API_KEY")
LLM_MODEL = _env("LLM_MODEL")

EMBED_BASE_URL = _env("EMBED_BASE_URL") or LLM_BASE_URL
EMBED_API_KEY = _env("EMBED_API_KEY") or LLM_API_KEY
EMBED_MODEL = _env("EMBED_MODEL")

TOP_K = int(_env("TOP_K", "8") or 8)
CANDIDATES_PER_SOURCE = 15
MAX_CHUNKS_PER_DOC = 4
REQUEST_TIMEOUT = 60.0
LLM_TIMEOUT = 90.0

# Чанкинг: ~800-1200 символов с перекрытием ~150.
CHUNK_SIZE = 1100
CHUNK_OVERLAP = 150
MIN_CHUNK_SIZE = 200
# Страховка для эмбеддингов: длиннее модель не примет.
EMBED_MAX_CHARS = 6000

# Сеть
USER_AGENT = "SibSUTIS-Agent/1.0 (+educational hackathon project)"
FETCH_TIMEOUT = 20.0
FETCH_RETRIES = 1


# --- Валидация ------------------------------------------------------------

class ConfigError(RuntimeError):
    """Не заполнены обязательные переменные окружения."""


def _missing(pairs: list[tuple[str, str]]) -> list[str]:
    return [name for name, value in pairs if not value]


def require_llm() -> None:
    missing = _missing([("LLM_BASE_URL", LLM_BASE_URL), ("LLM_API_KEY", LLM_API_KEY), ("LLM_MODEL", LLM_MODEL)])
    if missing:
        raise ConfigError(
            "Не заданы переменные окружения: " + ", ".join(missing) + ". Скопируйте .env.example в .env и заполните."
        )


def require_embeddings() -> None:
    missing = _missing(
        [("EMBED_BASE_URL", EMBED_BASE_URL), ("EMBED_API_KEY", EMBED_API_KEY), ("EMBED_MODEL", EMBED_MODEL)]
    )
    if missing:
        raise ConfigError(
            "Не заданы переменные окружения для эмбеддингов: "
            + ", ".join(missing)
            + ". Заполните .env (см. .env.example)."
        )


def status() -> dict[str, object]:
    """Безопасный снимок конфигурации: только факт наличия секретов."""
    return {
        "llm_configured": not _missing(
            [("LLM_BASE_URL", LLM_BASE_URL), ("LLM_API_KEY", LLM_API_KEY), ("LLM_MODEL", LLM_MODEL)]
        ),
        "embeddings_configured": not _missing(
            [("EMBED_BASE_URL", EMBED_BASE_URL), ("EMBED_API_KEY", EMBED_API_KEY), ("EMBED_MODEL", EMBED_MODEL)]
        ),
        "llm_model": LLM_MODEL or None,
        "embed_model": EMBED_MODEL or None,
        "top_k": TOP_K,
        "data_dir": str(DATA_DIR),
    }
