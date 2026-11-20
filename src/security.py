"""Безопасность: детектор инъекций, санитайзер контекста, безопасный JSONL-логер."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src import config

# --- Детектор инъекций ----------------------------------------------------
# Маркеры описывают ДИРЕКТИВЫ (смену роли, отмену правил, запрос промпта),
# а не тему. Слова «инъекция», «атака», «безопасность» сами по себе не флагаются.

INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("role_override", re.compile(
        r"(игнорир\w*\s+(все\s+)?(предыдущ|прошл|выше|прежн)\w*\s+(инструкц|правил|указан)\w*"
        r"|забудь\w*\s+(все\s+)?(инструкц|правил)\w*"
        r"|ignore\s+(all\s+)?(previous|prior|above)\s+(instructions?|rules?|prompts?)"
        r"|disregard\s+(all\s+)?(previous|prior)\s+(instructions?|rules?)"
        r"|отмени\w*\s+(все\s+)?(правил|ограничен)\w*)", re.I)),
    ("role_switch", re.compile(
        r"(ты\s+теперь\s+|теперь\s+ты\s+|представь,?\s+что\s+ты\s+"
        r"|you\s+are\s+now\s+|act\s+as\s+|pretend\s+to\s+be\s+)", re.I)),
    ("prompt_leak", re.compile(
        r"(системн\w*\s+промпт|system\s+prompt|reveal\s+your\s+(instructions|prompt)"
        r"|покаж\w*\s+(свои\s+)?(инструкц|промпт)|выведи\s+(свой\s+)?промпт"
        r"|print\s+your\s+(instructions|prompt))", re.I)),
    ("admin_claim", re.compile(
        r"(я\s+(твой\s+)?(администратор|разработчик|создатель|root)"
        r"|i\s+am\s+(the\s+)?(admin|administrator|developer|owner)"
        r"|режим\s+разработчика|developer\s+mode|jailbreak|\bDAN\b)", re.I)),
    ("cross_session", re.compile(
        r"(переписк\w*\s+(другой|других|чужой|иной)\s+сесси\w*"
        r"|истори\w*\s+(другой|чужой|иной)\s+сесси\w*"
        r"|other\s+session'?s?\s+(history|messages|conversation)"
        r"|messages?\s+from\s+(another|other)\s+session)", re.I)),
    ("hidden_directive", re.compile(
        r"(<\|?\s*im_start\s*\|?>|<\|?\s*im_end\s*\|?>|\[/?INST\]|<<\s*SYS\s*>>"
        r"|\bBEGIN\s+SYSTEM\b|\bEND\s+SYSTEM\b)", re.I)),
]

# Директивы внутри найденных документов (косвенная инъекция).
NEUTRALIZE_PATTERNS = [pattern for _, pattern in INJECTION_PATTERNS] + [
    re.compile(r"(?im)^\s*(system|assistant|developer)\s*:\s*"),
    re.compile(r"(?i)(игнорируй\w*\s+правил\w*|не\s+соблюдай\s+правил\w*|обойди\s+ограничен\w*)"),
    re.compile(r"(?i)(write\s+that|скажи,?\s+что|напиши,?\s+что)\s+.{0,80}(без\s+экзамен\w*|без\s+вступительн\w*)"),
]

REDACTION_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[=:]\s*[^\s,;\"']+"),
]

MAX_LOG_TEXT = 300


def detect_injection(text: str) -> list[str]:
    """Возвращает список флагов. Пустой список — подозрений нет."""
    if not text:
        return []
    flags: list[str] = []
    for name, pattern in INJECTION_PATTERNS:
        if pattern.search(text):
            flags.append(name)
    return flags


def _neutralize(text: str) -> str:
    cleaned = text
    for pattern in NEUTRALIZE_PATTERNS:
        cleaned = pattern.sub("[фрагмент нейтрализован]", cleaned)
    return cleaned


def sanitize_chunks(chunks: list[dict[str, Any]]) -> str:
    """Оборачивает чанки в XML-теги и нейтрализует встроенные директивы."""
    blocks: list[str] = []
    for chunk in chunks:
        title = str(chunk.get("title", "")).replace('"', "'")
        url = str(chunk.get("url", "")).replace('"', "'")
        year = str(chunk.get("year_version") or "не указан").replace('"', "'")
        body = _neutralize(str(chunk.get("text", "")))
        body = body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        blocks.append(
            f'<doc id="{chunk.get("chunk_id", "")}" url="{url}" title="{title}" '
            f'year="{year}" trust="untrusted_data">\n{body}\n</doc>'
        )
    return "\n\n".join(blocks)


def redact(text: str) -> str:
    """Удаляет из строки всё, похожее на ключи и токены."""
    cleaned = str(text)
    for pattern in REDACTION_PATTERNS:
        cleaned = pattern.sub("[redacted]", cleaned)
    return cleaned[:MAX_LOG_TEXT]


def log_event(event: dict[str, Any], path: Path = config.EVENTS_LOG) -> None:
    """Пишет событие в JSONL. Ключи и токены вырезаются."""
    record = dict(event)
    record.setdefault("timestamp", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    for key in ("error", "security_flags", "query"):
        if key in record and isinstance(record[key], str):
            record[key] = redact(record[key])
    if isinstance(record.get("query"), str) and len(record["query"]) > MAX_LOG_TEXT:
        record["query"] = record["query"][:MAX_LOG_TEXT]
    record.pop("query", None)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
