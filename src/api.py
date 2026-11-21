"""FastAPI-сервер агента «Справочник СибГУТИ»."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from src import config
from src.agent import get_agent
from src.ingest import (
    create_backup,
    database_status,
    delete_backup,
    restore_backup,
    run_directory,
    run_single_file,
    write_app_settings,
)

app = FastAPI(title="Ассистент СибГУТИ", version="1.0.0")

_DB_LOCK = threading.Lock()
_ALLOWED_UPLOADS = {".html", ".htm", ".pdf", ".txt", ".md"}
_MAX_UPLOAD_BYTES = 25 * 1024 * 1024


class ChatRequest(BaseModel):
    session_id: str = Field(default="", description="Идентификатор сессии; пустой — создать новую")
    message: str = Field(min_length=1, max_length=4000)


class BackupRequest(BaseModel):
    name: str = Field(default="", max_length=120)


class FolderRequest(BaseModel):
    folder: str = Field(min_length=1, max_length=500)


class DirectoryIngestRequest(BaseModel):
    path: str = Field(default="", max_length=500)


def _reset_retriever() -> None:
    agent = get_agent()
    agent.retriever = type(agent.retriever)()


def _safe_error(exc: Exception) -> str:
    message = f"{type(exc).__name__}: {exc}"
    for secret in (config.LLM_API_KEY, config.EMBED_API_KEY):
        if secret:
            message = message.replace(secret, "***")
    return message


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
async def ingest_file(
    file: Annotated[UploadFile, File()],
    url: Annotated[str, Form()] = "",
    title: Annotated[str, Form()] = "",
    section: Annotated[str, Form()] = "Прочее",
    year: Annotated[str, Form()] = "",
) -> dict[str, Any]:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _ALLOWED_UPLOADS:
        raise HTTPException(status_code=400, detail="поддерживаются HTML, PDF, TXT, MD")

    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=400, detail="файл пустой")
    if len(payload) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="файл больше 25 МБ")

    descriptor, temp_name = tempfile.mkstemp(suffix=suffix, dir=config.DATA_DIR)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
        with _DB_LOCK:
            stats = run_single_file(
                temp_path,
                url=url or f"local://{file.filename}",
                title=title or Path(file.filename or "document").stem,
                section=section,
                year=year or None,
            )
        _reset_retriever()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"не удалось проиндексировать: {_safe_error(exc)}") from exc
    finally:
        temp_path.unlink(missing_ok=True)

    return {
        "status": "ok",
        "documents": stats["unique_docs"],
        "chunks": stats["chunks"],
        "errors": stats["errors"],
    }


@app.get("/api/databases")
def databases() -> dict[str, Any]:
    return database_status()


@app.post("/api/databases/backup")
def backup_database(request: BackupRequest) -> dict[str, Any]:
    try:
        with _DB_LOCK:
            backup = create_backup(request.name)
    except (RuntimeError, OSError) as exc:
        raise HTTPException(status_code=409, detail=_safe_error(exc)) from exc
    return {"status": "ok", "backup": backup, "databases": database_status()}


@app.post("/api/databases/{backup_id}/restore")
def restore_database(backup_id: str) -> dict[str, Any]:
    try:
        with _DB_LOCK:
            restore_backup(backup_id)
        _reset_retriever()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="резервная копия не найдена") from exc
    except (ValueError, RuntimeError, OSError) as exc:
        raise HTTPException(status_code=400, detail=_safe_error(exc)) from exc
    return {"status": "ok", "databases": database_status()}


@app.delete("/api/databases/{backup_id}")
def remove_database(backup_id: str) -> dict[str, Any]:
    try:
        with _DB_LOCK:
            delete_backup(backup_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="резервная копия не найдена") from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=_safe_error(exc)) from exc
    return {"status": "ok", "databases": database_status()}


@app.put("/api/databases/folder")
def update_folder(request: FolderRequest) -> dict[str, Any]:
    from src.ingest import resolve_kb_folder

    try:
        folder = resolve_kb_folder(request.folder)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    write_app_settings({"folder": folder.relative_to(config.BASE_DIR).as_posix()})
    return {"status": "ok", "databases": database_status()}


@app.post("/api/ingest/directory")
def ingest_directory(request: DirectoryIngestRequest) -> dict[str, Any]:
    from src.ingest import read_app_settings, resolve_kb_folder

    folder_value = request.path.strip() or str(read_app_settings().get("folder", ""))
    try:
        folder = resolve_kb_folder(folder_value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        with _DB_LOCK:
            stats = run_directory(folder, rebuild=True)
        _reset_retriever()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=_safe_error(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_safe_error(exc)) from exc

    write_app_settings({"folder": folder.relative_to(config.BASE_DIR).as_posix()})
    return {"status": "ok", "stats": stats, "databases": database_status()}


@app.post("/api/embeddings/test")
def test_embeddings() -> dict[str, Any]:
    try:
        config.require_embeddings()
    except config.ConfigError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    from openai import OpenAI

    client = OpenAI(
        base_url=config.EMBED_BASE_URL,
        api_key=config.EMBED_API_KEY,
        timeout=config.REQUEST_TIMEOUT,
        max_retries=0,
    )
    started = time.perf_counter()
    try:
        response = client.embeddings.create(model=config.EMBED_MODEL, input=["ping"])
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_safe_error(exc)) from exc

    vector = response.data[0].embedding
    return {
        "status": "ok",
        "model": config.EMBED_MODEL,
        "dimensions": len(vector),
        "latency_ms": int((time.perf_counter() - started) * 1000),
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
