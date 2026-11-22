"""Гибридный поиск: SQLite FTS5 (BM25) + косинусное сходство (numpy) -> RRF."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from src import config

RRF_K = 60
NEAR_DUPLICATE_SIM = 0.97
MAX_QUERY_VECTOR_CACHE = 256
TOKEN_RE = re.compile(r"[0-9a-zA-Zа-яА-ЯёЁ]+(?:[.\-/][0-9a-zA-Zа-яА-ЯёЁ]+)*")


@dataclass
class Chunk:
    chunk_id: int
    doc_id: str
    text: str
    title: str
    url: str
    section: str
    year_version: str
    fetched_at: str
    score: float = 0.0
    fts_rank: int | None = None
    vec_rank: int | None = None

    def as_source(self) -> dict[str, str]:
        return {
            "title": self.title,
            "url": self.url,
            "section": self.section,
            "year_version": self.year_version,
        }


def sanitize_fts_query(query: str) -> str:
    """Строит безопасный FTS5-запрос: только термы/фразы, без операторов и кавычек."""
    tokens = TOKEN_RE.findall(query or "")
    parts: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        lowered = token.lower()
        if lowered in seen or len(lowered) < 2:
            continue
        seen.add(lowered)
        if re.search(r"[.\-/]", token):
            # Код вида 09.03.01 -> фраза "09 03 01"
            phrase = " ".join(re.findall(r"[0-9a-zA-Zа-яА-ЯёЁ]+", token))
            if phrase:
                parts.append(f'"{phrase}"')
        else:
            parts.append(f'"{lowered}"')
    return " OR ".join(parts)


class HybridRetriever:
    """Гибридный поиск по локальной базе знаний."""

    def __init__(
        self,
        kb_path: Path = config.KB_PATH,
        embeddings_path: Path = config.EMBEDDINGS_PATH,
        top_k: int = config.TOP_K,
    ) -> None:
        self.kb_path = Path(kb_path)
        self.embeddings_path = Path(embeddings_path)
        self.top_k = top_k
        self._embeddings: np.ndarray | None = None
        self._chunk_ids: np.ndarray | None = None
        self._id_to_pos: dict[int, int] | None = None
        self._load_lock = threading.Lock()
        self._query_cache: dict[str, np.ndarray] = {}
        self._query_cache_lock = threading.Lock()
        self._embedder = None

    # --- ресурсы ---------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if not self.kb_path.exists():
            raise FileNotFoundError(f"база знаний не найдена: {self.kb_path}. Запустите python -m src.ingest")
        connection = sqlite3.connect(f"file:{self.kb_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    def _load_embeddings(self) -> None:
        if self._embeddings is not None:
            return
        with self._load_lock:
            if self._embeddings is not None:
                return
            self._load_embeddings_locked()

    def _load_embeddings_locked(self) -> None:
        if not self.embeddings_path.exists():
            self._embeddings = np.zeros((0, 0), dtype=np.float32)
            self._chunk_ids = np.zeros((0,), dtype=np.int64)
            self._id_to_pos = {}
            return
        matrix = np.load(self.embeddings_path).astype(np.float32)
        connection = self._connect()
        try:
            ids = np.asarray([row["id"] for row in connection.execute("SELECT id FROM chunks ORDER BY id")])
        finally:
            connection.close()
        if ids.shape[0] != matrix.shape[0]:
            # Не роняем чат: без векторов поиск продолжит работать по FTS5.
            print(
                f"[retrieval] рассинхронизация индексов: чанков {ids.shape[0]}, "
                f"векторов {matrix.shape[0]}; поиск идёт только по FTS5",
                file=sys.stderr,
            )
            self._embeddings = np.zeros((0, 0), dtype=np.float32)
            self._chunk_ids = np.zeros((0,), dtype=np.int64)
            self._id_to_pos = {}
            return
        self._embeddings = matrix
        self._chunk_ids = ids
        self._id_to_pos = {int(chunk_id): position for position, chunk_id in enumerate(ids)}

    def _id_positions(self) -> dict[int, int]:
        self._load_embeddings()
        if self._id_to_pos is None:
            self._id_to_pos = (
                {int(chunk_id): position for position, chunk_id in enumerate(self._chunk_ids)}
                if self._chunk_ids is not None
                else {}
            )
        return self._id_to_pos

    def _query_vector(self, query: str) -> np.ndarray | None:
        if self._embeddings is None or self._embeddings.size == 0:
            return None
        cache_key = " ".join((query or "").split()).lower()
        with self._query_cache_lock:
            cached = self._query_cache.get(cache_key)
        if cached is not None:
            return cached
        if self._embedder is None:
            from src.ingest import embed_query

            self._embedder = embed_query
        if getattr(self._embedder, "__name__", "") == "embed_texts":
            raw = self._embedder([query])[0]
        else:
            raw = self._embedder(query)
        vector = np.asarray(raw, dtype=np.float32)
        norm = float(np.linalg.norm(vector))
        if norm > 0 and abs(norm - 1.0) > 1e-4:
            vector = vector / norm
        with self._query_cache_lock:
            if len(self._query_cache) >= MAX_QUERY_VECTOR_CACHE:
                oldest = next(iter(self._query_cache))
                self._query_cache.pop(oldest, None)
            self._query_cache[cache_key] = vector
        return vector

    # --- поиск -----------------------------------------------------------

    def _fts_search(self, connection: sqlite3.Connection, query: str, limit: int) -> list[int]:
        expression = sanitize_fts_query(query)
        if not expression:
            return []
        try:
            rows = connection.execute(
                """SELECT chunk_id FROM chunks_fts
                   WHERE chunks_fts MATCH ?
                   ORDER BY bm25(chunks_fts)
                   LIMIT ?""",
                (expression, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [int(row["chunk_id"]) for row in rows]

    def _vector_search(self, query: str, limit: int) -> list[int]:
        self._load_embeddings()
        try:
            vector = self._query_vector(query)
        except Exception as exc:
            print(f"[retrieval] query embedding fallback to FTS5: {type(exc).__name__}: {exc}", file=sys.stderr)
            return []
        if vector is None:
            return []
        scores = self._embeddings @ vector
        top = np.argsort(-scores)[:limit]
        return [int(self._chunk_ids[index]) for index in top if scores[index] > 0]

    def _fetch_chunks(self, connection: sqlite3.Connection, chunk_ids: list[int]) -> dict[int, sqlite3.Row]:
        if not chunk_ids:
            return {}
        placeholders = ",".join("?" for _ in chunk_ids)
        rows = connection.execute(
            f"""SELECT c.id AS id, c.doc_id AS doc_id, c.text AS text,
                       d.title AS title, d.url AS url, d.section AS section,
                       d.year_version AS year_version, d.fetched_at AS fetched_at
                FROM chunks c
                JOIN documents d ON d.doc_id = c.doc_id
                WHERE c.id IN ({placeholders})""",
            chunk_ids,
        ).fetchall()
        return {int(row["id"]): row for row in rows}

    def search(self, query: str, top_k: int | None = None) -> list[dict[str, Any]]:
        limit = top_k or self.top_k
        candidates = config.CANDIDATES_PER_SOURCE
        connection = self._connect()
        try:
            fts_ids = self._fts_search(connection, query, candidates)
            vec_ids = self._vector_search(query, candidates)

            scores: dict[int, float] = {}
            fts_rank: dict[int, int] = {}
            vec_rank: dict[int, int] = {}
            for rank, chunk_id in enumerate(fts_ids, start=1):
                scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank)
                fts_rank[chunk_id] = rank
            for rank, chunk_id in enumerate(vec_ids, start=1):
                scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank)
                vec_rank[chunk_id] = rank

            ordered = sorted(scores.items(), key=lambda item: -item[1])
            rows = self._fetch_chunks(connection, [chunk_id for chunk_id, _ in ordered])
        finally:
            connection.close()

        selected: list[Chunk] = []
        per_doc: dict[str, int] = {}
        id_to_pos = self._id_positions()

        for chunk_id, score in ordered:
            row = rows.get(chunk_id)
            if row is None:
                continue
            if per_doc.get(row["doc_id"], 0) >= config.MAX_CHUNKS_PER_DOC:
                continue
            if self._is_near_duplicate(chunk_id, selected, id_to_pos):
                continue
            selected.append(
                Chunk(
                    chunk_id=chunk_id,
                    doc_id=row["doc_id"],
                    text=row["text"],
                    title=row["title"],
                    url=row["url"],
                    section=row["section"],
                    year_version=row["year_version"] or "",
                    fetched_at=row["fetched_at"],
                    score=round(score, 6),
                    fts_rank=fts_rank.get(chunk_id),
                    vec_rank=vec_rank.get(chunk_id),
                )
            )
            per_doc[row["doc_id"]] = per_doc.get(row["doc_id"], 0) + 1
            if len(selected) >= limit:
                break

        return [
            {
                "chunk_id": chunk.chunk_id,
                "doc_id": chunk.doc_id,
                "text": chunk.text,
                "title": chunk.title,
                "url": chunk.url,
                "section": chunk.section,
                "year_version": chunk.year_version,
                "fetched_at": chunk.fetched_at,
                "score": chunk.score,
                "fts_rank": chunk.fts_rank,
                "vec_rank": chunk.vec_rank,
            }
            for chunk in selected
        ]

    def _is_near_duplicate(self, chunk_id: int, selected: list[Chunk], id_to_pos: dict[int, int]) -> bool:
        if not selected or self._embeddings is None or self._embeddings.size == 0:
            return False
        position = id_to_pos.get(chunk_id)
        if position is None:
            return False
        vector = self._embeddings[position]
        for chunk in selected:
            other_position = id_to_pos.get(chunk.chunk_id)
            if other_position is None:
                continue
            similarity = float(np.dot(vector, self._embeddings[other_position]))
            if similarity >= NEAR_DUPLICATE_SIM:
                return True
        return False


def format_context(chunks: list[dict[str, Any]]) -> str:
    """Собирает контекст для LLM в виде списка фрагментов."""
    blocks: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        year = chunk.get("year_version") or "не указан"
        blocks.append(
            f"[{index}] {chunk['title']} | раздел: {chunk['section']} | год: {year}\n{chunk['text']}"
        )
    return "\n\n".join(blocks)


# --- Подбор направлений по стеку и профессии ------------------------------

STOPWORDS = {
    "хочу", "хотел", "хотела", "работать", "работаю", "стать", "учиться", "поступить",
    "какие", "какой", "какое", "какую", "где", "что", "там", "это", "для", "или", "мне",
    "можно", "нужно", "лучше", "подбери", "подобрать", "направление", "направления",
    "профессия", "профессию", "стек", "технологии", "технология", "выпускники",
    "обучение", "обучения", "обучением", "обучении", "учат", "учит", "обучают", "изучают",
    "под", "на", "в", "с", "и", "по",
    "and", "the", "for", "with", "want", "work", "developer", "engineer",
}

SYNONYMS: dict[str, tuple[str, ...]] = {
    "python": ("python", "pandas", "numpy", "django", "flask", "fastapi"),
    "ml": ("машинное обучение", "ml", "нейронн", "data science", "искусственн", "deep learning", "машинн"),
    "ai": ("ai", "искусственный интеллект", "нейросет", "nlp", "компьютерное зрение"),
    "backend": ("backend", "бэкенд", "java", "go", "sql", "api", "микросервис", "серверн"),
    "frontend": ("frontend", "фронтенд", "typescript", "javascript", "react", "верстк", "web"),
    "devops": ("devops", "docker", "kubernetes", "k8s", "ci/cd", "linux", "облачн"),
    "security": ("безопасность", "криптограф", "защита информации", "информационная безопасность"),
    "robot": ("робот", "robot", "робототехник"),
    "media": ("медиа", "телерадиовещание", "видео", "мультимедиа", "телевидени"),
    "economy": ("экономик", "бизнес", "финанс", "менеджмент"),
    "advertising": ("реклам", "связи с общественностью", "маркетинг", "pr", "медиакоммуникац"),
    "radio": ("радиотехн", "радиоэлектрон", "радиоволн", "радиосистем"),
    "electronics": ("электроник", "наноэлектроник", "микроэлектроник", "схемотехник"),
    "data": ("данн", "sql", "аналитик", "big data", "хранилищ"),
    "network": ("сети", "сетев", "телекоммуникац", "tcp/ip", "маршрутизац", "инфокоммуникац", "ethernet"),
}


def _normalize(text: str) -> str:
    return str(text or "").lower().replace("ё", "е")


def _query_concepts(query: str) -> list[str]:
    normalized = _normalize(query)
    concepts: list[str] = []
    for name, terms in SYNONYMS.items():
        if any(term in normalized for term in terms):
            concepts.append(name)
    tokens = re.findall(r"[a-zа-я0-9+#./-]{3,}", normalized)
    for token in tokens:
        if token in STOPWORDS or token in concepts:
            continue
        if any(_concept_matches(name, token) for name in SYNONYMS):
            continue
        concepts.append(token)
    return concepts[:12]


def _concept_matches(concept: str, text: str) -> bool:
    haystack = _normalize(text)
    terms = SYNONYMS.get(concept, (concept,))
    for term in terms:
        normalized = _normalize(term)
        if normalized and normalized in haystack:
            return True
        stem = normalized[:5]
        if " " not in normalized and len(stem) >= 4 and stem in haystack:
            return True
    return False


def recommend_programs(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Подбирает направления по стеку/профессии и объясняет совпадение."""
    from src.ingest import load_program_catalog

    payload = load_program_catalog()
    programs = payload.get("programs") if isinstance(payload, dict) else None
    if not isinstance(programs, list) or not programs:
        return []

    concepts = _query_concepts(query)
    if not concepts:
        return []

    ranked: list[dict[str, Any]] = []
    for program in programs:
        if not isinstance(program, dict):
            continue
        disciplines = " ".join(program.get("disciplines") or [])
        professions = " ".join(program.get("professions") or [])
        title_text = " ".join(
            [str(program.get("profile", "")), str(program.get("direction", "")), str(program.get("institute", ""))]
        )
        description = str(program.get("description", ""))
        matched: list[str] = []
        discipline_hits = 0
        profession_hits = 0
        title_hits = 0
        for concept in concepts:
            strong = False
            if _concept_matches(concept, title_text):
                title_hits += 1
                strong = True
            if _concept_matches(concept, disciplines):
                discipline_hits += 1
                strong = True
            if _concept_matches(concept, professions):
                profession_hits += 1
                strong = True
            if strong:
                matched.append(concept)
            elif _concept_matches(concept, description):
                # Совпадение только в общем описании считаем слабым и не выносим в топ.
                continue

        if not matched:
            continue
        coverage = len(matched) / len(concepts)
        depth = min(1.0, (discipline_hits + profession_hits) / max(1, len(concepts) * 2))
        fit = round(35 + 45 * coverage + 15 * depth + min(3, title_hits))
        fit = max(35, min(97, fit))
        ranked.append(
            {
                "id": program.get("id", ""),
                "code": program.get("code", ""),
                "direction": program.get("direction", ""),
                "profile": program.get("profile", ""),
                "level": program.get("level", ""),
                "form": program.get("form", ""),
                "url": program.get("url", ""),
                "fit": fit,
                "matched": matched,
                "disciplines": (program.get("disciplines") or [])[:6],
                "professions": (program.get("professions") or [])[:4],
                "pass_score": program.get("pass_score"),
                "budget": program.get("budget"),
                "paid": program.get("paid"),
                "tuition": program.get("tuition", ""),
                "salary": program.get("salary", ""),
            }
        )

    ranked.sort(key=lambda item: (-item["fit"], -(item.get("pass_score") or 0)))
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in ranked:
        key = (str(item.get("code", "")), str(item.get("profile", "")))
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
        if len(unique) >= limit:
            break
    return unique


# --- Каталог приёмной кампании как контекст чата --------------------------
# Правая панель собирается из data/programs.json и data/min_scores.json,
# а RAG ищет только по data/docs. Чтобы чат не отвечал «такого направления нет»,
# когда панель его показывает, релевантные записи каталога добавляются в контекст.

DIRECTION_CODE_RE = re.compile(r"\b\d{2}[.\-]\d{2}[.\-]\d{2}\b")


def _catalog_programs() -> list[dict[str, Any]]:
    from src.ingest import load_program_catalog

    payload = load_program_catalog()
    programs = payload.get("programs") if isinstance(payload, dict) else None
    if not isinstance(programs, list):
        return []
    return [item for item in programs if isinstance(item, dict)]


def _best_form(programs: list[dict[str, Any]]) -> dict[str, Any]:
    """Из записей одного кода выбирает очную с известным проходным баллом."""
    with_score = [item for item in programs if int(item.get("pass_score") or 0) > 0]
    pool = with_score or programs
    full_time = [
        item
        for item in pool
        if "Очная" in str(item.get("form", "")) and "Заочная" not in str(item.get("form", ""))
    ]
    pool = full_time or pool
    return max(pool, key=lambda item: int(item.get("pass_score") or 0))


def match_program(query: str) -> dict[str, Any] | None:
    """Находит программу каталога по коду направления или названию."""
    programs = _catalog_programs()
    if not programs:
        return None

    normalized = _normalize(query)
    code_match = DIRECTION_CODE_RE.search(normalized)
    if code_match:
        code = code_match.group(0).replace("-", ".")
        same = [item for item in programs if str(item.get("code", "")) == code]
        if same:
            return _best_form(same)

    concepts = _query_concepts(query)
    if not concepts:
        return None

    best_code = ""
    best_hits = 0
    for program in programs:
        title = " ".join(
            str(program.get(key, "")) for key in ("direction", "profile", "institute")
        )
        hits = sum(1 for concept in concepts if _concept_matches(concept, title))
        if hits > best_hits:
            best_hits = hits
            best_code = str(program.get("code", ""))
    if not best_code or not best_hits:
        return None
    same = [item for item in programs if str(item.get("code", "")) == best_code]
    return _best_form(same)


def catalog_matches(query: str, limit: int = 6) -> list[dict[str, Any]]:
    """Записи каталога, релевантные вопросу (для контекста и источников)."""
    programs = _catalog_programs()
    if not programs:
        return []

    normalized = _normalize(query)
    codes = {item.replace("-", ".") for item in DIRECTION_CODE_RE.findall(normalized)}
    concepts = _query_concepts(query)
    if not codes and not concepts:
        return []

    scored: list[tuple[int, int, dict[str, Any]]] = []
    for program in programs:
        score = 10 if str(program.get("code", "")) in codes else 0
        title = " ".join(
            str(program.get(key, "")) for key in ("direction", "profile", "institute")
        )
        disciplines = " ".join(str(item) for item in (program.get("disciplines") or []))
        for concept in concepts:
            if _concept_matches(concept, title):
                score += 2
            elif _concept_matches(concept, disciplines):
                score += 1
        if score:
            scored.append((score, int(program.get("pass_score") or 0), program))

    scored.sort(key=lambda row: (-row[0], -row[1], str(row[2].get("code"))))
    selected: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for _, _, program in scored:
        key = (str(program.get("code", "")), str(program.get("form", "")))
        if key in seen:
            continue
        seen.add(key)
        selected.append(program)
        if len(selected) >= limit:
            break
    return selected


def format_catalog(items: list[dict[str, Any]]) -> str:
    """Компактная выжимка каталога для блока <catalog>."""
    if not items:
        return ""
    lines = ["Каталог приёмной кампании 2026 (те же данные, что в правой панели):"]
    lines.extend(_catalog_line(item) for item in items)
    return "\n".join(lines)


def _catalog_line(program: dict[str, Any]) -> str:
    code = str(program.get("code", "")).strip()
    direction = str(program.get("direction", "")).strip()
    meta = [
        str(program.get("level", "")).strip().lower(),
        str(program.get("form", "")).strip().lower(),
    ]
    years = program.get("years")
    if years:
        meta.append(f"{years} г.")
    head = f"- {code} «{direction}»"
    meta_text = ", ".join(item for item in meta if item)
    parts = [f"{head} — {meta_text}" if meta_text else head]

    profile = str(program.get("profile", "")).strip()
    if profile:
        parts.append(f"профиль «{profile}»")

    seats: list[str] = []
    budget = program.get("budget")
    paid = program.get("paid")
    if budget is not None:
        seats.append(f"бюджет {budget}" if int(budget or 0) else "бюджетных мест нет")
    if paid is not None:
        seats.append(f"платно {paid}")
    if seats:
        parts.append(", ".join(seats))

    pass_score = int(program.get("pass_score") or 0)
    if pass_score:
        parts.append(f"проходной 2026 — {pass_score}")
    subjects = SUBJECTS_BY_CODE.get(code)
    if subjects:
        parts.append("ЕГЭ: " + ", ".join(subjects))
    tuition = str(program.get("tuition", "")).strip()
    if tuition:
        parts.append(f"стоимость {tuition} ₽ за семестр")
    return "; ".join(parts) + "."


def min_scores_digest() -> str:
    """Строка с минимальными баллами 2026 для вопросов о порогах."""
    from src.ingest import load_min_scores_catalog

    payload = load_min_scores_catalog()
    rows = payload.get("soo") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        return ""
    parts = [
        f"{row.get('subject')} — {row.get('points')}"
        for row in rows
        if isinstance(row, dict) and row.get("subject") and row.get("points")
    ]
    return "Минимальные баллы ЕГЭ 2026: " + ", ".join(parts) + "." if parts else ""


SHORT_NAMES = {
    "02.03.02": ("ФИИТ", "FCS"),
    "09.03.01": ("ИВТ", "CS"),
    "09.03.02": ("ИСиТ", "IST"),
    "09.03.03": ("ПИ", "AI"),
    "10.03.01": ("ИБ", "SEC"),
    "11.03.01": ("Радио", "RADIO"),
    "11.03.02": ("Связь", "TEL"),
    "11.03.03": ("КТЭС", "KTES"),
    "11.03.04": ("Электроника", "ELEC"),
    "42.03.01": ("Реклама", "MEDIA"),
    "10.05.02": ("ИБТС", "SEC"),
    "11.05.01": ("РЭС", "RADIO"),
    "11.05.04": ("ИСС", "TEL"),
    "09.04.01": ("ИВТ (маг.)", "CS (MSc)"),
    "09.04.02": ("ИСиТ (маг.)", "IST (MSc)"),
    "09.04.03": ("ПИ (маг.)", "AI (MSc)"),
    "10.04.01": ("ИБ (маг.)", "SEC (MSc)"),
    "11.04.02": ("Связь (маг.)", "TEL (MSc)"),
    "42.04.01": ("Реклама (маг.)", "MEDIA (MSc)"),
    "20.03.01": ("Техносферная", "SAFETY"),
}

SUBJECTS_BY_CODE = {
    "02.03.02": ["Математика (профиль)", "Информатика / Физика", "Русский язык"],
    "09.03.01": ["Математика (профиль)", "Информатика / Физика", "Русский язык"],
    "09.03.02": ["Математика (профиль)", "Информатика / Физика", "Русский язык"],
    "09.03.03": ["Математика (профиль)", "Информатика / Физика", "Русский язык"],
    "10.03.01": ["Математика (профиль)", "Информатика / Физика", "Русский язык"],
    "11.03.01": ["Физика", "Информатика / Математика", "Русский язык"],
    "11.03.02": ["Математика (профиль)", "Физика", "Русский язык"],
    "11.03.03": ["Физика", "Информатика / Математика", "Русский язык"],
    "11.03.04": ["Физика", "Информатика / Математика", "Русский язык"],
    "42.03.01": ["Обществознание", "История / Информатика", "Русский язык"],
    "10.05.02": ["Математика (профиль)", "Информатика / Физика", "Русский язык"],
    "11.05.01": ["Физика", "Информатика / Математика", "Русский язык"],
    "11.05.04": ["Математика (профиль)", "Информатика / Физика", "Русский язык"],
    "20.03.01": ["Математика (профиль)", "Информатика / Физика", "Русский язык"],
    "09.04.01": ["Профильный экзамен (магистратура)"],
    "09.04.02": ["Профильный экзамен (магистратура)"],
    "09.04.03": ["Профильный экзамен (магистратура)"],
    "10.04.01": ["Профильный экзамен (магистратура)"],
    "11.04.02": ["Профильный экзамен (магистратура)"],
    "42.04.01": ["Профильный экзамен (магистратура)"],
}

LEVEL_EN = {
    "Бакалавриат": "Bachelor",
    "Специалитет": "Specialist",
    "Магистратура": "Master",
}
MASTER_MIN_SCORE = 35
FORM_LABELS = {
    "Очная": ("очная", "full-time"),
    "Заочная": ("заочная", "part-time"),
    "Заочная с применением дистанционных образовательных технологий": (
        "заочная (ДОТ)",
        "part-time (distance)",
    ),
}

MIN_SCORE_EN = {
    "Математика": "Mathematics",
    "Русский язык": "Russian language",
    "Информатика": "Computer science",
    "Физика": "Physics",
    "Обществознание": "Social studies",
    "История": "History",
    "Иностранный язык (английский)": "Foreign language (English)",
}


OVERVIEW_SCHEMA_VERSION = 4
DEFAULT_MIN_SCORES: dict[str, int] = {
    "Математика": 27,
    "Русский язык": 36,
    "Информатика": 40,
    "Физика": 36,
    "Обществознание": 44,
    "История": 34,
    "Иностранный язык (английский)": 34,
}


def _money_value(text: str) -> int:
    digits = re.sub(r"\D", "", text or "")
    return int(digits) if digits else 0


def _trim_text(text: str, limit: int = 420) -> str:
    """Обрезает описание по границе предложения или слова без обрыва на полуслове."""
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(cleaned) <= limit:
        return cleaned
    window = cleaned[:limit].rstrip()
    sentence_end = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
    if sentence_end >= int(limit * 0.55):
        return window[: sentence_end + 1].strip()
    word_end = window.rfind(" ")
    cut = window[:word_end] if word_end >= int(limit * 0.4) else window
    return cut.rstrip(" ,;:-—–") + "…"


def _subject_min_points(name: str, min_map: dict[str, int]) -> int | None:
    candidate = name.strip()
    if not candidate:
        return None
    if candidate in min_map:
        return min_map[candidate]
    base = re.sub(r"\s*\([^)]*\)", "", candidate).strip()
    if base in min_map:
        return min_map[base]
    lowered = base.lower()
    for key, points in min_map.items():
        key_base = re.sub(r"\s*\([^)]*\)", "", key).strip().lower()
        if key_base == lowered or key_base.startswith(lowered) or lowered.startswith(key_base):
            return points
    return None


def _min_exam_sum(code: str, min_map: dict[str, int]) -> int:
    """Минимальный суммарный порог 3 ЕГЭ для направления (используется для платного)."""
    slots = SUBJECTS_BY_CODE.get(code, [])
    if not slots:
        return 99
    total = 0
    for slot in slots:
        options = [part.strip() for part in slot.split("/") if part.strip()]
        scores = [pts for opt in options if (pts := _subject_min_points(opt, min_map)) is not None]
        total += min(scores) if scores else 30
    return total


def _overview_fingerprint() -> str:
    """Хэш состояния файлов базы и каталогов для автоматической инвалидации кэша панели."""
    hasher = hashlib.sha256()
    hasher.update(f"v{OVERVIEW_SCHEMA_VERSION}".encode())
    for path in (
        config.KB_PATH,
        config.PROGRAMS_PATH,
        config.MIN_SCORES_PATH,
        config.ACHIEVEMENTS_PATH,
    ):
        if path.exists():
            stat = path.stat()
            hasher.update(f"{path.name}:{stat.st_mtime_ns}:{stat.st_size};".encode())
        else:
            hasher.update(f"{path.name}:missing;".encode())
    return hasher.hexdigest()[:16]


def _read_overview_cache() -> dict[str, Any] | None:
    if not config.OVERVIEW_CACHE_PATH.exists():
        return None
    try:
        payload = json.loads(config.OVERVIEW_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("scoreDirections"), list):
        return None
    return payload


def _write_overview_cache(payload: dict[str, Any]) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, ensure_ascii=False, indent=2)
    fd, tmp_name = tempfile.mkstemp(prefix="overview_", suffix=".tmp", dir=config.DATA_DIR)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(raw)
        os.replace(tmp_path, config.OVERVIEW_CACHE_PATH)
    finally:
        tmp_path.unlink(missing_ok=True)


def _extract_kb_facts() -> dict[str, Any]:
    """Детерминированно извлекает актуальные цифры приёма из SQLite БД и каталогов."""
    facts: dict[str, Any] = {
        "kb_docs": 0,
        "kb_chunks": 0,
        "seats": {"budget_general": 711, "quotas": 229, "paid": 557},
        "bonus_max": 10,
        "submission_period_ru": "20 июня — 25 июля",
        "submission_period_en": "Jun 20 — Jul 25",
    }

    from src.ingest import load_achievements_catalog

    ach = load_achievements_catalog()
    try:
        max_b = int(ach.get("max_bachelor") or 0)
        if max_b > 0:
            facts["bonus_max"] = max_b
    except (ValueError, TypeError):
        facts["bonus_max"] = 10

    if not config.KB_PATH.exists():
        return facts

    try:
        connection = sqlite3.connect(f"file:{config.KB_PATH}?mode=ro", uri=True)
        try:
            facts["kb_docs"] = int(connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
            facts["kb_chunks"] = int(connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
            texts = [row[0] for row in connection.execute("SELECT text FROM chunks ORDER BY id").fetchall()]
        finally:
            connection.close()
    except sqlite3.Error:
        return facts

    full_text = "\n".join(texts)

    total_budget_m = re.search(r"общий объём\s*[—–-]\s*(\d+)\s*бюджетн", full_text, re.I)
    quotas_m = re.search(
        r"(\d+)\s*мест особой квоты,\s*(\d+)\s*мест отдельной квоты\s*и\s*(\d+)\s*мест целевой квоты",
        full_text,
        re.I,
    )
    if total_budget_m and quotas_m:
        total_budget = int(total_budget_m.group(1))
        quotas_sum = int(quotas_m.group(1)) + int(quotas_m.group(2)) + int(quotas_m.group(3))
        if total_budget > quotas_sum > 0:
            facts["seats"]["budget_general"] = total_budget - quotas_sum
            facts["seats"]["quotas"] = quotas_sum

    paid_m = re.search(r"Всего по очной форме\s*[—–-]\s*(\d+)\s*платных мест", full_text, re.I)
    if paid_m:
        paid_val = int(paid_m.group(1))
        if paid_val > 0:
            facts["seats"]["paid"] = paid_val

    sub_start_m = re.search(
        r"Начало при[её]ма заявлений и документов\s*\|\s*(\d{1,2}\s+[а-яё]+)",
        full_text,
        re.I,
    )
    sub_end_m = re.search(
        r"Завершение при[её]ма документов от поступающих по ЕГЭ[^|\n]*\|\s*(\d{1,2}\s+[а-яё]+)",
        full_text,
        re.I,
    )
    if sub_start_m and sub_end_m:
        facts["submission_period_ru"] = f"{sub_start_m.group(1).strip()} — {sub_end_m.group(1).strip()}"

    return facts


def build_overview(*, force_refresh: bool = False) -> dict[str, Any]:
    """Собирает данные для правой панели из каталога, SQLite БД и кэша `overview_cache.json`."""
    fingerprint = _overview_fingerprint()
    if not force_refresh:
        cached = _read_overview_cache()
        if cached is not None and cached.get("kb_fingerprint") == fingerprint:
            return cached

    from src.ingest import load_min_scores_catalog, load_program_catalog

    payload = load_program_catalog()
    catalog_programs = payload.get("programs") if isinstance(payload, dict) else []
    if not isinstance(catalog_programs, list):
        catalog_programs = []
    catalog_programs = [item for item in catalog_programs if isinstance(item, dict)]
    programs = [
        item
        for item in catalog_programs
        if "Очная" in str(item.get("form", ""))
        and "Заочная" not in str(item.get("form", ""))
    ]

    min_payload = load_min_scores_catalog()
    min_map = dict(DEFAULT_MIN_SCORES)
    min_subjects = []
    for item in min_payload.get("soo", []) if isinstance(min_payload, dict) else []:
        subject = str(item.get("subject", ""))
        points = int(item.get("points", 0))
        if subject and points:
            min_map[subject] = points
            min_subjects.append(
                {
                    "nameRu": subject,
                    "nameEn": MIN_SCORE_EN.get(subject, subject),
                    "budget": points,
                    "paid": points,
                }
            )

    levels = [("Бакалавриат", "Bachelor"), ("Специалитет", "Specialist"), ("Магистратура", "Master")]
    tuition = []
    for ru, en in levels:
        prices = [
            _money_value(item.get("tuition", ""))
            for item in programs
            if item.get("level") == ru and _money_value(item.get("tuition", ""))
        ]
        if not prices:
            continue
        price = min(prices)
        tuition.append(
            {
                "id": ru.lower(),
                "labelRu": ru,
                "labelEn": en,
                "priceRu": f"от {price:,} ₽".replace(",", " "),
                "priceEn": f"from {price:,} ₽".replace(",", " "),
                "pct": 100 if ru == "Бакалавриат" else 90 if ru == "Специалитет" else 95,
                "prompt": f"Сколько стоит обучение на уровне «{ru}» в СибГУТИ за семестр и за год?",
            }
        )

    grouped: dict[str, dict[str, Any]] = {}
    for item in catalog_programs:
        code = str(item.get("code", ""))
        level = str(item.get("level", ""))
        if not code or level not in LEVEL_EN:
            continue
        form = str(item.get("form", ""))
        is_full_time = "Очная" in form and "Заочная" not in form
        raw_score = int(item.get("pass_score") or 0)
        budget_seats = int(item.get("budget") or 0)
        paid_seats = int(item.get("paid") or 0)
        profile = str(item.get("profile", ""))
        current = grouped.get(code)
        if current is None:
            grouped[code] = {
                "code": code,
                "level": level,
                "profile": profile,
                "direction": str(item.get("direction", "")),
                "institute": str(item.get("institute", "")),
                "forms": {form} if form else set(),
                "profiles": {profile} if profile else set(),
                "_rawScore": raw_score if is_full_time else 0,
                "_rawScoreAny": raw_score,
                "budgetSeats": budget_seats,
                "paidSeats": paid_seats,
                "description": str(item.get("description", "")),
            }
            continue
        current["budgetSeats"] = max(current["budgetSeats"], budget_seats)
        current["paidSeats"] = max(current["paidSeats"], paid_seats)
        if form:
            current["forms"].add(form)
        if profile:
            current["profiles"].add(profile)
        if raw_score > current["_rawScoreAny"]:
            current["_rawScoreAny"] = raw_score
        if is_full_time and raw_score >= current["_rawScore"]:
            current["_rawScore"] = raw_score
            current["profile"] = profile
            current["description"] = str(item.get("description", ""))

    score_directions = []
    for item in grouped.values():
        code = item["code"]
        raw_score = item["_rawScore"] or item["_rawScoreAny"]
        has_budget = item["budgetSeats"] > 0
        is_master = item["level"] == "Магистратура"
        min_sum = MASTER_MIN_SCORE if is_master else _min_exam_sum(code, min_map)
        if is_master:
            budget_score = raw_score if has_budget else 0
            if has_budget:
                paid_score = min_sum
                paid_is_min = True
            else:
                paid_score = raw_score if raw_score > 0 else min_sum
                paid_is_min = raw_score <= 0
        elif has_budget:
            budget_score = raw_score
            paid_score = min_sum
            paid_is_min = True
        else:
            budget_score = 0
            paid_score = raw_score if raw_score > 0 else min_sum
            paid_is_min = raw_score <= 0
        short_ru, short_en = SHORT_NAMES.get(code, (code, code))
        desc = _trim_text(item["description"], 420)
        forms_ru: list[str] = []
        forms_en: list[str] = []
        for form in item["forms"]:
            for part in form.split(","):
                label = FORM_LABELS.get(part.strip())
                if label and label[0] not in forms_ru:
                    forms_ru.append(label[0])
                    forms_en.append(label[1])
        score_directions.append(
            {
                "id": code.replace(".", "-"),
                "code": code,
                "levelRu": item["level"],
                "levelEn": LEVEL_EN[item["level"]],
                "formsRu": forms_ru,
                "formsEn": forms_en,
                "profilesRu": sorted(item["profiles"]),
                "profilesEn": sorted(item["profiles"]),
                "shortRu": short_ru,
                "shortEn": short_en,
                "nameRu": item["direction"],
                "nameEn": item["direction"],
                "facultyRu": item["institute"],
                "facultyEn": item["institute"],
                "budget": budget_score,
                "paid": paid_score,
                "paidIsMinThreshold": paid_is_min,
                "budgetSeats": item["budgetSeats"],
                "paidSeats": item["paidSeats"],
                "subjectsRu": SUBJECTS_BY_CODE.get(code, []),
                "subjectsEn": SUBJECTS_BY_CODE.get(code, []),
                "descriptionRu": desc,
                "descriptionEn": desc,
            }
        )
    score_directions.sort(key=lambda d: (-d["budget"], -d["paid"], d["code"]))

    salaries_grouped: dict[str, dict[str, Any]] = {}
    for item in programs:
        value = _money_value(item.get("salary", ""))
        code = str(item.get("code", ""))
        if not value or not code:
            continue
        short_ru, short_en = SHORT_NAMES.get(code, (code, code))
        current = salaries_grouped.get(code)
        if current is None or value > _money_value(current["valueRu"]):
            salaries_grouped[code] = {
                "id": code.replace(".", "-"),
                "labelRu": f"{short_ru} · {item.get('direction', '')[:40]}",
                "labelEn": f"{short_en} · {item.get('direction', '')[:40]}",
                "valueRu": f"{value:,} ₽".replace(",", " "),
                "valueEn": f"{value:,} ₽".replace(",", " "),
                "pct": 0,
            }
    salaries = sorted(salaries_grouped.values(), key=lambda item: -_money_value(item["valueRu"]))[:4]
    max_salary = max((_money_value(item["valueRu"]) for item in salaries), default=1)
    for item in salaries:
        item["pct"] = round(_money_value(item["valueRu"]) / max_salary * 100)

    kb_facts = _extract_kb_facts()
    seats_facts = kb_facts["seats"]
    bonus_max = kb_facts["bonus_max"]

    overview = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "kb+catalog",
        "method": "kb+catalog",
        "kb_fingerprint": fingerprint,
        "kb_docs": kb_facts["kb_docs"],
        "kb_chunks": kb_facts["kb_chunks"],
        "tuition": tuition,
        "scoreDirections": score_directions,
        "minSubjectScores": {
            "noteRu": "Минимальные баллы 2026. Ниже порога документы не принимают.",
            "noteEn": "Minimum scores 2026. Applications below the threshold are not accepted.",
            "subjects": min_subjects
            or [
                {"nameRu": "Математика (профиль)", "nameEn": "Mathematics (profile)", "budget": 27, "paid": 27},
                {"nameRu": "Русский язык", "nameEn": "Russian language", "budget": 36, "paid": 36},
                {"nameRu": "Информатика", "nameEn": "Computer science", "budget": 40, "paid": 40},
                {"nameRu": "Физика", "nameEn": "Physics", "budget": 36, "paid": 36},
                {"nameRu": "Обществознание", "nameEn": "Social studies", "budget": 44, "paid": 44},
                {"nameRu": "История", "nameEn": "History", "budget": 34, "paid": 34},
            ],
        },
        "salaries": salaries,
        "seats": [
            {
                "id": "budget",
                "cls": "s1",
                "colorVar": "var(--seg-1)",
                "labelRu": "Бюджет (общий конкурс)",
                "labelEn": "State-funded (general)",
                "count": seats_facts["budget_general"],
                "prompt": "Сколько бюджетных мест по общему конкурсу выделено в СибГУТИ?",
            },
            {
                "id": "quotas",
                "cls": "s2",
                "colorVar": "var(--seg-2)",
                "labelRu": "Целевая и спецквоты",
                "labelEn": "Target & special quotas",
                "count": seats_facts["quotas"],
                "prompt": "Как устроены целевая, особая и отдельная квоты при поступлении в СибГУТИ?",
            },
            {
                "id": "paid",
                "cls": "s3",
                "colorVar": "var(--seg-3)",
                "labelRu": "Платные места",
                "labelEn": "Tuition-based seats",
                "count": seats_facts["paid"],
                "prompt": "Сколько платных мест доступно в СибГУТИ и можно ли потом перевестись на бюджет?",
            },
        ],
        "campus": [
            {
                "labelRu": "Академическая",
                "labelEn": "Academic",
                "valRu": "от 2 900 ₽/мес",
                "valEn": "from 2,900 ₽/mo",
                "prompt": "Какие условия и размер государственной академической стипендии в СибГУТИ?",
            },
            {
                "labelRu": "Повышенная",
                "labelEn": "Enhanced",
                "valRu": "до 12 000 ₽/мес",
                "valEn": "up to 12,000 ₽/mo",
                "prompt": "За какие достижения назначается повышенная академическая стипендия в СибГУТИ?",
            },
            {
                "labelRu": "Общежитие",
                "labelEn": "Dormitory",
                "valRu": "от 1 100 ₽/мес",
                "valEn": "from 1,100 ₽/mo",
                "prompt": "Как получить место в общежитии СибГУТИ и сколько стоит проживание?",
            },
            {
                "labelRu": "Доп. баллы ИД",
                "labelEn": "Bonus points",
                "valRu": f"до +{bonus_max} баллов",
                "valEn": f"up to +{bonus_max} pts",
                "prompt": "За какие индивидуальные достижения СибГУТИ начисляет дополнительные баллы?",
            },
        ],
        "timeline": [
            {
                "titleRu": "Подача документов",
                "titleEn": "Application submission",
                "periodRu": kb_facts["submission_period_ru"],
                "periodEn": kb_facts["submission_period_en"],
                "current": True,
                "prompt": "Какие сроки и способы подачи документов в СибГУТИ?",
            },
            {
                "titleRu": "Приоритетное зачисление",
                "titleEn": "Priority enrollment",
                "periodRu": "конец июля — начало августа",
                "periodEn": "late July — early August",
                "current": False,
                "prompt": "Как проходит приоритетное зачисление по квотам и БВИ?",
            },
            {
                "titleRu": "Основной этап",
                "titleEn": "Main stage",
                "periodRu": "начало августа",
                "periodEn": "early August",
                "current": False,
                "prompt": "Когда публикуются конкурсные списки и приказы основного этапа?",
            },
            {
                "titleRu": "Заселение в общежития",
                "titleEn": "Dorm move-in",
                "periodRu": "конец августа",
                "periodEn": "late August",
                "current": False,
                "prompt": "Когда и как заселяются в общежития СибГУТИ?",
            },
        ],
    }
    _write_overview_cache(overview)
    return overview


def refresh_overview() -> dict[str, Any]:
    """Пересчитывает снимок правой панели из SQLite и каталогов и сохраняет его в кэш.

    Панель собирается детерминированно (без LLM): цифры должны совпадать с ответами
    чата и не зависеть от доступности модели.
    """
    return build_overview(force_refresh=True)





