"""Проверки устойчивости загрузки и персистентности фоновых задач."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import httpx
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
