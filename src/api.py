"""FastAPI-сервер агента «Справочник СибГУТИ»."""

from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from src import config, security
from src.agent import get_agent
from src.ingest import (
    DB_LOCK,
    autoupdate_status,
    build_achievements_catalog,
    build_min_scores_catalog,
    build_program_catalog,
    configure_autoupdate,
    create_backup,
    database_status,
    delete_backup,
    discover_site_sources,
    ensure_knowledge_base,
    job_status,
    load_achievements_catalog,
    load_min_scores_catalog,
    load_program_catalog,
    restore_backup,
    run_autoupdate,
    run_directory,
    run_single_file,
    run_site_rebuild,
    site_rebuild_cooldown,
    start_autoupdate_scheduler,
    start_job,
    write_app_settings,
)
from src.retrieval import build_overview, recommend_programs, refresh_overview


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    ensure_knowledge_base()
    start_autoupdate_scheduler()
    yield


app = FastAPI(title="Ассистент СибГУТИ", version="1.0.0", lifespan=_lifespan)

_DB_LOCK = DB_LOCK
_ALLOWED_UPLOADS = {".html", ".htm", ".pdf", ".txt", ".md"}
_MAX_UPLOAD_BYTES = 25 * 1024 * 1024


class ContextItem(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    summary: str = Field(default="", max_length=800)


class ChatRequest(BaseModel):
    session_id: str = Field(default="", description="Идентификатор сессии; пустой — создать новую")
    message: str = Field(min_length=1, max_length=4000)
    context_items: list[ContextItem] = Field(default_factory=list, max_length=6)


class BackupRequest(BaseModel):
    name: str = Field(default="", max_length=120)


class FolderRequest(BaseModel):
    folder: str = Field(min_length=1, max_length=500)


class DirectoryIngestRequest(BaseModel):
    path: str = Field(default="", max_length=500)


class RecommendRequest(BaseModel):
    query: str = Field(min_length=2, max_length=500)
    limit: int = Field(default=5, ge=1, le=10)


class SiteRefreshRequest(BaseModel):
    ingest: bool = Field(default=False, description="Сразу собрать базу из найденных ссылок")


class AutoUpdateRequest(BaseModel):
    enabled: bool = Field(default=True)
    interval_hours: int = Field(default=24, ge=1, le=168)


def _reset_retriever() -> None:
    agent = get_agent()
    agent.retriever = type(agent.retriever)()


def _site_rebuild_task(progress: Any) -> dict[str, Any]:
    try:
        return run_site_rebuild(progress)
    finally:
        _reset_retriever()


def _safe_error(exc: Exception) -> str:
    message = f"{type(exc).__name__}: {exc}"
    for secret in (config.LLM_API_KEY, config.EMBED_API_KEY, config.ADMIN_TOKEN):
        if secret:
            message = message.replace(secret, "***")
    return message


def require_admin(
    request: Request,
    x_admin_token: Annotated[str | None, Header(alias="X-Admin-Token")] = None,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> None:
    """Проверяет rate limit и админ-токен для тяжёлых и изменяющих операций."""
    client_ip = (request.client.host if request.client else "") or "unknown"
    allowed, retry_after = security.admin_rate_limiter.check(client_ip)
    if not allowed:
        security.log_event(
            {
                "session_id": "admin",
                "status": "rate_limited",
                "security_flags": ["rate_limit_exceeded"],
                "error": f"{request.method} {request.url.path}",
            }
        )
        raise HTTPException(
            status_code=429,
            detail=f"Слишком много запросов. Повторите через {retry_after} с.",
            headers={"Retry-After": str(retry_after)},
        )

    provided = security.extract_admin_token(x_admin_token, authorization)
    if not security.verify_admin_token(provided):
        security.log_event(
            {
                "session_id": "admin",
                "status": "unauthorized",
                "security_flags": ["admin_auth_failed"],
                "error": f"{request.method} {request.url.path}",
            }
        )
        raise HTTPException(
            status_code=401,
            detail="Требуется действующий админ-токен (заголовок X-Admin-Token)",
            headers={"WWW-Authenticate": "Bearer"},
        )


@app.post("/api/admin/verify", dependencies=[Depends(require_admin)])
def verify_admin() -> dict[str, Any]:
    return {"status": "ok", "auth_enabled": bool(config.ADMIN_TOKEN)}


@app.get("/")
def index() -> FileResponse:
    page = config.WEB_DIR / "index.html"
    if not page.exists():
        raise HTTPException(status_code=500, detail="web/index.html не найден")
    return FileResponse(page, media_type="text/html")


@app.get("/presentation")
def presentation() -> FileResponse:
    page = config.WEB_DIR / "presentation.html"
    if not page.exists():
        raise HTTPException(status_code=404, detail="web/presentation.html не найден")
    return FileResponse(page, media_type="text/html")


@app.post("/api/session/new")
def new_session() -> dict[str, str]:
    return {"session_id": get_agent().new_session()}


@app.post("/api/chat")
def chat(request: ChatRequest) -> dict[str, Any]:
    agent = get_agent()
    session_id = request.session_id or agent.new_session()
    items = [item.model_dump() for item in request.context_items]
    return agent.answer(session_id, request.message, context_items=items or None)


@app.post("/api/ingest/file", dependencies=[Depends(require_admin)])
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


@app.post("/api/databases/backup", dependencies=[Depends(require_admin)])
def backup_database(request: BackupRequest) -> dict[str, Any]:
    try:
        with _DB_LOCK:
            backup = create_backup(request.name)
    except (RuntimeError, OSError) as exc:
        raise HTTPException(status_code=409, detail=_safe_error(exc)) from exc
    return {"status": "ok", "backup": backup, "databases": database_status()}


@app.post("/api/databases/{backup_id}/restore", dependencies=[Depends(require_admin)])
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


@app.delete("/api/databases/{backup_id}", dependencies=[Depends(require_admin)])
def remove_database(backup_id: str) -> dict[str, Any]:
    try:
        with _DB_LOCK:
            delete_backup(backup_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="резервная копия не найдена") from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=_safe_error(exc)) from exc
    return {"status": "ok", "databases": database_status()}


@app.put("/api/databases/folder", dependencies=[Depends(require_admin)])
def update_folder(request: FolderRequest) -> dict[str, Any]:
    from src.ingest import resolve_kb_folder

    try:
        folder = resolve_kb_folder(request.folder)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    write_app_settings({"folder": folder.relative_to(config.BASE_DIR).as_posix()})
    return {"status": "ok", "databases": database_status()}


@app.post("/api/ingest/directory", dependencies=[Depends(require_admin)])
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


@app.post("/api/embeddings/test", dependencies=[Depends(require_admin)])
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


@app.post("/api/recommend")
def recommend(request: RecommendRequest) -> dict[str, Any]:
    items = recommend_programs(request.query, limit=request.limit)
    return {"query": request.query, "count": len(items), "items": items}


@app.get("/api/programs")
def programs() -> dict[str, Any]:
    return load_program_catalog()


@app.get("/api/overview")
def overview() -> dict[str, Any]:
    return build_overview()


@app.post("/api/overview/refresh", dependencies=[Depends(require_admin)])
def refresh_overview_api() -> dict[str, Any]:
    try:
        with _DB_LOCK:
            data = refresh_overview()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_safe_error(exc)) from exc
    return {"status": "ok", "overview": data}


@app.get("/api/achievements")
def achievements() -> dict[str, Any]:
    return load_achievements_catalog()


@app.post("/api/achievements/refresh", dependencies=[Depends(require_admin)])
def refresh_achievements() -> dict[str, Any]:
    try:
        with _DB_LOCK:
            payload = build_achievements_catalog()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_safe_error(exc)) from exc
    return {
        "status": "ok",
        "bachelor": len(payload.get("bachelor", [])),
        "master": len(payload.get("master", [])),
        "target": len(payload.get("target", [])),
        "generated_at": payload.get("generated_at", ""),
    }


@app.get("/api/min-scores")
def min_scores() -> dict[str, Any]:
    return load_min_scores_catalog()


@app.post("/api/min-scores/refresh", dependencies=[Depends(require_admin)])
def refresh_min_scores() -> dict[str, Any]:
    try:
        with _DB_LOCK:
            payload = build_min_scores_catalog()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_safe_error(exc)) from exc
    return {
        "status": "ok",
        "soo": len(payload.get("soo", [])),
        "spo": len(payload.get("spo", [])),
        "master": payload.get("master", 35),
        "generated_at": payload.get("generated_at", ""),
    }


@app.post("/api/programs/refresh", dependencies=[Depends(require_admin)])
def refresh_programs() -> dict[str, Any]:
    try:
        with _DB_LOCK:
            payload = build_program_catalog()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_safe_error(exc)) from exc
    return {
        "status": "ok",
        "count": payload.get("count", 0),
        "errors": payload.get("errors", []),
        "generated_at": payload.get("generated_at", ""),
    }


@app.post("/api/site/sources", dependencies=[Depends(require_admin)])
def site_sources() -> dict[str, Any]:
    try:
        sources = discover_site_sources()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_safe_error(exc)) from exc
    return {"status": "ok", "count": len(sources), "sources": sources}


@app.post("/api/site/refresh", dependencies=[Depends(require_admin)])
def site_refresh(request: SiteRefreshRequest) -> dict[str, Any]:
    if not request.ingest:
        try:
            sources = discover_site_sources()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=_safe_error(exc)) from exc
        return {"status": "discovered", "count": len(sources), "sources": sources}

    cooldown = site_rebuild_cooldown()
    if not cooldown["allowed"]:
        raise HTTPException(
            status_code=429,
            detail={
                "message": "Полная пересборка с сайта уже выполнялась менее суток назад",
                "next_at": cooldown["next_at"],
                "remaining_sec": cooldown["remaining_sec"],
            },
            headers={"Retry-After": str(cooldown["remaining_sec"])},
        )

    try:
        job = start_job("site_rebuild", _site_rebuild_task)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "started", "job": job}


@app.get("/api/site/rebuild-status")
def site_rebuild_status() -> dict[str, Any]:
    return site_rebuild_cooldown()


@app.get("/api/jobs/current")
def current_job() -> dict[str, Any]:
    return job_status()


@app.get("/api/autoupdate")
def autoupdate() -> dict[str, Any]:
    return autoupdate_status()


@app.post("/api/autoupdate", dependencies=[Depends(require_admin)])
def update_autoupdate(request: AutoUpdateRequest) -> dict[str, Any]:
    return configure_autoupdate(request.enabled, request.interval_hours)


@app.post("/api/autoupdate/run", dependencies=[Depends(require_admin)])
def run_autoupdate_now() -> dict[str, Any]:
    try:
        job = start_job("autoupdate", run_autoupdate)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "started", "job": job}


@app.get("/api/stats")
def stats() -> JSONResponse:
    if not config.STATS_PATH.exists():
        return JSONResponse({"status": "empty", "detail": "база знаний ещё не собрана"}, status_code=200)
    payload = json.loads(config.STATS_PATH.read_text(encoding="utf-8"))
    return JSONResponse(payload)


@app.get("/api/health")
@app.get("/health", include_in_schema=False)
def health() -> dict[str, Any]:
    return {"status": "ok", "config": config.status()}
