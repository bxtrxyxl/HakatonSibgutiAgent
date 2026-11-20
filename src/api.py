"""FastAPI-сервер агента «Справочник СибГУТИ»."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from src import config
from src.agent import get_agent
from src.ingest import run_single_file

app = FastAPI(title="Ассистент СибГУТИ", version="1.0.0")


class ChatRequest(BaseModel):
    session_id: str = Field(default="", description="Идентификатор сессии; пустой — создать новую")
    message: str = Field(min_length=1, max_length=4000)


class IngestRequest(BaseModel):
    path: str = Field(min_length=1, description="Путь к локальному файлу (HTML/PDF/TXT/MD)")
    url: str = ""
    title: str = ""
    section: str = "Прочее"
    year: str = ""


@app.get("/")
def index() -> FileResponse:
    page = config.WEB_DIR / "index.html"
    if not page.exists():
        raise HTTPException(status_code=500, detail="web/index.html не найден")
    return FileResponse(page, media_type="text/html")


@app.post("/api/session/new")
def new_session() -> dict[str, str]:
    return {"session_id": get_agent().new_session()}


@app.post("/api/chat")
def chat(request: ChatRequest) -> dict[str, Any]:
    agent = get_agent()
    session_id = request.session_id or agent.new_session()
    return agent.answer(session_id, request.message)


@app.post("/api/ingest/file")
def ingest_file(request: IngestRequest) -> dict[str, Any]:
    path = Path(request.path).expanduser()
    if not path.is_absolute():
        path = config.BASE_DIR / path
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"файл не найден: {path}")
    if path.suffix.lower() not in {".html", ".htm", ".pdf", ".txt", ".md"}:
        raise HTTPException(status_code=400, detail="поддерживаются HTML, PDF, TXT, MD")
    try:
        stats = run_single_file(
            path,
            url=request.url or f"local://{path.name}",
            title=request.title or path.stem,
            section=request.section,
            year=request.year or None,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"не удалось проиндексировать: {exc}") from exc
    # Новый документ меняет индекс — сбрасываем кэш ретривера.
    get_agent().retriever = type(get_agent().retriever)()
    return {
        "status": "ok",
        "documents": stats["unique_docs"],
        "chunks": stats["chunks"],
        "errors": stats["errors"],
    }


@app.get("/api/stats")
def stats() -> JSONResponse:
    if not config.STATS_PATH.exists():
        return JSONResponse({"status": "empty", "detail": "база знаний ещё не собрана"}, status_code=200)
    payload = json.loads(config.STATS_PATH.read_text(encoding="utf-8"))
    return JSONResponse(payload)


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "config": config.status()}
