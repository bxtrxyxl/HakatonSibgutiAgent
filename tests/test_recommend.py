"""Проверки каталога направлений и подбора по стеку/профессии."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src import config
from src.agent import SibSUTISAgent
from src.api import app
from src.retrieval import (
    build_overview,
    catalog_matches,
    format_catalog,
    match_program,
    recommend_programs,
)

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


def test_api_overview(client: TestClient) -> None:
    payload = build_overview(force_refresh=True)
    assert payload["tuition"]
    assert len(payload["scoreDirections"]) >= 4
    assert payload["minSubjectScores"]["subjects"]
    assert payload["seats"]

    response = client.get("/api/overview")
    assert response.status_code == 200
    api_payload = response.json()
    assert api_payload["kb_fingerprint"] == payload["kb_fingerprint"]


def test_overview_covers_all_catalog_directions() -> None:
    payload = build_overview(force_refresh=True)
    directions = payload["scoreDirections"]
    catalog = json.loads(config.PROGRAMS_PATH.read_text(encoding="utf-8"))["programs"]
    expected = {
        item["code"]
        for item in catalog
        if item.get("level") in {"Бакалавриат", "Специалитет", "Магистратура"}
    }
    codes = {item["code"] for item in directions}
    assert codes == expected
    assert len(codes) == 20

    by_code = {item["code"]: item for item in directions}
    assert by_code["09.04.01"]["levelRu"] == "Магистратура"
    assert by_code["09.04.01"]["budget"] > 0
    assert by_code["09.04.01"]["paid"] == 35
    assert by_code["09.04.01"]["paidIsMinThreshold"] is True
    assert by_code["10.04.01"]["paid"] == 35
    assert by_code["10.04.01"]["paidIsMinThreshold"] is True
    assert "заочная (ДОТ)" in by_code["20.03.01"]["formsRu"]
    assert by_code["09.03.02"]["levelRu"] == "Бакалавриат"
    assert by_code["09.03.02"]["formsRu"]


def test_overview_paid_scores_and_cache_persistence(client: TestClient) -> None:
    response = client.get("/api/overview")
    assert response.status_code == 200
    payload = response.json()

    directions = payload["scoreDirections"]
    by_code = {item["code"]: item for item in directions}
    assert "42.03.01" in by_code
    assert by_code["42.03.01"]["budget"] == 0
    assert by_code["42.03.01"]["paid"] == 145
    assert by_code["42.03.01"]["paidIsMinThreshold"] is False

    assert "09.03.01" in by_code
    assert by_code["09.03.01"]["budget"] == 214
    assert by_code["09.03.01"]["paid"] >= 90
    assert by_code["09.03.01"]["paidIsMinThreshold"] is True

    for item in directions:
        if item["paidSeats"] > 0:
            assert item["paid"] > 0, f"{item['code']} has zero paid score"
        desc = item["descriptionRu"]
        if desc:
            assert desc[-1] in ".!?…", f"{item['code']} truncated mid-word: {desc[-30:]}"

    assert config.OVERVIEW_CACHE_PATH.exists()
    cached = json.loads(config.OVERVIEW_CACHE_PATH.read_text(encoding="utf-8"))
    assert cached["kb_fingerprint"] == payload["kb_fingerprint"]


def test_api_achievements(client: TestClient) -> None:
    response = client.get("/api/achievements")
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["bachelor"]) >= 15
    assert len(payload["master"]) >= 10
    assert payload["max_bachelor"] == 10


def test_api_min_scores(client: TestClient) -> None:
    response = client.get("/api/min-scores")
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["soo"]) >= 5
    assert payload["master"] == 35
    assert any(item["subject"] == "Математика" and item["points"] == 27 for item in payload["soo"])


def test_catalog_matches_finds_direction_not_in_docs() -> None:
    items = catalog_matches("Расскажи про 42.03.01 Реклама и связи с общественностью")
    assert items
    assert items[0]["code"] == "42.03.01"
    digest = format_catalog(items)
    assert "42.03.01" in digest
    assert "платно" in digest or "бюджет" in digest


def test_match_program_returns_full_time_record() -> None:
    program = match_program("10.03.01 Информационная безопасность")
    assert program is not None
    assert program["code"] == "10.03.01"
    line = format_catalog([program])
    assert "проходной 2026" in line
    assert "ЕГЭ:" in line


def test_chance_answer_asks_for_scores() -> None:
    agent = SibSUTISAgent(retriever=SimpleNamespace())  # type: ignore[arg-type]
    session = agent.sessions.new_session()
    result = agent._chance_answer(session, "Рассчитай шанс поступления на 09.03.02")
    assert result is not None
    answer = result["answer_markdown"]
    assert "балл" in answer.lower()
    assert "09.03.02" in answer


def test_chance_answer_compares_scores_with_pass_score() -> None:
    agent = SibSUTISAgent(retriever=SimpleNamespace())  # type: ignore[arg-type]
    session = agent.sessions.new_session()
    result = agent._chance_answer(session, "09.03.02, баллы 90 85 80")
    assert result is not None
    answer = result["answer_markdown"]
    assert "255" in answer
    assert "ориентир" in answer
