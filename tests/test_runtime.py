"""Проверки потокобезопасности, ограничений памяти и кэшей времени выполнения."""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pytest

from src import config, ingest, security
from src.agent import SessionStore
from src.retrieval import HybridRetriever


def test_session_store_evicts_oldest_sessions() -> None:
    store = SessionStore(limit=2)
    first = store.new_session()
    second = store.new_session()
    third = store.new_session()

    assert not store.exists(first)
    assert store.exists(second)
    assert store.exists(third)


def test_session_store_history_is_a_copy() -> None:
    store = SessionStore(limit=4)
    session_id = store.new_session()
    store.append(session_id, "user", "привет")

    snapshot = store.history(session_id)
    snapshot.append({"role": "user", "content": "подмена"})

    assert store.history(session_id) == [{"role": "user", "content": "привет"}]


def test_session_store_keeps_history_within_limit() -> None:
    store = SessionStore(limit=4)
    session_id = store.new_session()
    for index in range(40):
        store.append(session_id, "user", f"message-{index}")

    history = store.history(session_id)
    assert len(history) == 16
    assert history[-1]["content"] == "message-39"


def test_rate_limiter_prunes_stale_buckets(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"now": 1000.0}
    monkeypatch.setattr(security.time, "monotonic", lambda: clock["now"])
    limiter = security.RateLimiter()

    assert limiter.check("a", limit=1, window_sec=10) == (True, 0)
    assert limiter.check("a", limit=1, window_sec=10)[0] is False

    clock["now"] += 20.0
    assert limiter.check("b", limit=1, window_sec=10) == (True, 0)

    assert "a" not in limiter._buckets
    assert "b" in limiter._buckets


def test_catalog_cache_returns_copies_and_refreshes_by_mtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog_path = tmp_path / "programs.json"
    catalog_path.write_text(json.dumps({"count": 1, "programs": [{"id": "a"}]}), encoding="utf-8")
    monkeypatch.setattr(config, "PROGRAMS_PATH", catalog_path)

    first = ingest.load_program_catalog()
    first["programs"].append({"id": "mutated"})
    second = ingest.load_program_catalog()

    assert second["programs"] == [{"id": "a"}]

    catalog_path.write_text(json.dumps({"count": 1, "programs": [{"id": "b"}]}), encoding="utf-8")
    os.utime(catalog_path, (4_000_000_000, 4_000_000_000))
    third = ingest.load_program_catalog()

    assert third["programs"] == [{"id": "b"}]


def test_catalog_cache_falls_back_when_file_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "MIN_SCORES_PATH", tmp_path / "missing.json")

    payload = ingest.load_min_scores_catalog()

    assert payload == {"soo": [], "spo": [], "master": 35}


def test_retriever_caches_id_positions() -> None:
    retriever = HybridRetriever()
    retriever._embeddings = np.ones((2, 2), dtype=np.float32)
    retriever._chunk_ids = np.array([5, 9], dtype=np.int64)

    first = retriever._id_positions()
    second = retriever._id_positions()

    assert first is second
    assert first == {5: 0, 9: 1}
