"""Проверки каталога направлений и подбора по стеку/профессии."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src import config
from src.api import app
from src.retrieval import recommend_programs

pytestmark = pytest.mark.skipif(
    not config.PROGRAMS_PATH.exists(),
    reason="каталог не собран: python -m src.ingest --programs",
)


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


def test_catalog_has_profiles() -> None:
    items = recommend_programs("python машинное обучение", limit=5)
    assert items
    assert items[0]["code"] == "02.03.02"
    assert items[0]["fit"] >= 70
    assert items[0]["disciplines"]
    assert items[0]["professions"]


def test_backend_recommendation() -> None:
    items = recommend_programs("backend Java SQL", limit=5)
    assert items
    assert all(item["url"].startswith("https://portal.sibsutis.ru/") for item in items)


def test_api_recommend(client: TestClient) -> None:
    response = client.post("/api/recommend", json={"query": "frontend TypeScript", "limit": 3})
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] >= 1
    assert len(payload["items"]) <= 3


def test_api_programs(client: TestClient) -> None:
    response = client.get("/api/programs")
    assert response.status_code == 200
    assert response.json()["count"] >= 30
