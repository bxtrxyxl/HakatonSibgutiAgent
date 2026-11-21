"""Ядро агента: изолированные сессии, RAG-пайплайн, сборка ответа с источниками."""

from __future__ import annotations

import re
import time
import uuid
from typing import Any

from src import config, prompts, security
from src.ingest import load_achievements_catalog
from src.retrieval import HybridRetriever, recommend_programs

HISTORY_LIMIT = 8
LLM_MAX_TOKENS = 2000
CITATION_RE = re.compile(r"\[(\d{1,2})\]")
RECOMMEND_RE = re.compile(
    r"(подбер\w*|подходящ\w*|какие\s+направлен\w*|куда\s+поступ\w*|"
    r"хочу\s+(работать|стать|учиться)|кем\s+работ\w*|стек|професси\w*|"
    r"что\s+изуча\w*|какие\s+технологи\w*)",
    re.I,
)
ACHIEVEMENT_RE = re.compile(
    r"(индивидуальн\w*\s+достижен\w*|дополнительн\w*\s+балл\w*|"
    r"балл\w*\s+за\s+достижен\w*|за\s+какие\s+достижен\w*|"
    r"олимпиад\w*|волонтер\w*|медаль\w*|целевая\s+квота)",
    re.I,
)


class SessionStore:
    """Хранилище истории строго по session_id.

    Ни один метод не умеет читать историю «вообще»: доступ всегда идёт
    через конкретный идентификатор, поэтому утечка между сессиями невозможна.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, list[dict[str, str]]] = {}

    def new_session(self) -> str:
        session_id = str(uuid.uuid4())
        self._sessions[session_id] = []
        return session_id

    def exists(self, session_id: str) -> bool:
        return session_id in self._sessions

    def ensure(self, session_id: str) -> str:
        if session_id not in self._sessions:
            self._sessions[session_id] = []
        return session_id

    def history(self, session_id: str) -> list[dict[str, str]]:
        return [dict(message) for message in self._sessions.get(session_id, [])]

    def append(self, session_id: str, role: str, content: str) -> None:
        self._sessions.setdefault(session_id, []).append({"role": role, "content": content})
        if len(self._sessions[session_id]) > HISTORY_LIMIT * 2:
            self._sessions[session_id] = self._sessions[session_id][-HISTORY_LIMIT * 2:]

    def reset(self, session_id: str) -> None:
        self._sessions[session_id] = []


class SibSUTISAgent:
    """RAG-агент справочника СибГУТИ."""

    def __init__(self, retriever: HybridRetriever | None = None) -> None:
        self.retriever = retriever or HybridRetriever()
        self.sessions = SessionStore()
        self._client = None

    # --- инфраструктура --------------------------------------------------

    def _llm(self) -> Any:
        if self._client is None:
            from openai import OpenAI

            config.require_llm()
            self._client = OpenAI(
                base_url=config.LLM_BASE_URL,
                api_key=config.LLM_API_KEY,
                timeout=config.LLM_TIMEOUT,
                max_retries=1,
            )
        return self._client

    def _chat(self, messages: list[dict[str, str]], max_tokens: int = LLM_MAX_TOKENS) -> str:
        response = self._llm().chat.completions.create(
            model=config.LLM_MODEL,
            messages=messages,
            temperature=0.2,
            max_tokens=max_tokens,
        )
        content = (response.choices[0].message.content or "").strip()
        if not content:
            # Модель могла израсходовать бюджет на reasoning — один повтор с запасом.
            response = self._llm().chat.completions.create(
                model=config.LLM_MODEL,
                messages=messages,
                temperature=0.2,
                max_tokens=max_tokens * 2,
            )
            content = (response.choices[0].message.content or "").strip()
        return content

    # --- сессии ----------------------------------------------------------

    def new_session(self) -> str:
        return self.sessions.new_session()

    def reset_session(self, session_id: str) -> None:
        self.sessions.reset(session_id)

    # --- шаги пайплайна --------------------------------------------------

    def _rewrite_query(self, session_id: str, message: str) -> str:
        history = self.sessions.history(session_id)
        if not history:
            return message.strip()
        rendered = "\n".join(f"{item['role']}: {item['content'][:400]}" for item in history[-HISTORY_LIMIT:])
        try:
            rewritten = self._chat(
                [
                    {
                        "role": "user",
                        "content": prompts.REWRITE_PROMPT.format(history=rendered, question=message.strip()),
                    }
                ],
                max_tokens=200,
            )
        except Exception:
            return message.strip()
        rewritten = rewritten.strip().strip('"').strip()
        return rewritten[:400] if rewritten else message.strip()

    def _collect_sources(self, answer: str, chunks: list[dict[str, Any]]) -> list[dict[str, str]]:
        cited = {int(match) for match in CITATION_RE.findall(answer or "")}
        selected: list[dict[str, Any]] = []
        for number in sorted(cited):
            if 1 <= number <= len(chunks):
                selected.append(chunks[number - 1])
        if not selected:
            selected = chunks

        sources: list[dict[str, str]] = []
        seen: set[str] = set()
        for chunk in selected:
            url = str(chunk.get("url", ""))
            if not url or url in seen:
                continue
            seen.add(url)
            sources.append(
                {
                    "title": str(chunk.get("title", "")),
                    "url": url,
                    "section": str(chunk.get("section", "")),
                    "year_version": str(chunk.get("year_version") or ""),
                }
            )
        return sources

    def _achievements_answer(self, message: str) -> dict[str, Any] | None:
        data = load_achievements_catalog()
        bachelor = data.get("bachelor") or []
        master = data.get("master") or []
        target = data.get("target") or []
        if not bachelor and not master:
            return None

        lower = message.lower()
        want_master = "магистра" in lower
        want_target = "целев" in lower

        def cell(value: Any) -> str:
            return str(value or "").replace("|", "\\|").replace("\n", " ").strip()

        lines: list[str] = []
        if bachelor and not want_master:
            lines.append("**Бакалавриат и специалитет — не более 10 баллов суммарно**")
            lines.append("")
            lines.append("| № | Достижение | Баллы |")
            lines.append("| --- | --- | --- |")
            for item in bachelor:
                lines.append(f"| {item['number']} | {cell(item['title'])} | {item['points']} |")
            lines.append("")

        if master and (want_master or not bachelor):
            lines.append("**Магистратура — не более 50 баллов суммарно**")
            lines.append("")
            lines.append("| № | Достижение | Баллы |")
            lines.append("| --- | --- | --- |")
            for item in master:
                lines.append(f"| {item['number']} | {cell(item['title'])} | {item['points']} |")
            lines.append("")

        if target and (want_target or not want_master):
            lines.append("**Целевая квота**")
            lines.append("")
            for item in target:
                lines.append(f"- {item['title']} — **+{item['points']}** баллов.")
            lines.append("")

        if bachelor and master and not want_master:
            lines.append("Для магистратуры действует отдельная таблица — до 50 баллов. Уточните, если нужна она.")

        lines.append(
            "Баллы начисляются только при наличии подтверждающих документов "
            "до дня завершения приёма документов."
        )
        source_url = str(data.get("source", ""))
        sources = (
            [
                {
                    "title": "Приложение 3. Индивидуальные достижения 2026",
                    "url": source_url,
                    "section": "Поступление",
                    "year_version": "2026",
                }
            ]
            if source_url
            else []
        )
        return {"answer_markdown": "\n".join(lines), "sources": sources}

    def _recommend_answer(self, message: str) -> dict[str, Any] | None:
        items = recommend_programs(message, limit=5)
        if not items:
            return None

        def cell(value: Any) -> str:
            return str(value or "").replace("|", "\\|").replace("\n", " ").strip()

        lines = [
            "По вашему запросу подходят эти направления. Совпадение считается по пересечению стека "
            "с профилирующими дисциплинами и профессиями выпускников — это ориентир, а не гарантия поступления.",
            "",
            "| Направление | Профиль | Совпадение |",
            "| --- | --- | --- |",
        ]
        for item in items:
            lines.append(
                f"| {cell(item['code'])} {cell(item['direction'])} | {cell(item['profile'])} | {item['fit']}% |"
            )

        lines.append("")
        for item in items[:3]:
            lines.append(f"**{item['code']} {item['direction']} — {item['profile']}**")
            lines.append(f"Совпадение: **{item['fit']}%**.")
            if item["disciplines"]:
                lines.append("Что изучают: " + ", ".join(item["disciplines"][:5]) + ".")
            if item["professions"]:
                lines.append("Кем работают: " + "; ".join(item["professions"][:3]) + ".")
            details = []
            if item.get("pass_score"):
                details.append(f"проходной балл 2026 — {item['pass_score']}")
            if item.get("budget") is not None:
                details.append(f"бюджетных мест — {item['budget']}")
            if item.get("paid") is not None:
                details.append(f"платных мест — {item['paid']}")
            if details:
                lines.append("Поступление: " + ", ".join(details) + ".")
            lines.append("")

        lines.append(
            "Уточните стек или профессию — подберу точнее. "
            "Например: Python и машинное обучение, backend, сети, безопасность."
        )
        sources = []
        seen: set[str] = set()
        for item in items:
            url = str(item.get("url", ""))
            if not url or url in seen:
                continue
            seen.add(url)
            sources.append(
                {
                    "title": f"{item['code']} {item['profile']}",
                    "url": url,
                    "section": "Программы",
                    "year_version": "2026",
                }
            )
        return {"answer_markdown": "\n".join(lines), "sources": sources}

    # --- основной цикл ---------------------------------------------------

    def answer(self, session_id: str, user_message: str) -> dict[str, Any]:
        started = time.perf_counter()
        session_id = self.sessions.ensure(session_id)
        message = (user_message or "").strip()
        flags = security.detect_injection(message)
        retrieved: list[dict[str, Any]] = []

        def finish(payload: dict[str, Any], status: str, error: str = "") -> dict[str, Any]:
            latency_ms = int((time.perf_counter() - started) * 1000)
            payload["latency_ms"] = latency_ms
            payload["session_id"] = session_id
            security.log_event(
                {
                    "session_id": session_id,
                    "query_len": len(message),
                    "retrieved_urls": [chunk.get("url", "") for chunk in retrieved],
                    "latency_ms": latency_ms,
                    "security_flags": ",".join(flags),
                    "status": status,
                    "error": error,
                }
            )
            return payload

        if not message:
            return finish(
                {"answer_markdown": "Введите вопрос — например, о поступлении, общежитии или стипендии.",
                 "sources": []},
                "empty_query",
            )

        if not flags and ACHIEVEMENT_RE.search(message):
            achievements = self._achievements_answer(message)
            if achievements is not None:
                self.sessions.append(session_id, "user", message)
                self.sessions.append(session_id, "assistant", achievements["answer_markdown"])
                retrieved = achievements.get("sources", [])
                return finish(achievements, "achievements")

        if not flags and RECOMMEND_RE.search(message):
            recommendation = self._recommend_answer(message)
            if recommendation is not None:
                self.sessions.append(session_id, "user", message)
                self.sessions.append(session_id, "assistant", recommendation["answer_markdown"])
                retrieved = recommendation.get("sources", [])
                return finish(recommendation, "recommendation")

        try:
            query = self._rewrite_query(session_id, message)
            retrieved = self.retriever.search(query)
        except Exception as exc:
            return finish(
                {
                    "answer_markdown": "Не удалось выполнить поиск по базе знаний. "
                    "Проверьте, что база собрана: `python -m src.ingest --urls data/seed_urls.json`.",
                    "sources": [],
                },
                "retrieval_error",
                f"{type(exc).__name__}: {exc}",
            )

        if not retrieved:
            answer = (
                "В доступных материалах СибГУТИ не нашлось сведений по этому вопросу. "
                "Уточните, пожалуйста: вы абитуриент или студент, интересует бакалавриат, "
                "специалитет или магистратура? Актуальную информацию всегда можно проверить "
                "в разделе «Абитуриентам» на sibsutis.ru или в приёмной комиссии."
            )
            self.sessions.append(session_id, "user", message)
            self.sessions.append(session_id, "assistant", answer)
            return finish({"answer_markdown": answer, "sources": []}, "no_context")

        context = security.sanitize_chunks(retrieved)
        history = self.sessions.history(session_id)[-HISTORY_LIMIT:]
        messages: list[dict[str, str]] = [{"role": "system", "content": prompts.SYSTEM_PROMPT}]
        messages.extend(history)
        messages.append(
            {
                "role": "user",
                "content": (
                    f"<knowledge_base>\n{context}\n</knowledge_base>\n\n"
                    f"Вопрос пользователя: {message}"
                ),
            }
        )

        try:
            answer = self._chat(messages)
        except Exception as exc:
            return finish(
                {
                    "answer_markdown": "Сервис генерации ответа временно недоступен. "
                    "Повторите запрос через минуту — поиск по базе знаний при этом работает.",
                    "sources": [],
                },
                "llm_error",
                f"{type(exc).__name__}: {exc}",
            )

        if not answer:
            return finish(
                {"answer_markdown": "Модель не вернула ответ. Попробуйте переформулировать вопрос.", "sources": []},
                "empty_answer",
            )

        sources = self._collect_sources(answer, retrieved)
        self.sessions.append(session_id, "user", message)
        self.sessions.append(session_id, "assistant", answer)

        payload: dict[str, Any] = {"answer_markdown": answer, "sources": sources}
        if flags:
            payload["security_notice"] = prompts.SECURITY_NOTICE
        return finish(payload, "ok")


_default_agent: SibSUTISAgent | None = None


def get_agent() -> SibSUTISAgent:
    global _default_agent
    if _default_agent is None:
        _default_agent = SibSUTISAgent()
    return _default_agent
