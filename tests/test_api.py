"""Проверки API управления базой знаний (снапшоты, папка, эмбеддинги)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src import config
from src.api import app

pytestmark = pytest.mark.skipif(
    not config.KB_PATH.exists(),
    reason="база знаний не собрана",
)


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


def test_database_status(client: TestClient) -> None:
    response = client.get("/api/databases")
    assert response.status_code == 200
    payload = response.json()
    assert payload["active"]["docs"] > 0
    assert payload["active"]["chunks"] > 0
    assert isinstance(payload["backups"], list)
    assert "configured" in payload["embed"]


def test_backup_restore_delete(client: TestClient) -> None:
    created = client.post("/api/databases/backup", json={"name": "pytest backup"})
    assert created.status_code == 200
    backup = created.json()["backup"]
    backup_id = backup["id"]
    assert backup["docs"] > 0

    status = client.get("/api/databases").json()
    assert any(item["id"] == backup_id for item in status["backups"])

    restored = client.post(f"/api/databases/{backup_id}/restore")
    assert restored.status_code == 200
    assert restored.json()["databases"]["active"]["docs"] == backup["docs"]

    deleted = client.delete(f"/api/databases/{backup_id}")
    assert deleted.status_code == 200
    remaining = deleted.json()["databases"]["backups"]
    assert all(item["id"] != backup_id for item in remaining)


def test_folder_rejects_paths_outside_data(client: TestClient) -> None:
    response = client.put("/api/databases/folder", json={"folder": "C:/Windows"})
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

    response = client.post("/api/embeddings/test")
    assert response.status_code == 200
    payload = response.json()
    assert payload["dimensions"] == 4
    assert payload["model"] == config.EMBED_MODEL
