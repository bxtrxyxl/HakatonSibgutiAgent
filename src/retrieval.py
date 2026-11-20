"""Гибридный поиск: SQLite FTS5 (BM25) + косинусное сходство (numpy) -> RRF."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from src import config

RRF_K = 60
NEAR_DUPLICATE_SIM = 0.97
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
        if not self.embeddings_path.exists():
            self._embeddings = np.zeros((0, 0), dtype=np.float32)
            self._chunk_ids = np.zeros((0,), dtype=np.int64)
            return
        matrix = np.load(self.embeddings_path).astype(np.float32)
        connection = self._connect()
        try:
            ids = np.asarray([row["id"] for row in connection.execute("SELECT id FROM chunks ORDER BY id")])
        finally:
            connection.close()
        if ids.shape[0] != matrix.shape[0]:
            raise RuntimeError(
                f"рассинхронизация индексов: чанков {ids.shape[0]}, векторов {matrix.shape[0]}. "
                "Пересоберите базу: python -m src.ingest --urls data/seed_urls.json"
            )
        self._embeddings = matrix
        self._chunk_ids = ids

    def _query_vector(self, query: str) -> np.ndarray | None:
        if self._embeddings is None or self._embeddings.size == 0:
            return None
        if self._embedder is None:
            from src.ingest import embed_texts

            self._embedder = embed_texts
        vector = self._embedder([query])[0]
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
        vector = self._query_vector(query)
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
        self._load_embeddings()
        id_to_pos = {int(cid): pos for pos, cid in enumerate(self._chunk_ids)} if self._chunk_ids is not None else {}

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
