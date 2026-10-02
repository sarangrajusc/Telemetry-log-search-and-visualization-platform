"""
Test suite for the telemetry platform API.

Run with:
    cd backend
    TESTING=1 pytest -v

Uses a throwaway SQLite DB (set via DB_PATH) so tests never touch the real
telemetry.db, and TESTING=1 so main.py skips mounting the frontend static
files (not relevant to API tests, and avoids a missing-directory error in CI).
"""
import os
import tempfile

os.environ["TESTING"] = "1"
db_fd, db_path = tempfile.mkstemp(suffix=".db")
os.environ["DB_PATH"] = db_path
os.environ.pop("API_KEY", None)  # auth off by default for most tests

import pytest
from fastapi.testclient import TestClient

import database
database.DB_PATH = db_path  # module-level constant was read at import time elsewhere; keep in sync

import main

client = TestClient(main.app)


@pytest.fixture(autouse=True)
def fresh_db():
    """Reset the database before every test so tests don't leak state into each other."""
    database.init_db()
    with database.get_conn() as conn:
        conn.execute("DELETE FROM logs")
        conn.execute("DELETE FROM logs_fts")
    yield


def make_log(**overrides):
    log = {"source": "auth-service", "level": "INFO", "message": "user logged in"}
    log.update(overrides)
    return log


def test_health():
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "healthy"}


def test_ingest_single_log():
    resp = client.post("/api/logs/ingest", json=make_log())
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "id" in body


def test_ingest_rejects_invalid_level():
    resp = client.post("/api/logs/ingest", json=make_log(level="CRITICAL"))
    assert resp.status_code == 422


def test_ingest_rejects_blank_message():
    resp = client.post("/api/logs/ingest", json=make_log(message="   "))
    assert resp.status_code == 422


def test_ingest_batch():
    logs = [make_log(message=f"event {i}") for i in range(5)]
    resp = client.post("/api/logs/ingest/batch", json={"logs": logs})
    assert resp.status_code == 200
    assert resp.json()["ingested"] == 5


def test_ingest_batch_rejects_empty_list():
    resp = client.post("/api/logs/ingest/batch", json={"logs": []})
    assert resp.status_code in (400, 422)


def test_search_full_text():
    client.post("/api/logs/ingest", json=make_log(message="connection timeout to upstream"))
    client.post("/api/logs/ingest", json=make_log(message="request completed successfully"))

    resp = client.get("/api/logs/search", params={"q": "timeout"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert "timeout" in body["results"][0]["message"]


def test_search_filters_by_level():
    client.post("/api/logs/ingest", json=make_log(level="ERROR", message="failure one"))
    client.post("/api/logs/ingest", json=make_log(level="INFO", message="all good"))

    resp = client.get("/api/logs/search", params={"level": "ERROR"})
    body = resp.json()
    assert body["total"] == 1
    assert body["results"][0]["level"] == "ERROR"


def test_search_filters_by_source():
    client.post("/api/logs/ingest", json=make_log(source="payments-api"))
    client.post("/api/logs/ingest", json=make_log(source="auth-service"))

    resp = client.get("/api/logs/search", params={"source": "payments-api"})
    body = resp.json()
    assert body["total"] == 1
    assert body["results"][0]["source"] == "payments-api"


def test_search_pagination():
    for i in range(12):
        client.post("/api/logs/ingest", json=make_log(message=f"event {i}"))

    resp = client.get("/api/logs/search", params={"page": 1, "page_size": 5})
    body = resp.json()
    assert body["total"] == 12
    assert len(body["results"]) == 5


def test_stats_levels():
    client.post("/api/logs/ingest", json=make_log(level="ERROR"))
    client.post("/api/logs/ingest", json=make_log(level="ERROR"))
    client.post("/api/logs/ingest", json=make_log(level="INFO"))

    resp = client.get("/api/stats/levels")
    counts = {row["level"]: row["count"] for row in resp.json()}
    assert counts["ERROR"] == 2
    assert counts["INFO"] == 1


def test_stats_sources():
    client.post("/api/logs/ingest", json=make_log(source="gateway"))
    client.post("/api/logs/ingest", json=make_log(source="gateway"))
    client.post("/api/logs/ingest", json=make_log(source="search-index"))

    resp = client.get("/api/stats/sources")
    sources = {row["source"]: row["count"] for row in resp.json()}
    assert sources["gateway"] == 2


def test_alerts_check_no_errors():
    client.post("/api/logs/ingest", json=make_log(level="INFO"))
    resp = client.get("/api/alerts/check")
    assert resp.status_code == 200
    assert resp.json()["triggered"] is False


def test_alerts_check_triggers_above_threshold():
    for _ in range(8):
        client.post("/api/logs/ingest", json=make_log(level="ERROR"))
    for _ in range(2):
        client.post("/api/logs/ingest", json=make_log(level="INFO"))

    resp = client.get("/api/alerts/check")
    body = resp.json()
    assert body["error_rate"] == 0.8
    assert body["triggered"] is True


def test_retention_requires_api_key_when_set(monkeypatch):
    import security
    monkeypatch.setattr(security, "API_KEY", "secret123")

    resp = client.post("/api/admin/retention", params={"days": 30})
    assert resp.status_code == 401

    resp = client.post("/api/admin/retention", params={"days": 30}, headers={"X-API-Key": "secret123"})
    assert resp.status_code == 200


def test_ingest_requires_api_key_when_set(monkeypatch):
    import security
    monkeypatch.setattr(security, "API_KEY", "secret123")

    resp = client.post("/api/logs/ingest", json=make_log())
    assert resp.status_code == 401

    resp = client.post("/api/logs/ingest", json=make_log(), headers={"X-API-Key": "secret123"})
    assert resp.status_code == 200


def test_rate_limit_triggers(monkeypatch):
    import security
    monkeypatch.setattr(security, "RATE_LIMIT_REQUESTS", 3)
    security._request_log.clear()

    statuses = []
    for _ in range(5):
        resp = client.post("/api/logs/ingest", json=make_log())
        statuses.append(resp.status_code)

    assert statuses[:3] == [200, 200, 200]
    assert 429 in statuses[3:]
