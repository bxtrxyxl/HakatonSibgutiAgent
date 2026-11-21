"""Гибридный поиск: SQLite FTS5 (BM25) + косинусное сходство (numpy) -> RRF."""

from __future__ import annotations

import json
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
    if not config.PROGRAMS_PATH.exists():
        return []
    try:
        payload = json.loads(config.PROGRAMS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
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


SHORT_NAMES = {
    "02.03.02": ("ФИИТ", "FCS"),
    "09.03.01": ("ИВТ", "CS"),
    "09.03.02": ("ИСиТ", "IST"),
    "09.03.03": ("ПИ", "AI"),
    "10.03.01": ("ИБ", "SEC"),
    "11.03.01": ("Радио", "RADIO"),
    "11.03.02": ("Связь", "TEL"),
    "11.03.03": ("Электроника", "ELEC"),
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
}


def _money_value(text: str) -> int:
    digits = re.sub(r"\D", "", text or "")
    return int(digits) if digits else 0


def build_overview() -> dict[str, Any]:
    """Собирает данные для правой панели из каталога и официальных констант 2026."""
    try:
        payload = json.loads(config.PROGRAMS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    programs = payload.get("programs") if isinstance(payload, dict) else []
    programs = [
        item
        for item in programs
        if isinstance(item, dict)
        and "Очная" in str(item.get("form", ""))
        and "Заочная" not in str(item.get("form", ""))
    ]

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
    for item in programs:
        code = str(item.get("code", ""))
        level = str(item.get("level", ""))
        if not code or level not in {"Бакалавриат", "Специалитет"}:
            continue
        current = grouped.get(code)
        candidate = {
            "code": code,
            "profile": item.get("profile", ""),
            "direction": item.get("direction", ""),
            "institute": item.get("institute", ""),
            "budget": item.get("pass_score") or 0,
            "paid": 0,
            "budgetSeats": item.get("budget") or 0,
            "paidSeats": item.get("paid") or 0,
            "description": item.get("description", ""),
        }
        if current is None:
            grouped[code] = candidate
        else:
            current["budgetSeats"] = max(current["budgetSeats"], candidate["budgetSeats"])
            current["paidSeats"] = max(current["paidSeats"], candidate["paidSeats"])
            if candidate["budget"] > current["budget"]:
                current.update(
                    {
                        "budget": candidate["budget"],
                        "profile": candidate["profile"],
                        "description": candidate["description"],
                    }
                )

    directions = sorted(grouped.values(), key=lambda item: -item["budget"])[:6]
    score_directions = []
    for item in directions:
        short_ru, short_en = SHORT_NAMES.get(item["code"], (item["code"], item["code"]))
        score_directions.append(
            {
                "id": item["code"].replace(".", "-"),
                "code": item["code"],
                "shortRu": short_ru,
                "shortEn": short_en,
                "nameRu": item["direction"],
                "nameEn": item["direction"],
                "facultyRu": item["institute"],
                "facultyEn": item["institute"],
                "budget": item["budget"],
                "paid": item["paid"],
                "budgetSeats": item["budgetSeats"],
                "paidSeats": item["paidSeats"],
                "subjectsRu": SUBJECTS_BY_CODE.get(item["code"], []),
                "subjectsEn": SUBJECTS_BY_CODE.get(item["code"], []),
                "descriptionRu": item["description"][:500],
                "descriptionEn": item["description"][:500],
            }
        )

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

    return {
        "generated_at": payload.get("generated_at", ""),
        "source": "catalog",
        "tuition": tuition,
        "scoreDirections": score_directions,
        "minSubjectScores": {
            "noteRu": "Минимальные баллы 2026. Ниже порога документы не принимают.",
            "noteEn": "Minimum scores 2026. Applications below the threshold are not accepted.",
            "subjects": [
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
                "count": 711,
                "prompt": "Сколько бюджетных мест по общему конкурсу выделено в СибГУТИ?",
            },
            {
                "id": "quotas",
                "cls": "s2",
                "colorVar": "var(--seg-2)",
                "labelRu": "Целевая и спецквоты",
                "labelEn": "Target & special quotas",
                "count": 229,
                "prompt": "Как устроены целевая, особая и отдельная квоты при поступлении в СибГУТИ?",
            },
            {
                "id": "paid",
                "cls": "s3",
                "colorVar": "var(--seg-3)",
                "labelRu": "Платные места",
                "labelEn": "Tuition-based seats",
                "count": 557,
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
                "valRu": "до +10 баллов",
                "valEn": "up to +10 pts",
                "prompt": "За какие индивидуальные достижения СибГУТИ начисляет дополнительные баллы?",
            },
        ],
        "timeline": [
            {
                "titleRu": "Подача документов",
                "titleEn": "Application submission",
                "periodRu": "20 июня — 25 июля",
                "periodEn": "Jun 20 — Jul 25",
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

