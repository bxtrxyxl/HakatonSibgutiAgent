"""Проверки устойчивости загрузки и персистентности фоновых задач."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import httpx
import numpy as np
import pytest

from src import config, ingest


def _request() -> httpx.Request:
    return httpx.Request("GET", "https://example.test/page")


def _response(status: int, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, request=_request(), headers=headers or {})


class FakeClient:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    def get(self, url: str) -> httpx.Response:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_http_get_retries_transient_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ingest.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(config, "FETCH_RETRIES", 1)
    client = FakeClient([_response(503), _response(200)])

    response = ingest._http_get(client, "https://example.test/page")

    assert response.status_code == 200
    assert client.calls == 2


def test_http_get_does_not_retry_client_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ingest.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(config, "FETCH_RETRIES", 2)
    client = FakeClient([_response(404)])

    with pytest.raises(ingest.FetchError) as error:
        ingest._http_get(client, "https://example.test/missing")

    assert error.value.reason == "client_error"
    assert client.calls == 1


def test_http_get_retries_timeout_then_reports_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ingest.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(config, "FETCH_RETRIES", 1)
    timeout = httpx.ReadTimeout("timed out", request=_request())
    client = FakeClient([timeout, timeout])

    with pytest.raises(ingest.FetchError) as error:
        ingest._http_get(client, "https://example.test/slow")

    assert error.value.reason == "timeout"
    assert client.calls == 2


def test_classify_fetch_error_reasons() -> None:
    assert ingest._classify_fetch_error(httpx.ReadTimeout("t", request=_request())) == "timeout"
    assert (
        ingest._classify_fetch_error(httpx.ConnectError("getaddrinfo failed", request=_request()))
        == "dns"
    )
    assert (
        ingest._classify_fetch_error(httpx.ConnectError("connection reset", request=_request()))
        == "connection"
    )
    assert (
        ingest._classify_fetch_error(
            httpx.RemoteProtocolError("server disconnected", request=_request())
        )
        == "protocol"
    )
    assert (
        ingest._classify_fetch_error(
            httpx.HTTPStatusError("rate", request=_request(), response=_response(429))
        )
        == "rate_limited"
    )


def _idle_job() -> dict[str, object]:
    return {
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


def _set_job_state(state: dict[str, object]) -> None:
    with ingest._JOB_GUARD:
        ingest._JOB_STATE.clear()
        ingest._JOB_STATE.update(state)


def test_restore_marks_running_job_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = tmp_path / "app_settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "folder": "data/docs",
                "last_site_rebuild_at": "2026-01-01T00:00:00+00:00",
                "last_job": {
                    "id": "job123",
                    "kind": "site_rebuild",
                    "status": "running",
                    "progress": 42,
                    "step": "Сбор страниц",
                    "started_at": "2026-01-01T00:00:00+00:00",
                    "finished_at": "",
                    "duration_ms": 0,
                    "result": None,
                    "error": "",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "APP_SETTINGS_PATH", settings_path)
    snapshot = copy.deepcopy(ingest._JOB_STATE)
    try:
        _set_job_state(_idle_job())
        ingest._restore_job_state()

        state = ingest.job_status()
        assert state["status"] == "interrupted"
        assert state["id"] == "job123"
        assert state["finished_at"]
        assert "перезапуском" in state["error"]

        persisted = json.loads(settings_path.read_text(encoding="utf-8"))
        assert persisted["last_job"]["status"] == "interrupted"
        # Прерванная пересборка не блокирует повторный запуск на сутки.
        assert persisted["last_site_rebuild_at"] == ""
    finally:
        _set_job_state(snapshot)


def test_persist_job_state_writes_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = tmp_path / "app_settings.json"
    monkeypatch.setattr(config, "APP_SETTINGS_PATH", settings_path)
    snapshot = copy.deepcopy(ingest._JOB_STATE)
    try:
        _set_job_state(
            {
                **_idle_job(),
                "id": "job9",
                "kind": "site_rebuild",
                "status": "done",
                "progress": 100,
                "step": "Готово",
            }
        )
        ingest._persist_job_state()

        persisted = json.loads(settings_path.read_text(encoding="utf-8"))
        assert persisted["last_job"]["id"] == "job9"
        assert persisted["last_job"]["status"] == "done"
    finally:
        _set_job_state(snapshot)


def test_build_program_catalog_preserves_missing_profiles_on_partial_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog_path = tmp_path / "programs.json"
    old_ok = {
        "url": "https://portal.sibsutis.ru/abitur/profile/detail.php?ID=1",
        "code": "09.03.01",
        "profile": "Old One",
    }
    old_fail = {
        "url": "https://portal.sibsutis.ru/abitur/profile/detail.php?ID=2",
        "code": "09.03.03",
        "profile": "Preserved Two",
    }
    catalog_path.write_text(json.dumps({"count": 2, "programs": [old_ok, old_fail]}), encoding="utf-8")
    monkeypatch.setattr(config, "PROGRAMS_PATH", catalog_path)
    monkeypatch.setattr(
        ingest,
        "discover_program_links",
        lambda: [{"url": old_ok["url"], "title": "1"}, {"url": old_fail["url"], "title": "2"}],
    )

    def fake_fetch(url: str, use_wayback: bool = False) -> tuple[bytes, str, str, str]:
        if url == old_fail["url"]:
            raise RuntimeError("timeout")
        return b"<html></html>", "text/html", url, "direct"

    monkeypatch.setattr(ingest, "fetch_url", fake_fetch)
    monkeypatch.setattr(
        ingest,
        "parse_program_page",
        lambda url, html: {"url": url, "code": "09.03.01", "profile": "Fresh One"},
    )

    payload = ingest.build_program_catalog()

    assert payload["count"] == 2
    assert payload["preserved"] == 1
    assert [item["profile"] for item in payload["programs"]] == ["Fresh One", "Preserved Two"]
    assert payload["errors"][0]["preserved"] is True


def test_pdf_catalogs_preserve_previous_data_when_pdf_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ach_path = tmp_path / "achievements.json"
    min_path = tmp_path / "min_scores.json"
    ach_path.write_text(
        json.dumps({"bachelor": [{"title": "Медаль", "points": 10}], "master": [], "target": []}),
        encoding="utf-8",
    )
    min_path.write_text(
        json.dumps({"soo": [{"subject": "Математика", "points": 27}], "spo": [], "master": 35}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "ACHIEVEMENTS_PATH", ach_path)
    monkeypatch.setattr(config, "MIN_SCORES_PATH", min_path)
    monkeypatch.setattr(ingest, "fetch_url", lambda url, use_wayback=False: (b"%PDF", "application/pdf", url, "direct"))
    monkeypatch.setattr(ingest, "extract_pdf", lambda data: "пустой текст без таблиц")

    with pytest.raises(RuntimeError):
        ingest.build_achievements_catalog()
    with pytest.raises(RuntimeError):
        ingest.build_min_scores_catalog()

    assert json.loads(ach_path.read_text(encoding="utf-8"))["bachelor"][0]["title"] == "Медаль"
    assert json.loads(min_path.read_text(encoding="utf-8"))["soo"][0]["subject"] == "Математика"


def test_preserve_failed_docs_from_backup_restores_missing_doc_and_vectors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    backups_dir = data_dir / "backups"
    data_dir.mkdir()
    backups_dir.mkdir()

    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "KB_PATH", data_dir / "kb.sqlite")
    monkeypatch.setattr(config, "EMBEDDINGS_PATH", data_dir / "embeddings.npy")
    monkeypatch.setattr(config, "REGISTRY_PATH", data_dir / "registry.json")
    monkeypatch.setattr(config, "STATS_PATH", data_dir / "stats.json")
    monkeypatch.setattr(config, "BACKUPS_DIR", backups_dir)

    doc1 = {
        "doc_id": "d1",
        "title": "Doc 1",
        "url": "https://sibsutis.ru/ok",
        "section": "Поступление",
        "fetched_at": "2026-01-01T00:00:00Z",
        "year_version": "2026",
        "format": "html",
        "sha256": "h1",
        "chars": 12,
        "retrieved_via": "direct",
    }
    doc2 = {
        "doc_id": "d2",
        "title": "Doc 2",
        "url": "https://sibsutis.ru/failed",
        "section": "Общежития",
        "fetched_at": "2026-01-01T00:00:00Z",
        "year_version": "2026",
        "format": "html",
        "sha256": "h2",
        "chars": 14,
        "retrieved_via": "direct",
    }

    conn = ingest.connect_db()
    ingest.init_db(conn, rebuild=True)
    ingest.store_document(conn, doc1, [{"heading": "H1", "text": "Fresh text 1"}])
    ingest.store_document(conn, doc2, [{"heading": "H2", "text": "Old text 2"}])
    conn.close()
    ingest.append_embeddings(np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32))
    backup = ingest.create_backup("before")

    # Имитируем частичную пересборку: doc1 пересобрался, doc2 не скачался.
    config.EMBEDDINGS_PATH.unlink()
    conn = ingest.connect_db()
    ingest.init_db(conn, rebuild=True)
    ingest.store_document(conn, doc1, [{"heading": "H1", "text": "Fresh text 1"}])
    conn.close()
    ingest.append_embeddings(np.asarray([[1.0, 0.0]], dtype=np.float32))

    preserved, updated_stats = ingest._preserve_failed_docs_from_backup(
        str(backup["id"]),
        {"https://sibsutis.ru/failed"},
        [{"url": "https://sibsutis.ru/failed", "error": "timeout"}],
        0,
    )

    assert preserved == 1
    assert updated_stats is not None
    assert updated_stats["unique_docs"] == 2
    assert updated_stats["chunks"] == 2
    matrix = np.load(config.EMBEDDINGS_PATH)
    assert matrix.shape == (2, 2)
    assert np.allclose(matrix[1], np.asarray([0.0, 1.0], dtype=np.float32))


def test_run_directory_fts_only_builds_sqlite_without_embeddings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docs_dir = tmp_path / "docs"
    data_dir = tmp_path / "data"
    docs_dir.mkdir()
    data_dir.mkdir()
    (docs_dir / "guide.md").write_text(
        "# Поступление 2026\n\nПодача документов в приёмную комиссию СибГУТИ начинается 20 июня 2026 года.",
        encoding="utf-8",
    )

    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "KB_PATH", data_dir / "kb.sqlite")
    monkeypatch.setattr(config, "EMBEDDINGS_PATH", data_dir / "embeddings.npy")
    monkeypatch.setattr(config, "REGISTRY_PATH", data_dir / "registry.json")
    monkeypatch.setattr(config, "STATS_PATH", data_dir / "stats.json")
    monkeypatch.setattr(
        ingest,
        "embed_texts",
        lambda *_args, **_kwargs: pytest.fail("embed_texts не должен вызываться в режиме --fts-only"),
    )

    stats = ingest.run_directory(docs_dir, with_embeddings=False)

    assert stats["unique_docs"] == 1
    assert stats["chunks"] >= 1
    assert config.KB_PATH.exists()
    assert not config.EMBEDDINGS_PATH.exists()
    assert ingest.check_kb()[0] is True

    # Рассинхрон векторов делает базу непригодной для check_kb, а поиск не падает.
    np.save(config.EMBEDDINGS_PATH, np.ones((5, 2), dtype=np.float32))
    assert ingest.check_kb()[0] is False
    from src.retrieval import HybridRetriever

    found = HybridRetriever(config.KB_PATH, config.EMBEDDINGS_PATH).search("приёмную комиссию документов")
    assert found


def test_check_kb_flags_stale_docs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    docs_dir = tmp_path / "docs"
    data_dir = tmp_path / "data"
    docs_dir.mkdir()
    data_dir.mkdir()
    doc = docs_dir / "guide.md"
    doc.write_text(
        "# Поступление 2026\n\n"
        "Документы в приёмную комиссию СибГУТИ принимают с 20 июня по 25 июля 2026 года. "
        "Подать заявление можно лично, через портал абитуриента или по почте. "
        "Для поступления на бюджет нужны результаты ЕГЭ и индивидуальные достижения.",
        encoding="utf-8",
    )

    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "DOCS_DIR", docs_dir)
    monkeypatch.setattr(config, "KB_PATH", data_dir / "kb.sqlite")
    monkeypatch.setattr(config, "EMBEDDINGS_PATH", data_dir / "embeddings.npy")
    monkeypatch.setattr(config, "REGISTRY_PATH", data_dir / "registry.json")
    monkeypatch.setattr(config, "STATS_PATH", data_dir / "stats.json")

    ingest.run_directory(docs_dir, with_embeddings=False)
    assert ingest.check_kb()[0] is True

    # После правки документа база устаревает и start.bat должен её пересобрать.
    newer = config.KB_PATH.stat().st_mtime + 5
    os.utime(doc, (newer, newer))
    ok, reason = ingest.check_kb()
    assert ok is False
    assert "пересбор" in reason


def test_ensure_knowledge_base_builds_from_project_docs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docs_dir = tmp_path / "docs"
    data_dir = tmp_path / "data"
    docs_dir.mkdir()
    data_dir.mkdir()
    (docs_dir / "guide.md").write_text(
        "# Поступление 2026\n\n"
        "Документы в приёмную комиссию СибГУТИ принимают с 20 июня по 25 июля 2026 года. "
        "Подать заявление можно лично или через портал абитуриента.",
        encoding="utf-8",
    )

    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "DOCS_DIR", docs_dir)
    monkeypatch.setattr(config, "KB_PATH", data_dir / "kb.sqlite")
    monkeypatch.setattr(config, "EMBEDDINGS_PATH", data_dir / "embeddings.npy")
    monkeypatch.setattr(config, "REGISTRY_PATH", data_dir / "registry.json")
    monkeypatch.setattr(config, "STATS_PATH", data_dir / "stats.json")
    monkeypatch.setattr(config, "require_embeddings", lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(
        ingest, "embed_query", lambda *_args, **_kwargs: pytest.fail("embed_query не должен вызываться")
    )

    result = ingest.ensure_knowledge_base()

    assert result["status"] == "built"
    assert result["embeddings"] is False
    assert config.KB_PATH.exists()
    assert not config.EMBEDDINGS_PATH.exists()
    assert ingest.check_kb()[0] is True

    again = ingest.ensure_knowledge_base()
    assert again["status"] == "ready"


def test_backup_restore_without_embeddings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    docs_dir = tmp_path / "docs"
    data_dir = tmp_path / "data"
    docs_dir.mkdir()
    data_dir.mkdir()
    (docs_dir / "guide.md").write_text(
        "# Поступление 2026\n\n"
        "Документы в приёмную комиссию СибГУТИ принимают с 20 июня по 25 июля 2026 года. "
        "Подать заявление можно лично или через портал абитуриента.",
        encoding="utf-8",
    )

    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "DOCS_DIR", docs_dir)
    monkeypatch.setattr(config, "KB_PATH", data_dir / "kb.sqlite")
    monkeypatch.setattr(config, "EMBEDDINGS_PATH", data_dir / "embeddings.npy")
    monkeypatch.setattr(config, "REGISTRY_PATH", data_dir / "registry.json")
    monkeypatch.setattr(config, "STATS_PATH", data_dir / "stats.json")
    monkeypatch.setattr(config, "BACKUPS_DIR", data_dir / "backups")
    monkeypatch.setattr(config, "APP_SETTINGS_PATH", data_dir / "app_settings.json")

    ingest.run_directory(docs_dir, with_embeddings=False)
    assert not config.EMBEDDINGS_PATH.exists()

    backup = ingest.create_backup("fts")
    assert backup["embeddings"] is False
    assert (config.BACKUPS_DIR / backup["id"] / "kb.sqlite").exists()

    config.KB_PATH.unlink()
    restored = ingest.restore_backup(backup["id"])
    assert restored["has_kb"] is True
    assert config.KB_PATH.exists()

    ingest.delete_backup(backup["id"])
    assert not (config.BACKUPS_DIR / backup["id"]).exists()
