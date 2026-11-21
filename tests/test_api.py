"""Проверки API управления базой знаний (снапшоты, папка, эмбеддинги)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src import config, security
from src.api import app

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
