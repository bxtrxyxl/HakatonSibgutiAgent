"""Сбор, очистка и индексация базы знаний СибГУТИ.

Режимы CLI:
    python -m src.ingest --urls data/seed_urls.json
    python -m src.ingest --file path/to/doc.html --url https://... --title "..." --section "..."
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import numpy as np
from bs4 import BeautifulSoup, NavigableString, Tag
from pypdf import PdfReader

from src import config

# --- Константы ------------------------------------------------------------

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


# --- Утилиты --------------------------------------------------------------

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

def _http_get(client: httpx.Client, url: str) -> httpx.Response:
    last_error: Exception | None = None
    for attempt in range(config.FETCH_RETRIES + 1):
        try:
            response = client.get(url)
            response.raise_for_status()
            return response
        except Exception as exc:
            last_error = exc
            if attempt < config.FETCH_RETRIES:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"fetch failed: {type(last_error).__name__}: {last_error}")


def fetch_url(url: str, use_wayback: bool = True) -> tuple[bytes, str, str, str]:
    """Возвращает (content, content_type, final_url, retrieved_via)."""
    headers = {"User-Agent": config.USER_AGENT, "Accept-Language": "ru,en;q=0.8"}
    with httpx.Client(timeout=config.FETCH_TIMEOUT, follow_redirects=True, headers=headers) as client:
        try:
            response = _http_get(client, url)
            ctype = response.headers.get("content-type", "")
            return response.content, ctype, str(response.url), "origin"
        except Exception as origin_error:
            if not use_wayback:
                raise RuntimeError(f"origin unavailable: {origin_error}") from origin_error
            archive_url = f"https://web.archive.org/web/2025/{url}"
            try:
                response = _http_get(client, archive_url)
            except Exception as archive_error:
                raise RuntimeError(
                    f"origin unavailable ({origin_error}); archive unavailable ({archive_error})"
                ) from archive_error
            text_probe = response.content[:4000].decode("utf-8", errors="ignore").lower()
            if "has not archived that url" in text_probe or "wayback machine doesn't have that page" in text_probe:
                raise RuntimeError(f"origin unavailable ({origin_error}); no archived copy") from None
            ctype = response.headers.get("content-type", "")
            return response.content, ctype, str(response.url), "wayback"


# --- HTML -> Markdown -----------------------------------------------------

def table_to_markdown(table: Tag) -> str:
    rows: list[list[str]] = []
    for tr in table.find_all("tr"):
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


def chunk_markdown(markdown: str, title: str, section: str) -> list[dict[str, str]]:
    """Режет Markdown на чанки, не разрывая строки таблиц."""
    blocks = _split_blocks(markdown)
    chunks: list[dict[str, str]] = []
    heading = ""
    current: list[str] = []
    current_len = 0
    seen: set[str] = set()

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
        if len(text) < config.MIN_CHUNK_SIZE and chunks:
            return
        key = sha256_text(text)
        if key in seen:
            return
        seen.add(key)
        chunks.append({"text": text, "heading": heading})

    for block in blocks:
        if block.startswith("#"):
            level = len(block) - len(block.lstrip("#"))
            if level <= 3:
                heading = block.lstrip("#").strip()
        if current and current_len + len(block) + 2 > config.CHUNK_SIZE:
            tail: list[str] = []
            if current and len(current[-1]) <= config.CHUNK_OVERLAP * 2:
                tail = [current[-1]]
            flush()
            current = list(tail)
            current_len = sum(len(item) + 2 for item in current)
        current.append(block)
        current_len += len(block) + 2
    flush()
    return chunks


# --- Эмбеддинги -----------------------------------------------------------

def _embedding_client() -> Any:
    from openai import OpenAI

    config.require_embeddings()
    return OpenAI(
        base_url=config.EMBED_BASE_URL,
        api_key=config.EMBED_API_KEY,
        timeout=config.REQUEST_TIMEOUT,
        max_retries=0,
    )


def embed_texts(texts: list[str], batch_size: int = 32) -> np.ndarray:
    client = _embedding_client()
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start: start + batch_size]
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = client.embeddings.create(model=config.EMBED_MODEL, input=batch)
                ordered = sorted(response.data, key=lambda item: item.index)
                vectors.extend([item.embedding for item in ordered])
                break
            except Exception as exc:
                last_error = exc
                time.sleep(2.0 * (attempt + 1))
        else:
            raise RuntimeError(f"embeddings failed after retries: {type(last_error).__name__}: {last_error}")
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


def connect_db(path: Path = config.KB_PATH) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
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
    store_document(connection, doc, chunks)
    known_hashes.add(digest)

    embed_inputs = [f"{title}\n{section}\n{chunk['text']}" for chunk in chunks]
    vectors = embed_texts(embed_inputs)
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


def write_registry(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = [
        dict(row)
        for row in connection.execute(
            """SELECT doc_id, title, url, section, fetched_at, year_version, format,
                      chars, chunk_count, retrieved_via
               FROM documents ORDER BY section, title"""
        )
    ]
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


def run_urls(seed_path: Path, use_wayback: bool = True) -> dict[str, Any]:
    sources = json.loads(seed_path.read_text(encoding="utf-8"))
    connection = connect_db()
    init_db(connection, rebuild=True)
    known = existing_hashes(connection)
    vectors: list[np.ndarray] = []
    errors: list[dict[str, str]] = []
    skipped = 0

    for index, source in enumerate(sources, start=1):
        url = source.get("url", "?")
        try:
            chunk_count, new_vectors = process_source(source, connection, known, use_wayback)
            if new_vectors is None:
                skipped += 1
                print(f"[{index}/{len(sources)}] skip (duplicate) {url}")
            else:
                vectors.append(new_vectors)
                print(f"[{index}/{len(sources)}] ok {chunk_count} chunks {url}")
        except Exception as exc:
            message = str(exc)
            errors.append({"url": url, "error": message, "stage": "fetch+extract"})
            print(f"[{index}/{len(sources)}] ERROR {url}: {message}", file=sys.stderr)

    sync_embeddings(vectors, rebuild=True)
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

    content = path.read_bytes()
    fmt = detect_format(url or path.name, "", filename=path.name)

    if fmt == "pdf":
        markdown = extract_pdf(content)
        if len(markdown) < 50:
            errors.append({"url": url or path.name, "error": "no_text_layer", "stage": "extract"})
            markdown = ""
    elif fmt in {"txt", "md"}:
        markdown = decode_text(content)
    else:
        html = decode_text(content)
        _, markdown = html_to_markdown(html)

    if not markdown:
        write_registry(connection)
        rows = [dict(r) for r in connection.execute("SELECT * FROM documents")]
        stats = write_stats(connection, rows, errors, skipped)
        connection.close()
        return stats

    digest = sha256_text(markdown)
    if digest in known:
        skipped += 1
    else:
        chunks = chunk_markdown(markdown, title, section)
        doc = {
            "doc_id": doc_id_for(url or path.name),
            "title": title,
            "url": url or path.name,
            "section": section,
            "fetched_at": utc_now(),
            "year_version": year or find_year_version(title, markdown[:4000]),
            "format": fmt,
            "sha256": digest,
            "chars": len(markdown),
            "retrieved_via": "local_file",
        }
        store_document(connection, doc, chunks)
        vectors.append(embed_texts([f"{title}\n{section}\n{c['text']}" for c in chunks]))

    sync_embeddings(vectors, rebuild=False)
    registry = write_registry(connection)
    stats = write_stats(connection, registry, errors, skipped)
    connection.close()
    return stats


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Сбор базы знаний СибГУТИ")
    parser.add_argument("--urls", type=Path, help="JSON со списком источников (полная пересборка)")
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
    args = parser.parse_args(list(argv) if argv is not None else None)

    if not args.urls and not args.file:
        parser.error("укажите --urls или --file")

    started = time.perf_counter()
    if args.urls:
        stats = run_urls(args.urls, use_wayback=not args.no_wayback)
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


if __name__ == "__main__":
    raise SystemExit(main())
