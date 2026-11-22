"""Проверки API управления базой знаний (снапшоты, папка, эмбеддинги)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src import config, security
from src.agent import get_agent
from src.api import app
from src.ingest import site_rebuild_cooldown
from src.retrieval import build_overview, refresh_overview

pytestmark = pytest.mark.skipif(
    not config.KB_PATH.exists(),
    reason="база знаний не собрана",
)


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    security.admin_rate_limiter.reset()


def _admin_headers() -> dict[str, str]:
    return {"X-Admin-Token": config.ADMIN_TOKEN} if config.ADMIN_TOKEN else {}


def test_database_status(client: TestClient) -> None:
    response = client.get("/api/databases")
    assert response.status_code == 200
    payload = response.json()
    assert payload["active"]["docs"] > 0
    assert payload["active"]["chunks"] > 0
    assert isinstance(payload["backups"], list)
    assert "configured" in payload["embed"]


def test_backup_restore_delete(client: TestClient) -> None:
    headers = _admin_headers()
    created = client.post("/api/databases/backup", json={"name": "pytest backup"}, headers=headers)
    assert created.status_code == 200
    backup = created.json()["backup"]
    backup_id = backup["id"]
    assert backup["docs"] > 0

    status = client.get("/api/databases").json()
    assert any(item["id"] == backup_id for item in status["backups"])

    restored = client.post(f"/api/databases/{backup_id}/restore", headers=headers)
    assert restored.status_code == 200
    assert restored.json()["databases"]["active"]["docs"] == backup["docs"]

    deleted = client.delete(f"/api/databases/{backup_id}", headers=headers)
    assert deleted.status_code == 200
    remaining = deleted.json()["databases"]["backups"]
    assert all(item["id"] != backup_id for item in remaining)


def test_folder_rejects_paths_outside_data(client: TestClient) -> None:
    response = client.put("/api/databases/folder", json={"folder": "C:/Windows"}, headers=_admin_headers())
    assert response.status_code == 400
    assert "data/" in response.json()["detail"]


def test_embedding_probe_uses_server_config(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    if not config.EMBED_MODEL or not config.EMBED_API_KEY:
        pytest.skip("эмбеддинги не настроены")

    class FakeEmbeddings:
        def create(self, model: str, input: list[str]) -> SimpleNamespace:
            assert input
            return SimpleNamespace(data=[SimpleNamespace(embedding=[0.1, 0.2, 0.3, 0.4])])

    class FakeClient:
        embeddings = FakeEmbeddings()

    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: FakeClient())

    response = client.post("/api/embeddings/test", headers=_admin_headers())
    assert response.status_code == 200
    payload = response.json()
    assert payload["dimensions"] == 4
    assert payload["model"] == config.EMBED_MODEL


def test_admin_auth_blocks_heavy_endpoints_without_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "secret-admin-token-42"
    monkeypatch.setattr(config, "ADMIN_TOKEN", secret)
    monkeypatch.setattr(config, "ADMIN_RATE_LIMIT_MAX", 100)

    protected_calls = [
        ("POST", "/api/site/refresh", {"json": {"ingest": False}}),
        ("POST", "/api/site/sources", {}),
        ("POST", "/api/ingest/directory", {"json": {"path": "data/docs"}}),
        ("POST", "/api/ingest/file", {"files": {"file": ("note.md", b"# Test", "text/markdown")}}),
        ("POST", "/api/programs/refresh", {}),
        ("POST", "/api/achievements/refresh", {}),
        ("POST", "/api/min-scores/refresh", {}),
        ("POST", "/api/overview/refresh", {}),
        ("POST", "/api/autoupdate", {"json": {"enabled": False, "interval_hours": 24}}),
        ("POST", "/api/autoupdate/run", {}),
        ("POST", "/api/databases/backup", {"json": {"name": "test"}}),
        ("POST", "/api/databases/fake-id/restore", {}),
        ("DELETE", "/api/databases/fake-id", {}),
        ("PUT", "/api/databases/folder", {"json": {"folder": "data/docs"}}),
        ("POST", "/api/embeddings/test", {}),
        ("POST", "/api/admin/verify", {}),
    ]

    for method, path, kwargs in protected_calls:
        no_auth = client.request(method, path, **kwargs)
        assert no_auth.status_code == 401, f"{method} {path} returned {no_auth.status_code} without token"
        wrong_auth = client.request(method, path, headers={"X-Admin-Token": "wrong-token"}, **kwargs)
        assert wrong_auth.status_code == 401, f"{method} {path} returned {wrong_auth.status_code} with wrong token"

    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["config"]["admin_auth_enabled"] is True
    assert secret not in health.text

    dbs = client.get("/api/databases")
    assert dbs.status_code == 200
    assert dbs.json()["admin"]["auth_enabled"] is True
    assert secret not in dbs.text

    for public_path in ("/api/stats", "/api/programs"):
        public_resp = client.get(public_path)
        assert public_resp.status_code == 200
        assert secret not in public_resp.text


def test_admin_auth_accepts_x_admin_token_and_bearer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "secret-admin-token-42"
    monkeypatch.setattr(config, "ADMIN_TOKEN", secret)
    monkeypatch.setattr(config, "ADMIN_RATE_LIMIT_MAX", 100)

    by_header = client.post("/api/admin/verify", headers={"X-Admin-Token": secret})
    assert by_header.status_code == 200
    assert by_header.json() == {"status": "ok", "auth_enabled": True}

    by_bearer = client.post("/api/admin/verify", headers={"Authorization": f"Bearer {secret}"})
    assert by_bearer.status_code == 200
    assert by_bearer.json() == {"status": "ok", "auth_enabled": True}


def test_admin_rate_limit_returns_429(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "secret-admin-token-42"
    monkeypatch.setattr(config, "ADMIN_TOKEN", secret)
    monkeypatch.setattr(config, "ADMIN_RATE_LIMIT_MAX", 2)
    monkeypatch.setattr(config, "ADMIN_RATE_LIMIT_WINDOW_SEC", 60)
    security.admin_rate_limiter.reset()

    headers = {"X-Admin-Token": secret}
    assert client.post("/api/admin/verify", headers=headers).status_code == 200
    assert client.post("/api/admin/verify", headers=headers).status_code == 200

    limited = client.post("/api/admin/verify", headers=headers)
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1


def test_admin_token_redacted_from_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "ADMIN_TOKEN", "super-secret-xyz-999")
    cleaned = security.redact("failed with super-secret-xyz-999 and X-Admin-Token: another-secret")
    assert "super-secret-xyz-999" not in cleaned
    assert "another-secret" not in cleaned


def test_site_rebuild_cooldown_allows_once_per_day(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = tmp_path / "app_settings.json"
    monkeypatch.setattr(config, "APP_SETTINGS_PATH", settings_path)

    assert site_rebuild_cooldown()["allowed"] is True
    status = client.get("/api/site/rebuild-status")
    assert status.status_code == 200
    assert status.json()["allowed"] is True

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    settings_path.write_text(
        json.dumps({"folder": "data/docs", "last_site_rebuild_at": now}, ensure_ascii=False),
        encoding="utf-8",
    )

    blocked = site_rebuild_cooldown()
    assert blocked["allowed"] is False
    assert 0 < blocked["remaining_sec"] <= 24 * 60 * 60
    assert blocked["next_at"]

    status = client.get("/api/site/rebuild-status")
    assert status.status_code == 200
    assert status.json()["allowed"] is False

    blocked_call = client.post(
        "/api/site/refresh", json={"ingest": True}, headers=_admin_headers()
    )
    assert blocked_call.status_code == 429
    assert int(blocked_call.headers["Retry-After"]) >= 1
    assert blocked_call.json()["detail"]["next_at"]


def test_overview_refresh_persists_snapshot(client: TestClient) -> None:
    try:
        refreshed = client.post("/api/overview/refresh", headers=_admin_headers())
        assert refreshed.status_code == 200
        payload = refreshed.json()["overview"]
        assert payload["method"] == "kb+catalog"
        assert payload["kb_docs"] > 0
        assert payload["kb_fingerprint"]

        # Снимок сохраняется в data/overview_cache.json и отдаётся в GET /api/overview
        persisted = client.get("/api/overview")
        assert persisted.status_code == 200
        cached = persisted.json()
        assert cached["kb_fingerprint"] == payload["kb_fingerprint"]
        assert [item["count"] for item in cached["seats"]] == [
            item["count"] for item in payload["seats"]
        ]

        # Повторный пересчёт из БД даёт те же цифры без обращения к LLM
        again = refresh_overview()
        assert again["method"] == "kb+catalog"
        assert [item["count"] for item in again["seats"]] == [
            item["count"] for item in payload["seats"]
        ]
    finally:
        build_overview(force_refresh=True)


def test_chat_accepts_attached_context_items(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    agent = get_agent()
    captured: dict[str, object] = {}

    def fake_search(query: str, top_k: int | None = None) -> list[dict[str, object]]:
        captured["search_query"] = query
        return [
            {
                "chunk_id": 1,
                "doc_id": "doc-1",
                "title": "Проходные баллы СибГУТИ",
                "url": "https://sibsutis.ru/scores",
                "section": "Поступление",
                "year_version": "2026",
                "text": "Проходной балл на бюджет ИСиТ — 251.",
            }
        ]

    def fake_chat(messages: list[dict[str, str]], **kwargs: object) -> str:
        captured["messages"] = messages
        return "По прикреплённой карточке максимум — 251 балл на ИСиТ [1]."

    monkeypatch.setattr(agent.retriever, "search", fake_search)
    monkeypatch.setattr(agent, "_chat", fake_chat)

    response = client.post(
        "/api/chat",
        json={
            "message": "Что важно по этим параметрам?",
            "context_items": [
                {
                    "label": "Проходные баллы · Бюджет",
                    "summary": "Максимум 251 (ИСиТ), минимум 207 (ИБТС).",
                }
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert "251" in payload["answer_markdown"]
    assert len(payload["sources"]) == 1
    assert "Проходные баллы · Бюджет" in str(captured["search_query"])
    messages = captured["messages"]
    assert isinstance(messages, list)
    last_user = messages[-1]["content"]
    assert "<attached_context>" in last_user
    assert 'label="Проходные баллы · Бюджет"' in last_user
    assert "Максимум 251 (ИСиТ)" in last_user
    with agent.sessions._lock:
        agent.sessions._sessions.pop(payload["session_id"], None)
