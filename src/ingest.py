"""Сбор, очистка и индексация базы знаний СибГУТИ.

Режимы CLI:
    python -m src.ingest --urls data/seed_urls.json
    python -m src.ingest --file path/to/doc.html --url https://... --title "..." --section "..."
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
import numpy as np
from bs4 import BeautifulSoup, NavigableString, Tag
from pypdf import PdfReader

from src import config

# --- Константы ------------------------------------------------------------

# Один замок на все записи в базу: ручные эндпоинты и фоновые задачи не пересекаются.
DB_LOCK = threading.Lock()

REMOVE_TAGS = (
    "script", "style", "nav", "header", "footer", "aside", "form",
    "noscript", "iframe", "svg", "button", "select", "input",
)
BLOCK_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "table", "blockquote", "pre", "dt", "dd", "figcaption"}
MAIN_SELECTORS = ("main", "article", "[role=main]", "#content", ".content", "#main", ".main-content")

YEAR_RANGE_RE = re.compile(r"(20\d{2})\s*[-–—/]\s*(20\d{2})")
ACADEMIC_YEAR_RE = re.compile(r"(20\d{2})\s*/\s*(\d{2})")
SINGLE_YEAR_RE = re.compile(r"\b(20\d{2})\b")
YEAR_CONTEXT = re.compile(r"(при[её]м|редакц|год[уа]?|кампани|правил|набор)", re.I)

# Блоки-«мусор»: баннеры браузера, cookie-уведомления, служебная навигация.
BOILERPLATE_RE = re.compile(
    r"(ваш браузер устарел|установите последнюю версию|включите javascript|"
    r"для работы сайта|использу(ются|ет) cookie|мы используем файлы cookie|"
    r"перейти к содержимому|версия для слабовидящих)",
    re.I,
)


# --- Утилиты --------------------------------------------------------------

def _configure_console() -> None:
    """UTF-8 для логов: под uvicorn консоль Windows иначе падает на кириллице."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


_configure_console()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_space(value: str) -> str:
    return re.sub(r"[ \t\u00a0]+", " ", value).strip()


def slugify(url: str, limit: int = 60) -> str:
    slug = re.sub(r"^https?://", "", url)
    slug = re.sub(r"[^a-zA-Z0-9а-яА-Я]+", "-", slug)
    return slug.strip("-")[:limit] or "doc"


def doc_id_for(url: str) -> str:
    return hashlib.sha256(url.strip().lower().encode("utf-8")).hexdigest()[:16]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def detect_format(url: str, content_type: str, filename: str = "") -> str:
    name = (filename or url).lower().split("?")[0]
    if name.endswith(".pdf") or "application/pdf" in content_type:
        return "pdf"
    if name.endswith((".md", ".markdown")):
        return "md"
    if name.endswith(".txt"):
        return "txt"
    if "html" in content_type or name.endswith((".html", ".htm")) or not name.endswith("."):
        return "html"
    return "html"


# --- Загрузка -------------------------------------------------------------

class FetchError(RuntimeError):
    """Ошибка загрузки с машиночитаемой причиной для `stats.json`."""

    def __init__(self, message: str, reason: str = "unknown") -> None:
        super().__init__(message)
        self.reason = reason


def _classify_fetch_error(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.ConnectError):
        text = str(exc).lower()
        if "getaddrinfo" in text or "name or service not known" in text:
            return "dns"
        return "connection"
    if isinstance(exc, httpx.RemoteProtocolError):
        return "protocol"
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 429:
            return "rate_limited"
        if status >= 500:
            return "server_error"
        return "client_error"
    if isinstance(exc, httpx.TransportError):
        return "connection"
    return "unknown"


def _error_reason(exc: Exception) -> str:
    return exc.reason if isinstance(exc, FetchError) else "unknown"


def _is_retryable(exc: Exception) -> bool:
    """Повторяем только временные сбои: сеть, таймауты, 429 и 5xx."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == 429 or status >= 500
    return isinstance(exc, httpx.TransportError)


def _retry_delay(exc: Exception, attempt: int) -> float:
    if isinstance(exc, httpx.HTTPStatusError):
        header = exc.response.headers.get("retry-after", "").strip()
        if header.isdigit():
            return min(float(header), 10.0)
    return min(1.5 * (2**attempt), 8.0)


def _http_get(client: httpx.Client, url: str) -> httpx.Response:
    last_error: Exception | None = None
    for attempt in range(config.FETCH_RETRIES + 1):
        try:
            response = client.get(url)
            response.raise_for_status()
            return response
        except Exception as exc:
            last_error = exc
            if attempt < config.FETCH_RETRIES and _is_retryable(exc):
                time.sleep(_retry_delay(exc, attempt))
                continue
            break
    if last_error is None:
        raise FetchError(f"fetch failed: {url}", reason="unknown")
    reason = _classify_fetch_error(last_error)
    raise FetchError(
        f"fetch failed ({reason}): {type(last_error).__name__}: {last_error}",
        reason=reason,
    ) from last_error



def _archive_candidates(client: httpx.Client, url: str) -> list[str]:
    """Кандидаты на копию в веб-архиве: ближайший снапшот, затем общий редирект."""
    candidates: list[str] = []
    try:
        meta = client.get(
            "https://archive.org/wayback/available",
            params={"url": url, "timestamp": "2025"},
            timeout=10.0,
        )
        if meta.status_code == 200:
            closest = ((meta.json() or {}).get("archived_snapshots") or {}).get("closest") or {}
            if closest.get("available") and closest.get("url"):
                candidates.append(str(closest["url"]))
    except Exception:
        pass
    candidates.append(f"https://web.archive.org/web/{url}")
    return candidates


def fetch_url(url: str, use_wayback: bool = True) -> tuple[bytes, str, str, str]:
    """Возвращает (content, content_type, final_url, retrieved_via)."""
    headers = {"User-Agent": config.USER_AGENT, "Accept-Language": "ru,en;q=0.8"}
    with httpx.Client(timeout=config.FETCH_TIMEOUT, follow_redirects=True, headers=headers) as client:
        try:
            response = _http_get(client, url)
            ctype = response.headers.get("content-type", "")
            return response.content, ctype, str(response.url), "origin"
        except Exception as origin_error:
            origin_reason = (
                origin_error.reason
                if isinstance(origin_error, FetchError)
                else _classify_fetch_error(origin_error)
            )
            if not use_wayback:
                raise FetchError(
                    f"origin unavailable: {origin_error}", reason=origin_reason
                ) from origin_error
            archive_errors: list[str] = []
            for archive_url in _archive_candidates(client, url):
                try:
                    response = _http_get(client, archive_url)
                except Exception as archive_error:
                    archive_errors.append(str(archive_error))
                    continue
                text_probe = response.content[:4000].decode("utf-8", errors="ignore").lower()
                if "has not archived that url" in text_probe or "doesn't have that page" in text_probe:
                    archive_errors.append("no archived copy")
                    continue
                ctype = response.headers.get("content-type", "")
                return response.content, ctype, str(response.url), "wayback"
            raise FetchError(
                f"origin unavailable ({origin_error}); archive unavailable ({'; '.join(archive_errors)})",
                reason=origin_reason,
            ) from origin_error


# --- HTML -> Markdown -----------------------------------------------------

def table_to_markdown(table: Tag) -> str:
    rows: list[list[str]] = []
    for tr in table.find_all("tr"):
        # Только «свои» строки: строки вложенных таблиц не дублируем в родительской.
        if tr.find_parent("table") is not table:
            continue
        cells = tr.find_all(["th", "td"], recursive=False)
        if not cells:
            cells = tr.find_all(["th", "td"])
        if not cells:
            continue
        values = [clean_space(cell.get_text(" ", strip=True)).replace("|", "\\|") for cell in cells]
        if any(values):
            rows.append(values)
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    header = rows[0]
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join([" --- "] * width) + "|"]
    for row in rows[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _block_markdown(node: Tag) -> str:
    name = node.name
    if name == "table":
        return table_to_markdown(node)
    text = clean_space(node.get_text(" ", strip=True))
    if not text:
        return ""
    if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        level = int(name[1])
        return f"{'#' * level} {text}"
    if name == "li":
        return f"- {text}"
    if name == "blockquote":
        return f"> {text}"
    if name == "pre":
        return f"```\n{node.get_text(chr(10), strip=False).strip()}\n```"
    return text


def _walk(node: Tag, out: list[str]) -> None:
    for child in node.children:
        if isinstance(child, NavigableString):
            continue
        if not isinstance(child, Tag):
            continue
        name = (child.name or "").lower()
        if name in REMOVE_TAGS:
            continue
        element_id = (child.get("id") or "").lower()
        classes = " ".join(child.get("class") or []).lower()
        if element_id.startswith("wm-") or "wb-autocomplete" in classes or "donato" == element_id:
            continue
        if name in BLOCK_TAGS:
            rendered = _block_markdown(child)
            if rendered:
                out.append(rendered)
            continue
        _walk(child, out)


def extract_title(soup: BeautifulSoup) -> str:
    h1 = soup.find("h1")
    if h1:
        text = clean_space(h1.get_text(" ", strip=True))
        if text:
            return text
    if soup.title and soup.title.string:
        return clean_space(soup.title.string)
    return ""


def html_to_markdown(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    title = extract_title(soup)
    for tag in soup.find_all(list(REMOVE_TAGS)):
        tag.decompose()
    container: Tag | None = None
    for selector in MAIN_SELECTORS:
        found = soup.select_one(selector)
        if found and len(found.get_text(strip=True)) > 400:
            container = found
            break
    if container is None:
        container = soup.body or soup
    out: list[str] = []
    _walk(container, out)
    markdown = "\n\n".join(out)
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()
    return title, markdown


def trafilatura_fallback(html: str) -> str:
    try:
        import trafilatura

        text = trafilatura.extract(
            html,
            output_format="markdown",
            include_tables=True,
            include_comments=False,
            include_links=False,
            favor_precision=False,
        )
        return (text or "").strip()
    except Exception:
        return ""


# --- PDF / текст ----------------------------------------------------------

def extract_pdf(data: bytes) -> str:
    from io import BytesIO

    reader = PdfReader(BytesIO(data))
    pages: list[str] = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            pages.append("")
    text = "\n\n".join(p.strip() for p in pages if p.strip())
    return text.strip()


def decode_text(data: bytes) -> str:
    for encoding in ("utf-8", "cp1251", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


# --- Метаданные -----------------------------------------------------------

def find_year_version(*texts: str) -> str:
    blob = " ".join(t for t in texts if t)
    ranges = YEAR_RANGE_RE.findall(blob)
    if ranges:
        pairs = sorted(ranges, key=lambda pair: (pair[0], pair[1]))
        return f"{pairs[-1][0]}–{pairs[-1][1]}"
    academic = ACADEMIC_YEAR_RE.findall(blob)
    if academic:
        year, tail = academic[-1]
        return f"{year}/{tail}"
    for match in SINGLE_YEAR_RE.finditer(blob):
        window = blob[max(0, match.start() - 60): match.end() + 60]
        if YEAR_CONTEXT.search(window):
            return match.group(1)
    return ""


# --- Чанкинг --------------------------------------------------------------

def _split_blocks(markdown: str) -> list[str]:
    blocks: list[str] = []
    table: list[str] = []
    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()
        if line.strip().startswith("|"):
            table.append(line.strip())
            continue
        if table:
            blocks.append("\n".join(table))
            table = []
        stripped = line.strip()
        if stripped:
            blocks.append(stripped)
    if table:
        blocks.append("\n".join(table))
    return blocks


def _hard_split(text: str, limit: int) -> list[str]:
    """Аварийное дробление блока, который не влезает в лимит (патологические строки)."""
    pieces: list[str] = []
    current = ""
    for token in re.split(r"(\s+)", text):
        if not token:
            continue
        if len(current) + len(token) > limit and current.strip():
            pieces.append(current.strip())
            current = token.lstrip()
            continue
        current += token
    if current.strip():
        pieces.append(current.strip())
    return pieces


def _expand_tables(blocks: list[str], limit: int) -> list[str]:
    """Дробит слишком большие Markdown-таблицы по строкам, повторяя заголовок.

    Строки таблицы при этом не разрываются, а каждая часть остаётся
    корректной таблицей с шапкой.
    """
    expanded: list[str] = []
    for block in blocks:
        if not block.startswith("|") or len(block) <= limit:
            expanded.append(block)
            continue
        lines = block.split("\n")
        header = lines[0]
        separator = lines[1] if len(lines) > 1 and lines[1].lstrip("|").strip().startswith("---") else None
        body = lines[2:] if separator else lines[1:]
        head = [header] + ([separator] if separator else [])
        current = list(head)
        current_len = sum(len(item) + 1 for item in current)
        for row in body:
            if current_len + len(row) + 1 > limit and len(current) > len(head):
                expanded.append("\n".join(current))
                current = list(head)
                current_len = sum(len(item) + 1 for item in current)
            current.append(row)
            current_len += len(row) + 1
        if len(current) > len(head):
            expanded.append("\n".join(current))

    # Последняя страховка: ни один блок не должен превышать лимит.
    capped: list[str] = []
    for block in expanded:
        if len(block) <= limit:
            capped.append(block)
        else:
            capped.extend(_hard_split(block, limit))
    return capped


def chunk_markdown(markdown: str, title: str, section: str) -> list[dict[str, str]]:
    """Режет Markdown на чанки, не разрывая строки таблиц и не смешивая разделы."""
    blocks = _expand_tables(_split_blocks(markdown), config.CHUNK_SIZE)

    # Сначала группируем блоки по активному заголовку уровня <= 3, чтобы
    # содержимое одного раздела не попадало под заголовок следующего.
    groups: list[tuple[str, list[str]]] = []
    heading = ""
    current_group: list[str] = []
    pending_headings: list[str] = []
    for block in blocks:
        if BOILERPLATE_RE.search(block):
            continue
        if block.startswith("#"):
            level = len(block) - len(block.lstrip("#"))
            if level <= 3:
                if current_group:
                    if any(not item.startswith("#") for item in current_group):
                        groups.append((heading, current_group))
                    else:
                        # Одиночные заголовки не создают пустой чанк: переносим
                        # их в следующий раздел, чтобы не терять контекст.
                        pending_headings.extend(current_group)
                heading = block.lstrip("#").strip()
                current_group = [*pending_headings, block] if pending_headings else [block]
                pending_headings = []
                continue
        current_group.append(block)
    if current_group and any(not item.startswith("#") for item in current_group):
        groups.append((heading, current_group))

    chunks: list[dict[str, str]] = []
    seen: set[str] = set()
    for group_heading, group_blocks in groups:
        _chunk_group(group_heading, group_blocks, chunks, seen)
    return chunks


def _chunk_group(
    heading: str,
    blocks: list[str],
    chunks: list[dict[str, str]],
    seen: set[str],
) -> None:
    """Упаковывает блоки одного раздела в чанки с общим заголовком."""
    current: list[str] = []
    current_len = 0

    def flush() -> None:
        nonlocal current, current_len
        if not current:
            return
        body = "\n\n".join(current).strip()
        current, current_len = [], 0
        if not body:
            return
        # Заголовок добавляется как контекст, только если чанк не начинается с заголовка.
        prefix = f"## {heading}\n\n" if heading and not body.lstrip().startswith("#") else ""
        text = (prefix + body).strip()
        key = sha256_text(text)
        if key in seen:
            return
        seen.add(key)
        chunks.append({"text": text, "heading": heading})

    for block in blocks:
        if current and current_len + len(block) + 2 > config.CHUNK_SIZE:
            tail: list[str] = []
            if len(current[-1]) <= config.CHUNK_OVERLAP * 2:
                tail = [current[-1]]
            flush()
            current = list(tail)
            current_len = sum(len(item) + 2 for item in current)
        current.append(block)
        current_len += len(block) + 2
    flush()


# --- Эмбеддинги -----------------------------------------------------------

_EMBED_CLIENT_LOCK = threading.Lock()
_EMBED_CLIENTS: dict[tuple[str, str, float, Any], Any] = {}


def _embedding_client(timeout: float | None = None) -> Any:
    from openai import OpenAI

    config.require_embeddings()
    effective_timeout = config.REQUEST_TIMEOUT if timeout is None else float(timeout)
    cache_key = (config.EMBED_BASE_URL, config.EMBED_API_KEY, effective_timeout, OpenAI)
    with _EMBED_CLIENT_LOCK:
        client = _EMBED_CLIENTS.get(cache_key)
        if client is None:
            client = OpenAI(
                base_url=config.EMBED_BASE_URL,
                api_key=config.EMBED_API_KEY,
                timeout=effective_timeout,
                max_retries=0,
            )
            _EMBED_CLIENTS[cache_key] = client
    return client


def embed_query(text: str, timeout: float | None = None) -> np.ndarray:
    """Быстрый единичный эмбеддинг поискового запроса с повторным использованием клиента."""
    effective_timeout = config.QUERY_EMBED_TIMEOUT if timeout is None else float(timeout)
    client = _embedding_client(timeout=effective_timeout)
    prepared = (text or "").strip()[: config.EMBED_MAX_CHARS] or "пустой фрагмент"
    response = client.embeddings.create(model=config.EMBED_MODEL, input=[prepared])
    vector = np.asarray(response.data[0].embedding, dtype=np.float32)
    norm = float(np.linalg.norm(vector))
    if norm > 0:
        vector = vector / norm
    return vector


def _embed_batch(client: Any, batch: list[str]) -> list[list[float]]:
    """Эмбеддинги батча; при ошибке 400 дробит батч, изолируя проблемный текст."""
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = client.embeddings.create(model=config.EMBED_MODEL, input=batch)
            ordered = sorted(response.data, key=lambda item: item.index)
            return [item.embedding for item in ordered]
        except Exception as exc:
            last_error = exc
            time.sleep(2.0 * (attempt + 1))
    if len(batch) > 1:
        middle = len(batch) // 2
        return _embed_batch(client, batch[:middle]) + _embed_batch(client, batch[middle:])
    raise RuntimeError(f"embeddings failed after retries: {type(last_error).__name__}: {last_error}")


def embed_texts(texts: list[str], batch_size: int = 32) -> np.ndarray:
    client = _embedding_client()
    prepared = [(text[: config.EMBED_MAX_CHARS] if text.strip() else "пустой фрагмент") for text in texts]
    vectors: list[list[float]] = []
    for start in range(0, len(prepared), batch_size):
        vectors.extend(_embed_batch(client, prepared[start: start + batch_size]))
    matrix = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (matrix / norms).astype(np.float32)


# --- Хранилище ------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    section TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    year_version TEXT,
    format TEXT NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,
    chars INTEGER NOT NULL,
    chunk_count INTEGER NOT NULL,
    retrieved_via TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    heading TEXT,
    text TEXT NOT NULL,
    chars INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    chunk_id UNINDEXED,
    text,
    title,
    section,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""


def connect_db(path: Path | None = None) -> sqlite3.Connection:
    connection = sqlite3.connect(config.KB_PATH if path is None else path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def init_db(connection: sqlite3.Connection, rebuild: bool = False) -> None:
    if rebuild:
        connection.executescript(
            "DROP TABLE IF EXISTS chunks_fts; "
            "DROP TABLE IF EXISTS chunks; "
            "DROP TABLE IF EXISTS documents;"
        )
    connection.executescript(SCHEMA)
    connection.commit()


def existing_hashes(connection: sqlite3.Connection) -> set[str]:
    return {row["sha256"] for row in connection.execute("SELECT sha256 FROM documents")}


def store_document(connection: sqlite3.Connection, doc: dict[str, Any], chunks: list[dict[str, str]]) -> int:
    connection.execute(
        """INSERT INTO documents
           (doc_id, title, url, section, fetched_at, year_version, format, sha256, chars, chunk_count, retrieved_via)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            doc["doc_id"], doc["title"], doc["url"], doc["section"], doc["fetched_at"],
            doc["year_version"], doc["format"], doc["sha256"], doc["chars"], len(chunks),
            doc["retrieved_via"],
        ),
    )
    for index, chunk in enumerate(chunks):
        cursor = connection.execute(
            "INSERT INTO chunks (doc_id, idx, heading, text, chars) VALUES (?, ?, ?, ?, ?)",
            (doc["doc_id"], index, chunk["heading"], chunk["text"], len(chunk["text"])),
        )
        chunk_id = cursor.lastrowid
        connection.execute(
            "INSERT INTO chunks_fts (chunk_id, text, title, section) VALUES (?, ?, ?, ?)",
            (chunk_id, chunk["text"], doc["title"], doc["section"]),
        )
    connection.commit()
    return len(chunks)


# --- Сборка ---------------------------------------------------------------

def process_source(
    source: dict[str, Any],
    connection: sqlite3.Connection,
    known_hashes: set[str],
    use_wayback: bool = True,
) -> tuple[int, list[float] | None]:
    """Обрабатывает один источник. Возвращает (число чанков, эмбеддинги)."""
    url = source["url"]
    section = source.get("section") or "Прочее"
    fetched_at = utc_now()

    content, content_type, _final_url, via = fetch_url(url, use_wayback=use_wayback)
    fmt = detect_format(url, content_type)

    raw_name = f"{doc_id_for(url)}-{slugify(url)}{'.pdf' if fmt == 'pdf' else '.html' if fmt == 'html' else '.' + fmt}"
    (config.RAW_DIR / raw_name).write_bytes(content)

    if fmt == "pdf":
        body = extract_pdf(content)
        title = source.get("title") or Path(url).stem
        if len(body) < 50:
            raise RuntimeError("no_text_layer")
        markdown = body
    elif fmt in {"txt", "md"}:
        body = decode_text(content)
        title = source.get("title") or Path(url).stem
        markdown = body
    else:
        html = decode_text(content)
        extracted_title, markdown = html_to_markdown(html)
        title = source.get("title") or extracted_title or Path(url).stem
        if len(markdown) < config.MIN_CHUNK_SIZE:
            fallback = trafilatura_fallback(html)
            if len(fallback) > len(markdown):
                markdown = fallback

    if len(markdown) < 50:
        raise RuntimeError("empty_content")

    digest = sha256_text(markdown)
    if digest in known_hashes:
        return 0, None

    chunks = chunk_markdown(markdown, title, section)
    if not chunks:
        raise RuntimeError("no_chunks")

    doc = {
        "doc_id": doc_id_for(url),
        "title": title,
        "url": url,
        "section": section,
        "fetched_at": fetched_at,
        "year_version": source.get("year") or find_year_version(title, markdown[:4000]),
        "format": fmt,
        "sha256": digest,
        "chars": len(markdown),
        "retrieved_via": via,
    }
    embed_inputs = [f"{title}\n{section}\n{chunk['text']}" for chunk in chunks]
    # Сначала эмбеддинги, потом запись в БД: иначе при сбое модели
    # чанки остаются в базе без векторов и индексы расходятся.
    vectors = embed_texts(embed_inputs)

    store_document(connection, doc, chunks)
    known_hashes.add(digest)
    return len(chunks), vectors


def sync_embeddings(new_vectors: list[np.ndarray], rebuild: bool) -> int:
    if not new_vectors:
        if rebuild and not config.EMBEDDINGS_PATH.exists():
            np.save(config.EMBEDDINGS_PATH, np.zeros((0, 0), dtype=np.float32))
        return 0
    stacked = np.vstack(new_vectors).astype(np.float32)
    if not rebuild and config.EMBEDDINGS_PATH.exists():
        previous = np.load(config.EMBEDDINGS_PATH)
        if previous.size and previous.shape[1] != stacked.shape[1]:
            raise RuntimeError("размерность эмбеддингов не совпадает с существующей матрицей")
        stacked = np.vstack([previous, stacked]).astype(np.float32)
    np.save(config.EMBEDDINGS_PATH, stacked)
    return int(stacked.shape[0])


def append_embeddings(vectors: np.ndarray) -> None:
    """Дописывает векторы на диск сразу после документа.

    Нужно, чтобы обрыв процесса не оставлял базу без эмбеддингов.
    """
    if vectors.size == 0:
        return
    if config.EMBEDDINGS_PATH.exists():
        previous = np.load(config.EMBEDDINGS_PATH)
        if previous.size and previous.shape[1] != vectors.shape[1]:
            raise RuntimeError("размерность эмбеддингов не совпадает с существующей матрицей")
        vectors = np.vstack([previous, vectors]).astype(np.float32)
    np.save(config.EMBEDDINGS_PATH, vectors)


def verify_index_consistency(connection: sqlite3.Connection) -> int:
    """Проверяет, что число чанков и векторов совпадает."""
    chunks = connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    vectors = int(np.load(config.EMBEDDINGS_PATH).shape[0]) if config.EMBEDDINGS_PATH.exists() else 0
    if chunks != vectors:
        raise RuntimeError(
            f"рассинхронизация индексов: чанков {chunks}, векторов {vectors}. "
            "Пересоберите базу: python -m src.ingest --urls data/seed_urls.json"
        )
    return chunks


def read_registry(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            """SELECT doc_id, title, url, section, fetched_at, year_version, format,
                      chars, chunk_count, retrieved_via
               FROM documents ORDER BY section, title"""
        )
    ]


def write_registry(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = read_registry(connection)
    config.REGISTRY_PATH.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return rows


def write_stats(
    connection: sqlite3.Connection,
    registry: list[dict[str, Any]],
    errors: list[dict[str, str]],
    skipped_duplicates: int,
) -> dict[str, Any]:
    sections: dict[str, int] = {}
    for row in registry:
        sections[row["section"]] = sections.get(row["section"], 0) + 1
    chunk_count = connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    total_chars = connection.execute("SELECT COALESCE(SUM(chars), 0) FROM documents").fetchone()[0]
    by_format: dict[str, int] = {}
    for row in registry:
        by_format[row["format"]] = by_format.get(row["format"], 0) + 1
    stats = {
        "generated_at": utc_now(),
        "unique_docs": len(registry),
        "sections": sections,
        "total_chars": int(total_chars),
        "chunks": int(chunk_count),
        "formats": by_format,
        "skipped_duplicates": skipped_duplicates,
        "errors": errors,
        "error_count": len(errors),
        "embeddings": int(np.load(config.EMBEDDINGS_PATH).shape[0]) if config.EMBEDDINGS_PATH.exists() else 0,
    }
    config.STATS_PATH.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    return stats


def run_urls(
    seed_path: Path,
    use_wayback: bool = True,
    resume: bool = False,
    progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, Any]:
    sources = json.loads(seed_path.read_text(encoding="utf-8"))
    connection = connect_db()
    init_db(connection, rebuild=not resume)
    if not resume and config.EMBEDDINGS_PATH.exists():
        config.EMBEDDINGS_PATH.unlink()
    known = existing_hashes(connection)
    done_urls = (
        {row["url"] for row in connection.execute("SELECT url FROM documents")} if resume else set()
    )
    errors: list[dict[str, str]] = []
    skipped = 0

    for index, source in enumerate(sources, start=1):
        url = source.get("url", "?")
        if progress is not None:
            progress(index, len(sources), url)
        if url in done_urls:
            print(f"[{index}/{len(sources)}] skip (already indexed) {url}")
            continue
        try:
            chunk_count, new_vectors = process_source(source, connection, known, use_wayback)
            if new_vectors is None:
                skipped += 1
                print(f"[{index}/{len(sources)}] skip (duplicate) {url}")
            else:
                append_embeddings(new_vectors)
                print(f"[{index}/{len(sources)}] ok {chunk_count} chunks {url}")
        except Exception as exc:
            message = str(exc)
            errors.append(
                {
                    "url": url,
                    "error": message,
                    "stage": "fetch+extract",
                    "reason": _error_reason(exc),
                }
            )
            print(f"[{index}/{len(sources)}] ERROR {url}: {message}", file=sys.stderr)
        finally:
            # Состояние фиксируется после каждого источника: обрыв не теряет прогресс.
            write_registry(connection)
            write_stats(connection, read_registry(connection), errors, skipped)

    verify_index_consistency(connection)
    registry = write_registry(connection)
    stats = write_stats(connection, registry, errors, skipped)
    connection.close()
    return stats


def load_local_document(path: Path) -> tuple[str, str]:
    """Читает локальный файл и возвращает (markdown, format)."""
    fmt = detect_format(path.name, "", filename=path.name)
    data = path.read_bytes()
    if fmt == "pdf":
        return extract_pdf(data), "pdf"
    text = decode_text(data)
    if fmt in {"txt", "md"}:
        return text, fmt
    _, markdown = html_to_markdown(text)
    return markdown, "html"


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Разбирает необязательный YAML-блок в начале .md (без внешних зависимостей)."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    meta: dict[str, str] = {}
    for line in text[3:end].splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip().lower()] = value.strip().strip("\"'")
    return meta, text[end + 4:].lstrip("\n")


def first_heading(markdown: str) -> str:
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def process_local_file(
    path: Path,
    connection: sqlite3.Connection,
    known: set[str],
    url: str = "",
    title: str = "",
    section: str = "",
    year: str | None = None,
    default_url: str = "",
    with_embeddings: bool = True,
) -> np.ndarray | None:
    """Индексирует один локальный файл. Возвращает векторы или None при дубликате."""
    markdown, fmt = load_local_document(path)
    meta, body = parse_frontmatter(markdown)
    if body.strip():
        markdown = body
    if len(markdown) < 50:
        raise RuntimeError("empty_content")

    # Приоритет: явный аргумент -> url из frontmatter -> local:// путь.
    final_url = url or meta.get("url") or default_url or f"local://{path.name}"
    final_title = title or meta.get("title") or first_heading(markdown) or path.stem
    final_section = section or meta.get("section") or "Прочее"

    digest = sha256_text(markdown)
    if digest in known:
        return None

    chunks = chunk_markdown(markdown, final_title, final_section)
    if not chunks:
        raise RuntimeError("no_chunks")

    doc = {
        "doc_id": doc_id_for(final_url),
        "title": final_title,
        "url": final_url,
        "section": final_section,
        "fetched_at": utc_now(),
        "year_version": year or meta.get("year") or find_year_version(final_title, markdown[:4000]),
        "format": fmt,
        "sha256": digest,
        "chars": len(markdown),
        "retrieved_via": "local_file",
    }
    vectors = (
        embed_texts([f"{final_title}\n{final_section}\n{c['text']}" for c in chunks])
        if with_embeddings
        else np.zeros((0, 0), dtype=np.float32)
    )
    store_document(connection, doc, chunks)
    known.add(digest)
    return vectors


def run_directory(directory: Path, rebuild: bool = True, with_embeddings: bool = True) -> dict[str, Any]:
    """Собирает базу из папки с локальными .md/.txt/.html/.pdf."""
    suffixes = {".md", ".markdown", ".txt", ".html", ".htm", ".pdf"}
    files = sorted(path for path in directory.rglob("*") if path.suffix.lower() in suffixes)
    if not files:
        raise RuntimeError(f"в папке нет поддерживаемых файлов: {directory}")

    connection = connect_db()
    init_db(connection, rebuild=rebuild)
    if rebuild and config.EMBEDDINGS_PATH.exists():
        config.EMBEDDINGS_PATH.unlink()
    known = existing_hashes(connection)
    errors: list[dict[str, str]] = []
    skipped = 0

    for index, path in enumerate(files, start=1):
        relative = path.relative_to(directory).as_posix()
        section = path.parent.name if path.parent != directory else "Прочее"
        try:
            vectors = process_local_file(
                path,
                connection,
                known,
                section=section,
                default_url=f"local://{relative}",
                with_embeddings=with_embeddings,
            )
            if vectors is None:
                skipped += 1
                print(f"[{index}/{len(files)}] skip (duplicate) {relative}")
            else:
                if with_embeddings:
                    append_embeddings(vectors)
                    print(f"[{index}/{len(files)}] ok {vectors.shape[0]} chunks {relative}")
                else:
                    print(f"[{index}/{len(files)}] ok (FTS5) {relative}")
        except Exception as exc:
            errors.append(
                {"url": relative, "error": str(exc), "stage": "local_file", "reason": "local_io"}
            )
            print(f"[{index}/{len(files)}] ERROR {relative}: {exc}", file=sys.stderr)
        finally:
            write_registry(connection)
            write_stats(connection, read_registry(connection), errors, skipped)

    if with_embeddings:
        verify_index_consistency(connection)
    registry = write_registry(connection)
    stats = write_stats(connection, registry, errors, skipped)
    connection.close()
    return stats


def run_single_file(
    path: Path,
    url: str,
    title: str,
    section: str,
    year: str | None = None,
    use_wayback: bool = True,
) -> dict[str, Any]:
    connection = connect_db()
    init_db(connection, rebuild=False)
    known = existing_hashes(connection)
    errors: list[dict[str, str]] = []
    skipped = 0
    vectors: list[np.ndarray] = []

    try:
        result = process_local_file(
            path, connection, known, url=url, title=title, section=section, year=year
        )
        if result is None:
            skipped += 1
        else:
            vectors.append(result)
    except Exception as exc:
        errors.append(
            {"url": url or path.name, "error": str(exc), "stage": "local_file", "reason": "local_io"}
        )

    sync_embeddings(vectors, rebuild=False)
    registry = write_registry(connection)
    stats = write_stats(connection, registry, errors, skipped)
    connection.close()
    return stats


# --- Каталог направлений и обход сайта --------------------------------

def _clean_text(node: Any) -> str:
    return " ".join(node.get_text(" ", strip=True).split())


def _items_after_heading(soup: BeautifulSoup, heading_text: str) -> list[str]:
    heading = None
    for tag in soup.find_all(["h2", "h3"]):
        if heading_text.lower() in _clean_text(tag).lower():
            heading = tag
            break
    if heading is None:
        return []
    items: list[str] = []
    for node in heading.find_all_next():
        if node.name in {"h2", "h3"} and node is not heading:
            break
        if node.name == "li":
            text = _clean_text(node)
            if text and text not in items:
                items.append(text)
    return items


def _first_number(pattern: str, text: str) -> int | None:
    match = re.search(pattern, text, re.I)
    return int(match.group(1)) if match else None


def parse_program_page(url: str, html: str) -> dict[str, Any]:
    """Достаёт из страницы профиля поля для каталога и рекомендаций."""
    soup = BeautifulSoup(html, "html.parser")
    full_text = _clean_text(soup)
    institute = _clean_text(soup.find("h1")) if soup.find("h1") else ""

    code = ""
    direction = ""
    code_tag = None
    for tag in soup.find_all("h2"):
        text = _clean_text(tag)
        match = re.match(r"^(\d{2}\.\d{2}\.\d{2})\s+(.+)$", text)
        if match:
            code, direction = match.group(1), match.group(2)
            code_tag = tag
            break

    profile = ""
    if code_tag is not None:
        next_p = code_tag.find_next("p")
        if next_p is not None:
            profile = _clean_text(next_p)

    stats_text = _clean_text(soup.select_one(".padding-content")) if soup.select_one(".padding-content") else ""
    years = _first_number(r"(\d+)\s*(?:год|года|лет)\s*Срок обучения", stats_text)
    paid = _first_number(r"(\d+)\*?\s*Платные места", stats_text)
    budget = _first_number(r"(\d+)\*?\s*Бюджетные места", stats_text)
    pass_score = _first_number(r"(\d+)\s*Проходной балл", stats_text)
    tuition_match = re.search(r"(?<!\d)(\d{1,3}(?:\s\d{3})+|\d+)\s*руб\.\s*Стоимость обучения", stats_text, re.I)
    salary_match = re.search(r"(?<!\d)(\d{1,3}(?:\s\d{3})+|\d+)\s*руб\.\s*Средняя зарплата", full_text, re.I)

    level = ""
    form = ""
    for tag in soup.find_all("h3"):
        text = _clean_text(tag)
        if "Уровень подготовки" in text:
            level = text.split(":", 1)[-1].strip()
        elif "Формы обучения" in text:
            form = text.split(":", 1)[-1].strip()

    disciplines = _items_after_heading(soup, "Профилирующие дисциплины")
    professions = _items_after_heading(soup, "Кем работают выпускники")
    exams = ""
    if "Вступительные испытания:" in full_text:
        exams = full_text.split("Вступительные испытания:", 1)[1].split("К списку направлений", 1)[0].strip()[:1200]

    plan_pdf = ""
    plan_link = soup.select_one("a.plan__link")
    if plan_link and plan_link.get("href"):
        plan_pdf = urljoin(url, plan_link["href"])

    description = ""
    for paragraph in soup.find_all("p"):
        text = _clean_text(paragraph)
        if len(text) < 150:
            continue
        if text.startswith("Подготовка студентов") or text.startswith("Выпускники данной образовательной программы"):
            description = text[:1200]
            break
    if not description and profile:
        for paragraph in soup.find_all("p"):
            text = _clean_text(paragraph)
            if len(text) > 200 and "СибГУТИ" not in text[:80] and "Приёмная комиссия" not in text:
                description = text[:1200]
                break

    program_id = parse_qs(urlparse(url).query).get("ID", [""])[0] or hashlib.sha1(url.encode()).hexdigest()[:10]

    return {
        "id": program_id,
        "url": url,
        "institute": institute,
        "code": code,
        "direction": direction,
        "profile": profile,
        "level": level,
        "form": form,
        "years": years,
        "budget": budget,
        "paid": paid,
        "pass_score": pass_score,
        "tuition": " ".join(tuition_match.group(1).split()) if tuition_match else "",
        "salary": " ".join(salary_match.group(1).split()) if salary_match else "",
        "disciplines": disciplines,
        "professions": professions,
        "exams": exams,
        "plan_pdf": plan_pdf,
        "description": description,
    }


def discover_program_links(index_url: str = config.PROFILE_INDEX_URL) -> list[dict[str, str]]:
    content, _content_type, final_url, _via = fetch_url(index_url, use_wayback=False)
    soup = BeautifulSoup(decode_text(content), "html.parser")
    found: dict[str, dict[str, str]] = {}
    for anchor in soup.find_all("a", href=True):
        href = urljoin(final_url, anchor["href"])
        if "profile/detail.php" not in href:
            continue
        title = _clean_text(anchor)
        if title and href not in found:
            found[href] = {"url": href, "title": title}
    return list(found.values())


def build_program_catalog() -> dict[str, Any]:
    """Скачивает профили направлений и сохраняет структурированный каталог.

    При сбое отдельных карточек или пустом ответе сохраняет прежние профили из
    текущего ``data/programs.json``, чтобы рабочие данные не терялись.
    """
    previous = load_program_catalog().get("programs", [])
    prev_by_url = {str(item.get("url", "")): item for item in previous if isinstance(item, dict) and item.get("url")}
    links = discover_program_links()
    programs: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    fresh_count = 0
    preserved_count = 0

    for index, link in enumerate(links, start=1):
        url = link["url"]
        try:
            content, _content_type, final_url, _via = fetch_url(url, use_wayback=False)
            program = parse_program_page(final_url, decode_text(content))
            if program.get("code"):
                programs.append(program)
                seen_urls.add(url)
                seen_urls.add(final_url)
                fresh_count += 1
                print(f"[{index}/{len(links)}] program {program['code']} {program.get('profile', '')[:50]}")
            else:
                entry: dict[str, Any] = {"url": url, "error": "no_program_code"}
                fallback = prev_by_url.get(url)
                if fallback is not None:
                    programs.append(copy.deepcopy(fallback))
                    seen_urls.add(url)
                    preserved_count += 1
                    entry["preserved"] = True
                errors.append(entry)
        except Exception as exc:
            entry = {"url": url, "error": f"{type(exc).__name__}: {exc}"}
            fallback = prev_by_url.get(url)
            if fallback is not None:
                programs.append(copy.deepcopy(fallback))
                seen_urls.add(url)
                preserved_count += 1
                entry["preserved"] = True
            errors.append(entry)
            print(f"[{index}/{len(links)}] ERROR {url}: {exc}", file=sys.stderr)

    for old_url, old_item in prev_by_url.items():
        if old_url not in seen_urls:
            programs.append(copy.deepcopy(old_item))
            preserved_count += 1

    if fresh_count == 0 and previous:
        raise RuntimeError("не удалось обновить ни один профиль; сохранён прежний каталог")

    programs.sort(key=lambda item: (item.get("code", ""), item.get("profile", "")))
    payload = {
        "generated_at": utc_now(),
        "source": config.PROFILE_INDEX_URL,
        "count": len(programs),
        "preserved": preserved_count,
        "programs": programs,
        "errors": errors,
    }
    config.PROGRAMS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


_CATALOG_CACHE: dict[str, tuple[int, int, dict[str, Any]]] = {}
_CATALOG_LOCK = threading.Lock()


def _load_json_catalog(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    """Читает JSON-каталог с кэшем по `mtime`/размеру и отдаёт копию.

    Пересборка каталога меняет файл, поэтому кэш инвалидируется сам.
    """
    try:
        stat = path.stat()
    except OSError:
        return copy.deepcopy(default)
    key = str(path)
    with _CATALOG_LOCK:
        cached = _CATALOG_CACHE.get(key)
        if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
            return copy.deepcopy(cached[2])
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return copy.deepcopy(default)
    if not isinstance(payload, dict):
        return copy.deepcopy(default)
    with _CATALOG_LOCK:
        _CATALOG_CACHE[key] = (stat.st_mtime_ns, stat.st_size, payload)
    return copy.deepcopy(payload)


def load_program_catalog() -> dict[str, Any]:
    return _load_json_catalog(
        config.PROGRAMS_PATH,
        {"generated_at": "", "count": 0, "programs": [], "errors": []},
    )


ACHIEVEMENTS_URL = (
    "https://sibsutis.ru/upload/iblock/a08/"
    "Приложение 3 Перечень и порядок учета индивидуальных достижений "
    "при поступлении в СибГУТИ и филиалы.pdf"
)


def _parse_achievement_table(lines: list[str], start_marker: str, end_marker: str) -> list[dict[str, Any]]:
    start = next((i for i, line in enumerate(lines) if start_marker in line), 0)
    end = next((i for i, line in enumerate(lines[start + 1:], start + 1) if end_marker in line), len(lines))
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw in lines[start:end]:
        line = " ".join(raw.split())
        if not line:
            continue
        match = re.match(r"^(\d{1,2})\.\s*(.*)$", line)
        if match:
            if current and current.get("points") is not None:
                entries.append(current)
            title = match.group(2).strip()
            points = None
            inline = re.search(r"\s(\d{1,2})(?:\s*\([^)]*\))?\s*$", title)
            if inline:
                points = int(inline.group(1))
                title = title[: inline.start()].strip()
            current = {
                "number": int(match.group(1)),
                "title": title,
                "details": [],
                "points": points,
            }
            continue
        if current is None:
            continue
        if re.fullmatch(r"\d{1,2}", line) and current.get("details") and current.get("points") is None:
            current["points"] = int(line)
            continue
        if current.get("points") is None and len(line) > 2:
            current["details"].append(line)
    if current and current.get("points") is not None:
        entries.append(current)

    result: list[dict[str, Any]] = []
    for entry in entries:
        details = " ".join(entry["details"]).strip()
        title = (entry["title"] or details).strip()
        if len(title) > 120:
            title = title[:117].rstrip() + "…"
        result.append(
            {
                "number": entry["number"],
                "title": title,
                "details": details[:500],
                "points": entry["points"],
            }
        )
    return result


def build_achievements_catalog() -> dict[str, Any]:
    """Скачивает Приложение 3 и превращает таблицы достижений в JSON.

    Если PDF не содержит текстового слоя или разбор вернул пустые списки,
    недостающие разделы берутся из прежнего файла или обновление прерывается.
    """
    previous = load_achievements_catalog()
    content, _content_type, _final_url, _via = fetch_url(ACHIEVEMENTS_URL, use_wayback=False)
    text = extract_pdf(content)
    lines = text.splitlines()
    parsed_bachelor = _parse_achievement_table(
        lines,
        "При приеме на обучение по программам бакалавриата",
        "При приеме на обучение по программам магистратуры",
    )
    parsed_master = _parse_achievement_table(
        lines,
        "При приеме на обучение по программам магистратуры",
        "3. При приеме на обучение по программам бакалавриата",
    )
    parsed_target = _parse_achievement_table(
        lines,
        "3. При приеме на обучение по программам бакалавриата",
        "4. Перечень индивидуальных достижений",
    )
    if not parsed_bachelor and not parsed_master and (previous.get("bachelor") or previous.get("master")):
        raise RuntimeError("не удалось извлечь достижения из PDF; сохранён прежний каталог")
    payload = {
        "generated_at": utc_now(),
        "source": ACHIEVEMENTS_URL,
        "max_bachelor": 10,
        "max_master": 50,
        "bachelor": parsed_bachelor or previous.get("bachelor", []),
        "master": parsed_master or previous.get("master", []),
        "target": parsed_target or previous.get("target", []),
    }
    config.ACHIEVEMENTS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def load_achievements_catalog() -> dict[str, Any]:
    return _load_json_catalog(
        config.ACHIEVEMENTS_PATH,
        {"bachelor": [], "master": [], "target": [], "max_bachelor": 10, "max_master": 50},
    )


MIN_SCORES_URL = (
    "https://sibsutis.ru/applicants/documents/Прием%202026/"
    "Правила%20приёма/Минимальные%20баллы%202026%20на%20сайт.pdf"
)


def _parse_min_score_section(lines: list[str]) -> list[dict[str, Any]]:
    subjects: list[dict[str, Any]] = []
    buffer: list[str] = []
    for raw in lines:
        line = " ".join(raw.split())
        if not line:
            continue
        if line.startswith(("Для ", "Минимальн", "*", "образования (ВО)")):
            continue
        inline = re.match(r"^(.+?)\s+(\d{1,3})$", line)
        if inline:
            subject = inline.group(1).strip(" .")
            if subject:
                subjects.append({"subject": subject, "points": int(inline.group(2))})
            buffer = []
            continue
        if re.fullmatch(r"\d{1,3}", line):
            subject = " ".join(buffer).strip(" .")
            if subject:
                subjects.append({"subject": subject, "points": int(line)})
            buffer = []
            continue
        if len(line) > 2:
            buffer.append(line)
    return subjects


def build_min_scores_catalog() -> dict[str, Any]:
    """Скачивает таблицу минимальных баллов и сохраняет её в JSON.

    Если разбор PDF дал пустые разделы, сохраняет прежние значения.
    """
    previous = load_min_scores_catalog()
    content, _content_type, _final_url, _via = fetch_url(MIN_SCORES_URL, use_wayback=False)
    text = extract_pdf(content)
    lines = text.splitlines()
    normalized = [" ".join(line.split()) for line in lines]

    soo_start = next(
        (i for i, line in enumerate(normalized) if "среднего общего образования" in line),
        0,
    )
    spo_start = next(
        (i for i, line in enumerate(normalized) if "среднего профессионального образования" in line),
        len(normalized),
    )
    master_start = next(
        (i for i, line in enumerate(normalized) if "программам магистратуры" in line),
        len(normalized),
    )

    parsed_soo = _parse_min_score_section(normalized[soo_start:spo_start])
    parsed_spo = _parse_min_score_section(normalized[spo_start:master_start])
    master_found: int | None = None
    for line in normalized[master_start:]:
        match = re.search(r"(\d{2,3})\s*балл", line)
        if match:
            master_found = int(match.group(1))
            break

    if not parsed_soo and not parsed_spo and (previous.get("soo") or previous.get("spo")):
        raise RuntimeError("не удалось извлечь минимальные баллы из PDF; сохранён прежний каталог")

    payload = {
        "generated_at": utc_now(),
        "source": MIN_SCORES_URL,
        "soo": parsed_soo or previous.get("soo", []),
        "spo": parsed_spo or previous.get("spo", []),
        "master": master_found if master_found is not None else int(previous.get("master", 35) or 35),
    }
    config.MIN_SCORES_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def load_min_scores_catalog() -> dict[str, Any]:
    return _load_json_catalog(config.MIN_SCORES_PATH, {"soo": [], "spo": [], "master": 35})


SITE_PAGES: list[tuple[str, str, str]] = [
    ("https://sibsutis.ru/abitur/priem.php", "Поступление", "Приёмная комиссия 2026"),
    (
        "https://sibsutis.ru/abitur/kak-postupit/vstupitelnye-ispytaniya-v-sibguti.php",
        "Поступление",
        "Вступительные испытания",
    ),
    ("https://sibsutis.ru/abitur/pravila-priema/", "Поступление", "Правила приёма"),
    ("https://sibsutis.ru/abitur/bachelor/", "Поступление", "Правила приёма: приложения"),
    ("https://sibsutis.ru/abitur/faq.php", "Поступление", "FAQ абитуриента"),
    ("https://sibsutis.ru/abitur/kak-postupit/podayte-dokumenty.php", "Поступление", "Подача документов"),
    ("https://sibsutis.ru/abitur/kak-postupit/tselevaya-kvota/", "Поступление", "Целевая квота"),
    (
        "https://sibsutis.ru/abitur/kak-postupit/inostrannye-grazhdane/",
        "Поступление",
        "Иностранные граждане",
    ),
    ("https://sibsutis.ru/abitur/platnoe-obuchenie/", "Поступление", "Платное обучение"),
    ("https://sibsutis.ru/abitur/instituty/", "Институты", "Институты СибГУТИ"),
    (
        "https://sibsutis.ru/abitur/instituty/institut-bazovykh-distsiplin/",
        "Институты",
        "Институт базовых дисциплин",
    ),
    (
        "https://sibsutis.ru/abitur/instituty/institut-informatiki-i-vychislitelnoy-tekhniki/",
        "Институты",
        "Институт информатики и ВТ",
    ),
    (
        "https://sibsutis.ru/abitur/instituty/institut-telekommunikatsiy/",
        "Институты",
        "Институт телекоммуникаций",
    ),
    (
        "https://sibsutis.ru/abitur/instituty/institut--bezopasnosti/",
        "Институты",
        "Институт безопасности",
    ),
    ("https://sibsutis.ru/sveden/grants/", "Стипендии", "Стипендии и поддержка"),
    ("https://sibsutis.ru/sveden/education/", "Программы", "Образовательные программы"),
    ("https://sibsutis.ru/abitur/konkursnye-spiski/", "Поступление", "Конкурсные списки"),
    ("https://sibsutis.ru/abitur/obshchezhitiya/", "Общежития", "Общежития и кампусы"),
    ("https://sibsutis.ru/abitur/oplata-za-obshchezhitie.php", "Общежития", "Оплата за общежитие"),
    ("https://portal.sibsutis.ru/abitur/profile/", "Программы", "Каталог направлений и профилей"),
    (
        "https://sibsutis.ru/applicants/documents/%D0%9F%D1%80%D0%B8%D0%B5%D0%BC%202026/"
        "%D0%98%D1%82%D0%BE%D0%B3%D0%B8%20%D0%BF%D1%80%D0%B8%D0%B5%D0%BC%D0%B0%202026.pdf",
        "Поступление",
        "Итоги приёма 2026",
    ),
    (
        "https://sibsutis.ru/students/info/%D0%9E%20%D1%81%D1%82%D0%B8%D0%BF%D0%B5%D0%BD%D0%B4"
        "%D0%B8%D0%B0%D0%BB%D1%8C%D0%BD%D0%BE%D0%BC%20%D0%BE%D0%B1%D0%B5%D1%81%D0%BF%D0%B5"
        "%D1%87%D0%B5%D0%BD%D0%B8%D0%B8/2026-2027/4-1836%D0%BE-26.pdf",
        "Стипендии",
        "Размер стипендий 2026/2027",
    ),
    (
        "https://sibsutis.ru/students/info/%D0%9E%20%D1%81%D1%82%D0%B8%D0%BF%D0%B5%D0%BD%D0%B4"
        "%D0%B8%D0%B0%D0%BB%D1%8C%D0%BD%D0%BE%D0%BC%20%D0%BE%D0%B1%D0%B5%D1%81%D0%BF%D0%B5"
        "%D1%87%D0%B5%D0%BD%D0%B8%D0%B8/4-1559%D0%BE-26.pdf",
        "Стипендии",
        "Специальная стипендия 2026",
    ),
    (
        "https://sibsutis.ru/students/info/%D0%9E%20%D0%BD%D0%B0%D0%BB%D0%B8%D1%87%D0%B8%D0%B8"
        "%20%D0%BE%D0%B1%D1%89%D0%B5%D0%B6%D0%B8%D1%82%D0%B8%D0%B9%2C%20%D0%BA%D0%BE%D0%BB"
        "%D0%B8%D1%87%D0%B5%D1%81%D1%82%D0%B2%D0%B5%20%D0%B6%D0%B8%D0%BB%D1%8B%D1%85%20%D0"
        "%BF%D0%BE%D0%BC%D0%B5%D1%89%D0%B5%D0%BD%D0%B8%D0%B9%20%D0%B2%20%D0%BE%D0%B1%D1%89"
        "%D0%B5%D0%B6%D0%B8%D1%82%D0%B8%D1%8F%D1%85%20%D0%B4%D0%BB%D1%8F%20%D0%B8%D0%BD%D0"
        "%BE%D0%B3%D0%BE%D1%80%D0%BE%D0%B4%D0%BD%D0%B8%D1%85%20%D0%BE%D0%B1%D1%83%D1%87%D0"
        "%B0%D1%8E%D1%89%D0%B8%D1%85%D1%81%D1%8F%2C%20%D1%84%D0%BE%D1%80%D0%BC%D0%B8%D1%80"
        "%D0%BE%D0%B2%D0%B0%D0%BD%D0%B8%D0%B8%20%D0%BF%D0%BB%D0%B0%D1%82%D1%8B%20%D0%B7%D0"
        "%B0%20%D0%BF%D1%80%D0%BE%D0%B6%D0%B8%D0%B2%D0%B0%D0%BD%D0%B8%D0%B5%20%D0%B2%20%D0"
        "%BE%D0%B1%D1%89%D0%B5%D0%B6%D0%B8%D1%82%D0%B8%D1%8F%D1%85/2026%20-%202027/%D0%9F"
        "%D1%80%D0%B8%D0%BA%D0%B0%D0%B7%20%D0%BE%D0%B1%20%D1%83%D1%82%D0%B2%D0%B5%D1%80%D0"
        "%B6%D0%B4%D0%B5%D0%BD%D0%B8%D0%B8%20%D1%80%D0%B0%D0%B7%D0%BC%D0%B5%D1%80%D0%B0%20"
        "%D0%BF%D0%BB%D0%B0%D1%82%D1%8B%20%D0%B7%D0%B0%20%D0%BF%D1%80%D0%BE%D0%B6%D0%B8%D0"
        "%B2%D0%B0%D0%BD%D0%B8%D0%B5%20%D0%B2%20%D0%BE%D0%B1%D1%89%D0%B5%D0%B6%D0%B8%D1%82"
        "%D0%B8%D1%8F%D1%85%20%D0%A1%D0%B8%D0%B1%D0%93%D0%A3%D0%A2%D0%98%20%D0%B4%D0%BB%D1"
        "%8F%20%D0%BE%D0%B1%D1%83%D1%87%D0%B0%D1%8E%D1%89%D0%B8%D1%85%D1%81%D1%8F.pdf",
        "Общежития",
        "Размер платы за проживание 2026/2027",
    ),
]

PDF_KEYWORDS = (
    "приложение 1",
    "приложение 2",
    "приложение 3",
    "приложение 4",
    "приложение 5",
    "приложение 6",
    "приложение 7",
    "приложение 8",
    "приложение 9",
    "приложение 10",
    "минимальные баллы",
    "количество бюджетных",
    "количество платных",
    "расписание вступительных",
    "памятка",
    "медосмотр",
    "соответствие направленности",
)


def _previous_sources(section: str) -> list[dict[str, str]]:
    """Ранее собранные источники раздела — запасной вариант, если сайт недоступен."""
    if not config.SITE_SOURCES_PATH.exists():
        return []
    try:
        payload = json.loads(config.SITE_SOURCES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    return [
        item
        for item in payload
        if isinstance(item, dict)
        and item.get("section") == section
        and item.get("url")
        and item.get("title")
    ]


def discover_site_sources() -> list[dict[str, str]]:
    """Собирает ссылки для автоматического обновления базы."""
    sources: dict[str, dict[str, str]] = {}

    def add(url: str, section: str, title: str) -> None:
        if url not in sources:
            sources[url] = {"url": url, "section": section, "title": title, "year": "2026"}

    for url, section, title in SITE_PAGES:
        add(url, section, title)

    try:
        program_links = discover_program_links()
    except Exception as exc:
        # Портал может не ответить: не теряем уже собранные ссылки на профили.
        print(f"program discovery warning: {exc}", file=sys.stderr)
        program_links = _previous_sources("Программы")

    for link in program_links:
        add(link["url"], "Программы", link["title"])

    try:
        content, _content_type, final_url, _via = fetch_url(config.BACHELOR_RULES_URL, use_wayback=False)
        soup = BeautifulSoup(decode_text(content), "html.parser")
        for anchor in soup.find_all("a", href=True):
            href = urljoin(final_url, anchor["href"])
            title = _clean_text(anchor)
            low = (href + " " + title).lower()
            if href.lower().endswith(".pdf") and any(key in low for key in PDF_KEYWORDS):
                add(href, "Поступление", title or Path(href).name)
    except Exception as exc:
        # Если страница приложений недоступна, сохраняем ранее найденные PDF.
        print(f"PDF discovery warning: {exc}", file=sys.stderr)
        for item in _previous_sources("Поступление"):
            if item["url"].lower().endswith(".pdf"):
                add(item["url"], "Поступление", item["title"])

    payload = sorted(sources.values(), key=lambda item: (item["section"], item["title"]))
    config.SITE_SOURCES_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return payload


# --- Снапшоты базы знаний ------------------------------------------------

BACKUP_ID_RE = re.compile(r"^db_[0-9]{8}_[0-9]{6}_[a-f0-9]{6}$")


def read_app_settings() -> dict[str, Any]:
    """Настройки, которые можно менять без перезапуска (папка источников)."""
    default_folder = config.DOCS_DIR.relative_to(config.BASE_DIR).as_posix()
    if not config.APP_SETTINGS_PATH.exists():
        return {"folder": default_folder}
    try:
        payload = json.loads(config.APP_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"folder": default_folder}
    if not isinstance(payload, dict):
        return {"folder": default_folder}
    payload.setdefault("folder", default_folder)
    folder = str(payload.get("folder") or default_folder).replace("\\", "/")
    candidate = Path(folder)
    if candidate.is_absolute():
        try:
            folder = candidate.resolve().relative_to(config.BASE_DIR.resolve()).as_posix()
        except ValueError:
            folder = default_folder
    payload["folder"] = folder
    return payload


def write_app_settings(patch: dict[str, Any]) -> dict[str, Any]:
    settings = read_app_settings()
    settings.update({key: value for key, value in patch.items() if value is not None})
    config.APP_SETTINGS_PATH.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return settings


SITE_REBUILD_MIN_INTERVAL_SEC = 24 * 60 * 60


def site_rebuild_cooldown() -> dict[str, Any]:
    """Проверяет суточный лимит на полную пересборку базы с сайта.

    Полная пересборка обходит десятки страниц и PDF, поэтому повторный запуск
    раньше чем через сутки блокируется, чтобы не нагружать сайт вуза.
    """
    raw = str(read_app_settings().get("last_site_rebuild_at") or "").strip()
    last_dt: datetime | None = None
    if raw:
        try:
            last_dt = datetime.fromisoformat(raw)
        except ValueError:
            last_dt = None
        if last_dt is not None and last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
    if last_dt is None:
        return {"last_at": "", "next_at": "", "remaining_sec": 0, "allowed": True}

    next_dt = last_dt + timedelta(seconds=SITE_REBUILD_MIN_INTERVAL_SEC)
    remaining = int((next_dt - datetime.now(timezone.utc)).total_seconds())
    if remaining <= 0:
        return {"last_at": raw, "next_at": "", "remaining_sec": 0, "allowed": True}
    return {
        "last_at": raw,
        "next_at": next_dt.isoformat(timespec="seconds"),
        "remaining_sec": remaining,
        "allowed": False,
    }


def resolve_kb_folder(folder: str) -> Path:
    """Разрешает папку источников только внутри data/ (без выхода за пределы проекта)."""
    raw = (folder or "").strip()
    if not raw:
        raise ValueError("путь к папке не задан")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = config.BASE_DIR / candidate
    resolved = candidate.resolve()
    data_root = config.DATA_DIR.resolve()
    if not resolved.is_relative_to(data_root):
        raise ValueError("папка должна находиться внутри data/")
    if not resolved.is_dir():
        raise ValueError(f"папка не найдена: {resolved}")
    return resolved


def _read_stats() -> dict[str, Any]:
    if not config.STATS_PATH.exists():
        return {}
    try:
        payload = json.loads(config.STATS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _active_database() -> dict[str, Any]:
    stats = _read_stats()
    settings = read_app_settings()
    return {
        "id": "active",
        "name": "Активная база",
        "created_at": stats.get("generated_at") or utc_now(),
        "docs": int(stats.get("unique_docs", 0)),
        "chunks": int(stats.get("chunks", 0)),
        "chars": int(stats.get("total_chars", 0)),
        "folder": settings.get("folder", config.DOCS_DIR.relative_to(config.BASE_DIR).as_posix()),
        "has_kb": config.KB_PATH.exists(),
    }


def _backup_meta(backup_dir: Path) -> dict[str, Any] | None:
    meta_path = backup_dir / "meta.json"
    if not meta_path.exists():
        return None
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def list_backups() -> list[dict[str, Any]]:
    backups: list[dict[str, Any]] = []
    if not config.BACKUPS_DIR.exists():
        return backups
    for backup_dir in config.BACKUPS_DIR.iterdir():
        if not backup_dir.is_dir():
            continue
        meta = _backup_meta(backup_dir)
        if meta and BACKUP_ID_RE.match(str(meta.get("id", ""))):
            backups.append(meta)
    backups.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
    return backups


def create_backup(name: str = "") -> dict[str, Any]:
    """Создаёт согласованный снапшот kb.sqlite + embeddings.npy и метаданные."""
    if not config.KB_PATH.exists() or not config.EMBEDDINGS_PATH.exists():
        raise RuntimeError("база знаний ещё не собрана: нечего копировать")

    now = datetime.now(timezone.utc)
    backup_id = f"db_{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    backup_dir = config.BACKUPS_DIR / backup_id
    backup_dir.mkdir(parents=True, exist_ok=False)

    source = sqlite3.connect(config.KB_PATH)
    target = sqlite3.connect(backup_dir / "kb.sqlite")
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()

    for filename in ("embeddings.npy", "registry.json", "stats.json"):
        source_path = config.DATA_DIR / filename
        if source_path.exists():
            shutil.copy2(source_path, backup_dir / filename)

    stats = _read_stats()
    meta = {
        "id": backup_id,
        "name": (name or "").strip() or f"Копия · {now.strftime('%d.%m.%Y %H:%M')}",
        "created_at": now.isoformat(timespec="seconds"),
        "docs": int(stats.get("unique_docs", 0)),
        "chunks": int(stats.get("chunks", 0)),
        "chars": int(stats.get("total_chars", 0)),
    }
    (backup_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return meta


def _backup_dir(backup_id: str) -> Path:
    if not BACKUP_ID_RE.match(backup_id or ""):
        raise ValueError("некорректный идентификатор резервной копии")
    backup_dir = (config.BACKUPS_DIR / backup_id).resolve()
    if backup_dir.parent != config.BACKUPS_DIR.resolve():
        raise ValueError("некорректный путь резервной копии")
    if not backup_dir.is_dir():
        raise FileNotFoundError(backup_id)
    return backup_dir


def restore_backup(backup_id: str) -> dict[str, Any]:
    """Заменяет живую базу снапшотом через временные файлы и atomic replace."""
    backup_dir = _backup_dir(backup_id)
    required = (backup_dir / "kb.sqlite", backup_dir / "embeddings.npy")
    if not all(path.exists() for path in required):
        raise RuntimeError("в резервной копии нет kb.sqlite или embeddings.npy")

    for filename in ("kb.sqlite", "embeddings.npy", "registry.json", "stats.json"):
        source_path = backup_dir / filename
        if not source_path.exists():
            continue
        target_path = config.DATA_DIR / filename
        temp_path = target_path.with_name(target_path.name + ".restore-tmp")
        shutil.copy2(source_path, temp_path)
        temp_path.replace(target_path)

    return _active_database()


def delete_backup(backup_id: str) -> None:
    backup_dir = _backup_dir(backup_id)
    shutil.rmtree(backup_dir)


def database_status() -> dict[str, Any]:
    settings = read_app_settings()
    return {
        "active": _active_database(),
        "backups": list_backups(),
        "folder": settings.get("folder", config.DOCS_DIR.relative_to(config.BASE_DIR).as_posix()),
        "embed": {
            "endpoint": config.EMBED_BASE_URL or None,
            "model": config.EMBED_MODEL or None,
            "configured": bool(config.EMBED_BASE_URL and config.EMBED_API_KEY and config.EMBED_MODEL),
        },
        "admin": {
            "auth_enabled": bool(config.ADMIN_TOKEN),
            "rate_limit_max": config.ADMIN_RATE_LIMIT_MAX,
            "rate_limit_window_sec": config.ADMIN_RATE_LIMIT_WINDOW_SEC,
        },
    }


# --- Фоновые задачи и автообновление --------------------------------------

_JOB_GUARD = threading.Lock()
_JOB_STATE: dict[str, Any] = {
    "id": "",
    "kind": "",
    "status": "idle",
    "progress": 0,
    "step": "",
    "started_at": "",
    "finished_at": "",
    "duration_ms": 0,
    "result": None,
    "error": "",
}
_SCHEDULER_THREAD: threading.Thread | None = None


def job_status() -> dict[str, Any]:
    with _JOB_GUARD:
        return copy.deepcopy(_JOB_STATE)


def _persist_job_state() -> None:
    """Сохраняет снимок задачи, чтобы её статус пережил перезапуск сервера."""
    with _JOB_GUARD:
        snapshot = copy.deepcopy(_JOB_STATE)
    if snapshot["status"] == "idle":
        return
    try:
        write_app_settings({"last_job": snapshot})
    except OSError as exc:
        print(f"persist job warning: {exc}", file=sys.stderr)


def _restore_job_state() -> None:
    """Восстанавливает последнюю задачу; прерванную рестартом помечает `interrupted`."""
    persisted = read_app_settings().get("last_job")
    if not isinstance(persisted, dict) or not persisted.get("id"):
        return
    state = copy.deepcopy(_JOB_STATE)
    state.update({key: persisted[key] for key in state if key in persisted})
    interrupted = state.get("status") == "running"
    if interrupted:
        state.update(
            {
                "status": "interrupted",
                "finished_at": utc_now(),
                "error": "Задача прервана перезапуском сервера",
            }
        )
    with _JOB_GUARD:
        _JOB_STATE.update(state)
    if interrupted:
        patch: dict[str, Any] = {"last_job": state}
        if state.get("kind") == "site_rebuild":
            # Прерванная пересборка не должна блокировать повторный запуск на сутки.
            patch["last_site_rebuild_at"] = ""
        write_app_settings(patch)


def _job_progress(done: int, total: int, step: str = "") -> None:
    total = max(1, int(total))
    percent = max(0, min(100, int(done / total * 100)))
    with _JOB_GUARD:
        _JOB_STATE["progress"] = percent
        _JOB_STATE["step"] = step


def start_job(kind: str, target: Callable[[Callable[..., None]], Any]) -> dict[str, Any]:
    """Запускает фоновую задачу; одновременно выполняется только одна."""
    with _JOB_GUARD:
        if _JOB_STATE["status"] == "running":
            raise RuntimeError(f"уже выполняется задача: {_JOB_STATE['kind']}")
        _JOB_STATE.update(
            {
                "id": uuid.uuid4().hex[:12],
                "kind": kind,
                "status": "running",
                "progress": 0,
                "step": "Запуск",
                "started_at": utc_now(),
                "finished_at": "",
                "duration_ms": 0,
                "result": None,
                "error": "",
            }
        )
    _persist_job_state()
    thread = threading.Thread(target=_run_job, args=(kind, target), name=f"job-{kind}", daemon=True)
    thread.start()
    return job_status()


def _run_job(kind: str, target: Callable[[Callable[..., None]], Any]) -> None:
    started = time.perf_counter()
    try:
        with DB_LOCK:
            result = target(_job_progress)
        with _JOB_GUARD:
            _JOB_STATE.update(
                {
                    "status": "done",
                    "progress": 100,
                    "step": "Готово",
                    "finished_at": utc_now(),
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                    "result": result,
                }
            )
        _persist_job_state()
    except Exception as exc:
        with _JOB_GUARD:
            _JOB_STATE.update(
                {
                    "status": "error",
                    "finished_at": utc_now(),
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        _persist_job_state()


def _preserve_failed_docs_from_backup(
    backup_id: str,
    failed_urls: set[str],
    errors: list[dict[str, Any]],
    skipped: int,
) -> tuple[int, dict[str, Any] | None]:
    """Переносит из резервной копии документы и эмбеддинги тех URL, которые не удалось обновить."""
    if not backup_id or not failed_urls:
        return 0, None
    backup_dir = _backup_dir(backup_id)
    backup_kb = backup_dir / "kb.sqlite"
    backup_emb = backup_dir / "embeddings.npy"
    if not backup_kb.exists() or not backup_emb.exists():
        return 0, None

    backup_matrix = np.load(backup_emb).astype(np.float32)
    backup_conn = sqlite3.connect(f"file:{backup_kb}?mode=ro", uri=True)
    backup_conn.row_factory = sqlite3.Row
    live_conn = connect_db()
    preserved = 0
    try:
        chunk_rows = backup_conn.execute("SELECT id FROM chunks ORDER BY id").fetchall()
        id_to_pos = {int(row["id"]): idx for idx, row in enumerate(chunk_rows)}
        if len(id_to_pos) != backup_matrix.shape[0]:
            return 0, None

        live_urls = {str(row["url"]) for row in live_conn.execute("SELECT url FROM documents")}
        live_hashes = existing_hashes(live_conn)
        backup_docs = backup_conn.execute(
            """SELECT doc_id, title, url, section, fetched_at, year_version,
                      format, sha256, chars, retrieved_via
               FROM documents"""
        ).fetchall()

        for doc_row in backup_docs:
            url = str(doc_row["url"])
            sha = str(doc_row["sha256"])
            if url not in failed_urls or url in live_urls or sha in live_hashes:
                continue
            chunk_records = backup_conn.execute(
                "SELECT id, heading, text FROM chunks WHERE doc_id = ? ORDER BY idx",
                (doc_row["doc_id"],),
            ).fetchall()
            if not chunk_records:
                continue
            positions = [id_to_pos.get(int(chunk["id"]), -1) for chunk in chunk_records]
            if any(pos < 0 for pos in positions):
                continue
            vectors = backup_matrix[positions]
            chunks_payload = [
                {"heading": str(chunk["heading"] or ""), "text": str(chunk["text"] or "")}
                for chunk in chunk_records
            ]
            store_document(live_conn, dict(doc_row), chunks_payload)
            append_embeddings(vectors)
            live_urls.add(url)
            live_hashes.add(sha)
            preserved += 1

        if preserved == 0:
            return 0, None

        verify_index_consistency(live_conn)
        registry = write_registry(live_conn)
        stats = write_stats(live_conn, registry, errors, skipped)
        return preserved, stats
    finally:
        backup_conn.close()
        live_conn.close()


def run_site_rebuild(progress: Callable[..., None] | None = None) -> dict[str, Any]:
    """Находит ссылки и полностью пересобирает базу из сайта.

    Перед пересборкой делает резервную копию. Если часть URL не ответила,
    их прежние документы и векторы подтягиваются из копии. Если сайт не отдал
    ни одного документа или итоговый объём меньше половины прежнего — откат.
    """
    before_docs = int(_read_stats().get("unique_docs") or 0)
    previous_stamp = str(read_app_settings().get("last_site_rebuild_at") or "")
    # Отметку ставим сразу: суточный лимит защищает сайт и от повторных неудачных попыток.
    write_app_settings(
        {"last_site_rebuild_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    )

    try:
        backup: dict[str, Any] | None = None
        if config.KB_PATH.exists() and config.EMBEDDINGS_PATH.exists():
            if progress is not None:
                progress(0, 1, "Резервная копия перед пересборкой")
            backup = create_backup("Перед пересборкой с сайта")

        if progress is not None:
            progress(0, 1, "Поиск ссылок на сайте")
        sources = discover_site_sources()
        stats = run_urls(config.SITE_SOURCES_PATH, use_wayback=False, progress=progress)

        fresh_docs = int(stats.get("unique_docs") or 0)
        if fresh_docs == 0:
            if backup is not None:
                restore_backup(str(backup["id"]))
            raise RuntimeError(
                f"сайт отдал 0 документов вместо {before_docs} — база восстановлена из копии"
            )

        preserved_docs = 0
        errors = list(stats.get("errors", []))
        failed_urls = {str(err.get("url") or "") for err in errors if isinstance(err, dict) and err.get("url")}
        if backup is not None and failed_urls:
            preserved_docs, updated_stats = _preserve_failed_docs_from_backup(
                str(backup["id"]), failed_urls, errors, int(stats.get("skipped") or 0)
            )
            if updated_stats is not None:
                stats = updated_stats

        after_docs = int(stats.get("unique_docs") or 0)
        degraded = before_docs > 0 and after_docs < before_docs * 0.5
        if degraded:
            if backup is not None:
                restore_backup(str(backup["id"]))
            raise RuntimeError(
                f"сайт отдал {after_docs} документов вместо {before_docs} — "
                "база восстановлена из копии"
            )

        return {
            "sources": len(sources),
            "stats": stats,
            "backup_id": str(backup["id"]) if backup else "",
            "preserved_docs": preserved_docs,
        }
    except Exception:
        # Неудачная попытка не должна блокировать повторный запуск на сутки.
        write_app_settings({"last_site_rebuild_at": previous_stamp})
        raise


def _catalog_body_hash(path: Path) -> str:
    """Хэш JSON без служебных полей: позволяет понять, изменились ли данные."""
    if not path.exists():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key not in {"generated_at", "changed"}}
    return sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _file_hash(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return sha256_text(path.read_text(encoding="utf-8"))
    except OSError:
        return ""


def run_autoupdate(progress: Callable[..., None] | None = None) -> dict[str, Any]:
    """Обновляет каталог, достижения, минимальные баллы и ссылки парсинга."""
    started_at = utc_now()
    targets: list[tuple[str, str, Callable[[], dict[str, Any]], Path]] = [
        ("programs", "Каталог направлений", build_program_catalog, config.PROGRAMS_PATH),
        ("achievements", "Индивидуальные достижения", build_achievements_catalog, config.ACHIEVEMENTS_PATH),
        ("min_scores", "Минимальные баллы", build_min_scores_catalog, config.MIN_SCORES_PATH),
    ]
    total = len(targets) + 1
    steps: list[dict[str, Any]] = []

    for index, (key, title, builder, path) in enumerate(targets, start=1):
        if progress is not None:
            progress(index, total, title)
        before = _catalog_body_hash(path)
        try:
            payload = builder()
            changed = _catalog_body_hash(path) != before
            steps.append(
                {
                    "key": key,
                    "title": title,
                    "status": "ok",
                    "changed": changed,
                    "generated_at": payload.get("generated_at", ""),
                }
            )
        except Exception as exc:
            steps.append(
                {"key": key, "title": title, "status": "error", "error": f"{type(exc).__name__}: {exc}"}
            )

    if progress is not None:
        progress(total, total, "Ссылки для парсинга")
    before_sources = _file_hash(config.SITE_SOURCES_PATH)
    try:
        sources = discover_site_sources()
        steps.append(
            {
                "key": "site_sources",
                "title": "Ссылки для парсинга",
                "status": "ok",
                "changed": _file_hash(config.SITE_SOURCES_PATH) != before_sources,
                "count": len(sources),
            }
        )
    except Exception as exc:
        steps.append(
            {
                "key": "site_sources",
                "title": "Ссылки для парсинга",
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )

    changed = [step["title"] for step in steps if step.get("changed")]
    status = "ok" if all(step["status"] == "ok" for step in steps) else "partial"
    summary = {
        "started_at": started_at,
        "finished_at": utc_now(),
        "status": status,
        "changed": changed,
        "steps": steps,
    }

    cfg = _autoupdate_config()
    cfg["last_run"] = summary["finished_at"]
    cfg["last_status"] = status
    cfg["last_summary"] = summary
    write_app_settings({"autoupdate": cfg})
    return summary


def _autoupdate_config() -> dict[str, Any]:
    settings = read_app_settings()
    raw = settings.get("autoupdate")
    raw = raw if isinstance(raw, dict) else {}
    try:
        interval = int(raw.get("interval_hours", 24))
    except (TypeError, ValueError):
        interval = 24
    return {
        "enabled": bool(raw.get("enabled", False)),
        "interval_hours": max(1, min(168, interval)),
        "last_run": str(raw.get("last_run") or ""),
        "last_status": str(raw.get("last_status") or ""),
        "last_summary": raw.get("last_summary") or {},
    }


def autoupdate_status() -> dict[str, Any]:
    cfg = _autoupdate_config()
    next_run = ""
    due = True
    if cfg["last_run"]:
        try:
            last_run = datetime.fromisoformat(cfg["last_run"])
            next_dt = last_run + timedelta(hours=cfg["interval_hours"])
            next_run = next_dt.isoformat(timespec="seconds")
            due = datetime.now(timezone.utc) >= next_dt
        except ValueError:
            due = True
    cfg["next_run"] = next_run
    cfg["due"] = due
    cfg["job"] = job_status()
    return cfg


def configure_autoupdate(enabled: bool, interval_hours: int) -> dict[str, Any]:
    cfg = _autoupdate_config()
    cfg["enabled"] = bool(enabled)
    try:
        hours = int(interval_hours)
    except (TypeError, ValueError):
        hours = 24
    cfg["interval_hours"] = max(1, min(168, hours))
    write_app_settings({"autoupdate": cfg})
    return autoupdate_status()


def start_autoupdate_scheduler() -> bool:
    """Запускает фоновый поток планировщика (идемпотентно)."""
    global _SCHEDULER_THREAD
    with _JOB_GUARD:
        if _SCHEDULER_THREAD is not None and _SCHEDULER_THREAD.is_alive():
            return False
        _SCHEDULER_THREAD = threading.Thread(
            target=_scheduler_loop, name="autoupdate", daemon=True
        )
        _SCHEDULER_THREAD.start()
        return True


def _scheduler_loop() -> None:
    while True:
        time.sleep(30)
        try:
            cfg = autoupdate_status()
            if not cfg["enabled"] or not cfg["due"]:
                continue
            if cfg["job"]["status"] == "running":
                continue
            start_job("autoupdate", run_autoupdate)
        except Exception as exc:
            print(f"autoupdate warning: {exc}", file=sys.stderr)


def check_kb() -> tuple[bool, str]:
    """Проверяет, что база пригодна для поиска: есть чанки, векторы (если есть) совпадают."""
    if not config.KB_PATH.exists():
        return False, "нет data/kb.sqlite"
    try:
        connection = sqlite3.connect(f"file:{config.KB_PATH}?mode=ro", uri=True)
        try:
            chunks = connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            connection.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return False, f"база повреждена: {exc}"
    if chunks == 0:
        return False, "в базе нет чанков"
    if config.EMBEDDINGS_PATH.exists():
        try:
            vectors = int(np.load(config.EMBEDDINGS_PATH).shape[0])
        except Exception as exc:
            return False, f"embeddings.npy не читается: {exc}"
        if vectors != chunks:
            return False, f"чанков {chunks}, векторов {vectors}"
    return True, f"чанков {chunks}"


def main(argv: Iterable[str] | None = None) -> int:
    # Консоль Windows по умолчанию cp1252: без этого русский --help падает.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    parser = argparse.ArgumentParser(description="Сбор базы знаний СибГУТИ")
    parser.add_argument("--urls", type=Path, help="JSON со списком источников (полная пересборка)")
    parser.add_argument("--dir", type=Path, help="Папка с локальными .md/.txt/.html/.pdf (полная пересборка)")
    parser.add_argument("--file", type=Path, help="Один локальный документ (HTML/PDF/TXT/MD)")
    parser.add_argument("--url", default="", help="Канонический URL документа для режима --file")
    parser.add_argument("--title", default="", help="Заголовок документа")
    parser.add_argument("--section", default="Прочее", help="Раздел базы знаний")
    parser.add_argument("--year", default="", help="Версия/год документа")
    parser.add_argument(
        "--no-wayback",
        action="store_true",
        help="Не использовать веб-архив при недоступности источника",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Продолжить сбор: не очищать базу и пропустить уже проиндексированные URL",
    )
    parser.add_argument(
        "--programs",
        action="store_true",
        help="Обновить каталог направлений с portal.sibsutis.ru",
    )
    parser.add_argument(
        "--site-sources",
        action="store_true",
        help="Собрать список ссылок сайта для автоматического обновления",
    )
    parser.add_argument(
        "--achievements",
        action="store_true",
        help="Обновить структурированный список индивидуальных достижений",
    )
    parser.add_argument(
        "--min-scores",
        action="store_true",
        help="Обновить структурированные минимальные баллы",
    )
    parser.add_argument(
        "--fts-only",
        action="store_true",
        help="Собрать локальный FTS5-индекс без обращения к API эмбеддингов",
    )
    parser.add_argument(
        "--check-kb",
        action="store_true",
        help="Проверить, что локальная база пригодна для поиска (код выхода 0 — да)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.check_kb:
        ok, reason = check_kb()
        print(("OK: " if ok else "FAIL: ") + reason)
        return 0 if ok else 1

    if args.programs:
        payload = build_program_catalog()
        print(f"Каталог направлений: {payload['count']} программ, ошибок={len(payload['errors'])}")
        for error in payload["errors"]:
            print(f"  ! {error['url']}: {error['error']}", file=sys.stderr)
        return 0

    if args.achievements:
        payload = build_achievements_catalog()
        print(
            f"Достижения: бакалавриат={len(payload['bachelor'])}, "
            f"магистратура={len(payload['master'])}, целевые={len(payload.get('target', []))}"
        )
        return 0

    if args.min_scores:
        payload = build_min_scores_catalog()
        print(
            f"Минимальные баллы: СОО={len(payload['soo'])}, "
            f"СПО/ВО={len(payload['spo'])}, магистратура={payload['master']}"
        )
        return 0

    if args.site_sources:
        sources = discover_site_sources()
        print(f"Источников найдено: {len(sources)} → {config.SITE_SOURCES_PATH}")
        return 0

    if not args.urls and not args.file and not args.dir:
        parser.error(
            "укажите --dir, --urls, --file, --programs, --site-sources, --achievements или --min-scores"
        )

    started = time.perf_counter()
    if args.dir:
        if not args.dir.is_dir():
            print(f"папка не найдена: {args.dir}", file=sys.stderr)
            return 2
        stats = run_directory(args.dir, with_embeddings=not args.fts_only)
    elif args.urls:
        stats = run_urls(args.urls, use_wayback=not args.no_wayback, resume=args.resume)
    else:
        if not args.file.exists():
            print(f"файл не найден: {args.file}", file=sys.stderr)
            return 2
        stats = run_single_file(
            args.file,
            url=args.url,
            title=args.title or args.file.stem,
            section=args.section,
            year=args.year or None,
            use_wayback=not args.no_wayback,
        )

    elapsed = time.perf_counter() - started
    print(
        f"Готово за {elapsed:.1f} c: документов={stats['unique_docs']}, "
        f"чанков={stats['chunks']}, ошибок={stats['error_count']}"
    )
    for error in stats["errors"]:
        print(f"  ! {error['url']}: {error['error']}", file=sys.stderr)
    return 0


_restore_job_state()


if __name__ == "__main__":
    raise SystemExit(main())
