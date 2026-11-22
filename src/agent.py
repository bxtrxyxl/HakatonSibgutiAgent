"""Ядро агента: изолированные сессии, RAG-пайплайн, сборка ответа с источниками."""

from __future__ import annotations

import re
import threading
import time
import uuid
from typing import Any

from src import config, prompts, security
from src.ingest import load_achievements_catalog
from src.retrieval import HybridRetriever, recommend_programs

HISTORY_LIMIT = 8
LLM_HISTORY_MESSAGES = 4
LLM_HISTORY_ITEM_CHARS = 600
LLM_MAX_TOKENS = 1200
FOLLOWUP_RE = re.compile(
    r"(?:^(?:а|и|но|ну|тогда|ещ[её]|кстати|хорошо|ладно|понятно)\b"
    r"|\b(?:он|она|оно|они|его|е[её]|их|него|не[её]|них|нему|ней|ним|"
    r"этот|эта|это|эти|этого|этой|этим|этих|туда|там|оттуда|"
    r"такой|такая|такие|такое|подробнее)\b)",
    re.I,
)
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


def _needs_rewrite(message: str, history: list[dict[str, str]]) -> bool:
    """Переформулировка нужна только если есть история и вопрос опирается на контекст диалога."""
    if not history:
        return False
    text = (message or "").strip()
    if not text:
        return False
    if FOLLOWUP_RE.search(text):
        return True
    tokens = re.findall(r"[0-9a-zA-Zа-яА-ЯёЁ]+", text)
    return len(tokens) <= 5


class SessionStore:
    """Хранилище истории строго по session_id.

    Ни один метод не умеет читать историю «вообще»: доступ всегда идёт
    через конкретный идентификатор, поэтому утечка между сессиями невозможна.
    """

    def __init__(self, limit: int | None = None) -> None:
        self._limit = config.SESSION_LIMIT if limit is None else limit
        self._lock = threading.Lock()
        self._sessions: dict[str, list[dict[str, str]]] = {}

    def _evict_locked(self) -> None:
        """Удаляет самые старые сессии, пока не уложимся в лимит."""
        if self._limit <= 0:
            return
        while len(self._sessions) > self._limit:
            oldest = next(iter(self._sessions))
            self._sessions.pop(oldest, None)

    def new_session(self) -> str:
        session_id = str(uuid.uuid4())
        with self._lock:
            self._sessions[session_id] = []
            self._evict_locked()
        return session_id

    def exists(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._sessions

    def ensure(self, session_id: str) -> str:
        with self._lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = []
                self._evict_locked()
            return session_id

    def history(self, session_id: str) -> list[dict[str, str]]:
        with self._lock:
            messages = self._sessions.get(session_id, [])
            return [dict(message) for message in messages]

    def append(self, session_id: str, role: str, content: str) -> None:
        with self._lock:
            messages = self._sessions.setdefault(session_id, [])
            messages.append({"role": role, "content": content})
            if len(messages) > HISTORY_LIMIT * 2:
                del messages[:-HISTORY_LIMIT * 2]

    def reset(self, session_id: str) -> None:
        with self._lock:
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

    def _chat(
        self,
        messages: list[dict[str, str]],
        max_tokens: int = LLM_MAX_TOKENS,
        timeout: float | None = None,
        retry_on_empty: bool = True,
    ) -> str:
        kwargs: dict[str, Any] = {
            "model": config.LLM_MODEL,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
        if timeout is not None:
            kwargs["timeout"] = timeout
        response = self._llm().chat.completions.create(**kwargs)
        content = (response.choices[0].message.content or "").strip()
        if not content and retry_on_empty:
            # Модель могла израсходовать бюджет на reasoning — один повтор с запасом.
            response = self._llm().chat.completions.create(**{**kwargs, "max_tokens": max_tokens * 2})
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
        if not _needs_rewrite(message, history):
            return message.strip()
        rendered = "\n".join(
            f"{item['role']}: {item['content'][:300]}"
            for item in history[-LLM_HISTORY_MESSAGES:]
        )
        try:
            rewritten = self._chat(
                [
                    {
                        "role": "user",
                        "content": prompts.REWRITE_PROMPT.format(history=rendered, question=message.strip()),
                    }
                ],
                max_tokens=80,
                timeout=config.LLM_REWRITE_TIMEOUT,
                retry_on_empty=False,
            )
        except Exception:
            return message.strip()
        rewritten = rewritten.strip().strip('"').strip()
        return rewritten[:300] if rewritten else message.strip()

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

    def answer(
        self,
        session_id: str,
        user_message: str,
        context_items: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        session_id = self.sessions.ensure(session_id)
        message = (user_message or "").strip()
        attached_items = [
            item
            for item in (context_items or [])
            if isinstance(item, dict) and (item.get("label") or item.get("summary"))
        ][:6]
        attached_xml, context_flags = security.sanitize_context_items(attached_items)
        flags = list(dict.fromkeys([*security.detect_injection(message), *context_flags]))
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

        if not attached_items and not flags and ACHIEVEMENT_RE.search(message):
            achievements = self._achievements_answer(message)
            if achievements is not None:
                self.sessions.append(session_id, "user", message)
                self.sessions.append(session_id, "assistant", achievements["answer_markdown"])
                retrieved = achievements.get("sources", [])
                return finish(achievements, "achievements")

        if not attached_items and not flags and RECOMMEND_RE.search(message):
            recommendation = self._recommend_answer(message)
            if recommendation is not None:
                self.sessions.append(session_id, "user", message)
                self.sessions.append(session_id, "assistant", recommendation["answer_markdown"])
                retrieved = recommendation.get("sources", [])
                return finish(recommendation, "recommendation")

        try:
            query = self._rewrite_query(session_id, message)
            if attached_items and not context_flags:
                ctx_hint = " ".join(
                    str(item.get("label", "")).strip() for item in attached_items if item.get("label")
                )[:220]
                search_query = f"{query} {ctx_hint}".strip() if ctx_hint else query
            else:
                search_query = query
            retrieved = self.retriever.search(search_query)
        except Exception as exc:
            return finish(
                {
                    "answer_markdown": "Не удалось выполнить поиск по базе знаний. "
                    "Пересоберите базу: `python -m src.ingest --dir data/docs` "
                    "(или запустите `start.bat` — он соберёт её сам).",
                    "sources": [],
                },
                "retrieval_error",
                f"{type(exc).__name__}: {exc}",
            )

        if not retrieved and not attached_xml:
            answer = (
                "В доступных материалах СибГУТИ не нашлось сведений по этому вопросу. "
                "Уточните, пожалуйста: вы абитуриент или студент, интересует бакалавриат, "
                "специалитет или магистратура? Актуальную информацию всегда можно проверить "
                "в разделе «Абитуриентам» на sibsutis.ru или в приёмной комиссии."
            )
            self.sessions.append(session_id, "user", message)
            self.sessions.append(session_id, "assistant", answer)
            return finish({"answer_markdown": answer, "sources": []}, "no_context")

        context = security.sanitize_chunks(retrieved) if retrieved else "(дополнительных фрагментов не найдено)"
        history = [
            {"role": item["role"], "content": item["content"][:LLM_HISTORY_ITEM_CHARS]}
            for item in self.sessions.history(session_id)[-LLM_HISTORY_MESSAGES:]
        ]
        messages: list[dict[str, str]] = [{"role": "system", "content": prompts.SYSTEM_PROMPT}]
        messages.extend(history)
        user_parts = [f"<knowledge_base>\n{context}\n</knowledge_base>"]
        if attached_xml:
            user_parts.append(f"<attached_context>\n{attached_xml}\n</attached_context>")
        user_parts.append(f"Вопрос пользователя: {message}")
        messages.append({"role": "user", "content": "\n\n".join(user_parts)})

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
        ctx_labels = [
            str(item.get("label", "")).strip()[:60]
            for item in attached_items
            if isinstance(item, dict) and item.get("label")
        ]
        user_history = (
            f"{message}\n[Контекст: {', '.join(ctx_labels)}]"
            if ctx_labels and not context_flags
            else message
        )
        self.sessions.append(session_id, "user", user_history)
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
