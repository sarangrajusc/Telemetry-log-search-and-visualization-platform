"""
Tests for: secret redaction, hardened auth, TTL cache, incident investigation
(retrieval + concurrency + partial failure), the MCP server, and the Elasticsearch backend.
"""
import asyncio
import json
import os
import threading
import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import cache as cache_mod
import database
import es_backend
import investigate
import redaction
import security
import storage
from models import LogEntry


@pytest.fixture(autouse=True)
def fresh_db():
    database.init_db()
    with database.get_conn() as conn:
        conn.execute("DELETE FROM logs")
        conn.execute("DELETE FROM logs_fts")
    yield


@pytest.fixture
def client():
    import main
    return TestClient(main.app)


def add(level="INFO", source="svc", message="hello", minutes_ago=0, **kw):
    ts = datetime.utcnow() - timedelta(minutes=minutes_ago)
    e = LogEntry(source=source, level=level, message=message, timestamp=ts, **kw)
    database.insert_log(e)


# ---------------------------------------------------------------- redaction

@pytest.mark.parametrize("raw,kind", [
    ("login with AKIAIOSFODNN7EXAMPLE ok", "aws-access-key"),
    ("push using ghp_" + "a" * 36, "github-token"),
    ("slack xoxb-1234567890-abcdefghij", "slack-token"),
    ("stripe sk_live_" + "x" * 24, "stripe-key"),
    ("jwt eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0NTY3.SflKxwRJSMeKKF2QT4fw", "jwt"),
    ("Authorization: Bearer abcdef0123456789abcdef", "bearer-token"),
    ("connect postgres://admin:s3cr3tpw@db.internal:5432/app", "url-credentials"),
    ("-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----", "private-key"),
])
def test_redacts_known_secret_shapes(raw, kind):
    out = redaction.redact(raw)
    assert f"[REDACTED:{kind}]" in out


def test_redacts_key_value_secrets_but_keeps_context():
    out = redaction.redact("user=bob password=hunter2 api_key: abc123XYZ done")
    assert "hunter2" not in out and "abc123XYZ" not in out
    assert "user=bob" in out and "done" in out


def test_does_not_touch_normal_logs():
    msg = "Request completed in 120ms for user 42 on host-7"
    assert redaction.redact(msg) == msg


def test_tags_are_redacted_by_key_and_value():
    e = LogEntry(source="s", level="info", message="m",
                 tags={"api_key": "abc", "note": "token=zzz999", "region": "us-east", "nested": {"password": "p"}})
    assert e.tags["api_key"] == "[REDACTED:secret]"
    assert "zzz999" not in e.tags["note"]
    assert e.tags["region"] == "us-east"
    assert e.tags["nested"]["password"] == "[REDACTED:secret]"


def test_ingest_api_never_stores_secret(client):
    resp = client.post("/api/logs/ingest", json={
        "source": "billing", "level": "ERROR",
        "message": "charge failed, key=AKIAIOSFODNN7EXAMPLE password=hunter2"})
    assert resp.status_code == 200
    _, rows = database.search_logs(page_size=5)
    stored = rows[0]["message"]
    assert "AKIAIOSFODNN7EXAMPLE" not in stored and "hunter2" not in stored
    assert "charge failed" in stored


# ---------------------------------------------------------------- auth hardening

def test_reads_open_by_default_but_protected_when_enabled(client, monkeypatch):
    monkeypatch.setattr(security, "API_KEY", "k3y")
    assert client.get("/api/logs/search").status_code == 200  # PROTECT_READS off

    monkeypatch.setattr(security, "PROTECT_READS", True)
    assert client.get("/api/logs/search").status_code == 401
    assert client.get("/api/investigate").status_code == 401
    assert client.get("/api/logs/search", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/api/logs/search", headers={"X-API-Key": "k3y"}).status_code == 200
    assert client.get("/api/health").status_code == 200  # health stays open


def test_key_comparison_is_constant_time(monkeypatch):
    called = {}
    real = security.hmac.compare_digest

    def spy(a, b):
        called["yes"] = True
        return real(a, b)

    monkeypatch.setattr(security, "API_KEY", "secret")
    monkeypatch.setattr(security.hmac, "compare_digest", spy)
    assert security._key_ok("secret") is True
    assert security._key_ok("nope") is False
    assert security._key_ok(None) is False
    assert called["yes"]


def test_cors_disabled_without_explicit_origins(client):
    resp = client.get("/api/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in resp.headers


# ---------------------------------------------------------------- search robustness

def test_hyphenated_and_malformed_queries_do_not_500(client):
    add(source="auth-service", message="auth-service restarted")
    for q in ["auth-service", '"unbalanced', "a AND", "foo:bar"]:
        assert client.get("/api/logs/search", params={"q": q}).status_code == 200
    assert client.get("/api/logs/search", params={"q": "auth-service"}).json()["total"] == 1


def test_malformed_query_fallback_preserves_explicit_or(client):
    add(source="a", message="charge failed")
    add(source="b", message="login ok")
    add(source="c", message="unrelated")
    total = lambda q: client.get("/api/logs/search", params={"q": q}).json()["total"]
    assert total("auth-service OR charge") == 1      # OR kept: matches "charge" only
    assert total("charge login") == 0                # implicit AND: no single log has both
    assert database.fts_safe_query("a-b OR c") == '"a" OR "b" OR "c"'


# ---------------------------------------------------------------- cache

def test_ttl_expiry_and_lru_eviction():
    now = [0.0]
    c = cache_mod.TTLCache(ttl=10, max_entries=2, clock=lambda: now[0])
    c.set("a", 1)
    c.set("b", 2)
    assert c.get("a") == 1
    c.set("c", 3)                      # evicts least-recently-used ("b")
    assert c.get("b") is None and c.get("a") == 1 and c.get("c") == 3
    now[0] = 11
    assert c.get("a") is None          # expired


def test_cached_none_counts_as_hit_and_decorator_memoizes():
    c = cache_mod.TTLCache(ttl=60)
    calls = []

    @cache_mod.cached(c)
    def f(x, opts=None):
        calls.append(x)
        return None

    f(1, opts={"k": [1, 2]})
    f(1, opts={"k": [1, 2]})
    f(2)
    assert calls == [1, 2]
    assert c.stats()["hits"] == 1 and c.stats()["misses"] == 2


def test_cache_is_thread_safe_under_contention():
    c = cache_mod.TTLCache(ttl=60, max_entries=50)

    def worker(n):
        for i in range(500):
            c.set((n, i % 60), i)
            c.get((n, (i + 1) % 60))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert c.stats()["entries"] <= 50


# ---------------------------------------------------------------- investigation

def test_question_terms_drops_stopwords():
    assert investigate.question_terms("Why is the payments-api failing with timeout errors?") == \
        ["payments-api", "timeout"]


def test_normalize_template_collapses_variable_parts():
    a = investigate.normalize_template("Connection to 10.0.0.12 failed after 350ms (req 9f86d081884c7d65)")
    b = investigate.normalize_template("Connection to 192.168.1.9 failed after 12ms (req aaaaaaaaaaaaaaaa)")
    assert a == b == "Connection to <ip> failed after <n>ms (req <hex>)"


@pytest.mark.parametrize("tz", ["UTC", "America/Los_Angeles", "Asia/Tokyo"])
def test_window_does_not_depend_on_machine_timezone(tz):
    """Regression: .timestamp() on a naive UTC datetime shifts by the local UTC offset."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = tz
    time.tzset()
    try:
        start, end = investigate._window(60, now=datetime(2026, 1, 1, 12, 0, 7))
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()
    assert (start, end) == ("2026-01-01T11:00:00", "2026-01-01T12:00:00")


def seed_incident():
    for i in range(30):
        add("ERROR", "payments-api", f"Connection timeout to db-{i} after {100 + i}ms", minutes_ago=5)
    for i in range(5):
        add("ERROR", "auth-service", f"Invalid token for user {i}", minutes_ago=10)
    for i in range(65):
        add("INFO", "gateway", f"Request {i} ok", minutes_ago=3)
    add("INFO", "old", "ancient", minutes_ago=600)  # outside the window


def strip_volatile(ev):
    return {k: v for k, v in ev.items() if k != "elapsed_ms"}


def test_investigation_finds_dominant_pattern_and_source():
    seed_incident()
    ev = investigate.investigate("why is payments-api timeout failing?", window_minutes=60)
    assert ev["total_logs_in_window"] == 100          # the 600-min-old log is excluded
    assert ev["error_rate"] == 0.35
    top = ev["top_error_patterns"][0]
    assert top["pattern"] == "Connection timeout to db-<n> after <n>ms" and top["count"] == 30
    assert next(iter(ev["affected_sources"])) == "payments-api"
    assert ev["keyword_matches"]["total"] >= 30
    assert "[log:" in ev["context"] and ev["partial_failures"] == {}


def test_parallel_and_sequential_results_are_identical():
    seed_incident()
    now = datetime.utcnow()
    par = investigate.investigate("timeout", 60, parallel=True, now=now, use_cache=False)
    seq = investigate.investigate("timeout", 60, parallel=False, now=now, use_cache=False)
    assert strip_volatile(par) == strip_volatile(seq)


def test_repeat_investigations_are_served_from_cache():
    seed_incident()
    now = datetime.utcnow()
    investigate.investigate("timeout", 60, now=now)
    before = cache_mod.query_cache.stats()
    investigate.investigate("timeout", 60, now=now)
    after = cache_mod.query_cache.stats()
    assert after["hits"] - before["hits"] == 5       # all five sub-queries hit
    assert after["misses"] == before["misses"]


def test_one_failing_subquery_degrades_instead_of_failing(monkeypatch):
    seed_incident()
    real = storage.search_logs

    def flaky(**kw):
        if kw.get("level") == "WARN":
            raise RuntimeError("backend exploded")
        return real(**kw)

    monkeypatch.setattr(storage, "search_logs", flaky)
    ev = investigate.investigate("timeout", 60, use_cache=False)
    assert "warnings" in ev["partial_failures"] and "backend exploded" in ev["partial_failures"]["warnings"]
    assert ev["top_error_patterns"][0]["count"] == 30   # the rest of the report is intact
    assert "Evidence gaps" in ev["context"]


def test_slow_subquery_times_out_without_blocking_the_report(monkeypatch):
    seed_incident()
    real = storage.search_logs

    def slow(**kw):
        if kw.get("level") == "WARN":
            time.sleep(0.6)
        return real(**kw)

    monkeypatch.setattr(storage, "search_logs", slow)
    monkeypatch.setattr(investigate, "TASK_TIMEOUT_SECONDS", 0.15)
    t0 = time.perf_counter()
    ev = investigate.investigate("timeout", 60, use_cache=False)
    assert time.perf_counter() - t0 < 0.5
    assert "timed out" in ev["partial_failures"]["warnings"]


def test_investigate_endpoint_and_cache_stats(client):
    seed_incident()
    r = client.get("/api/investigate", params={"q": "payments timeout", "window_minutes": 60})
    assert r.status_code == 200
    assert r.json()["top_error_patterns"][0]["count"] == 30
    client.get("/api/investigate", params={"q": "payments timeout", "window_minutes": 60})
    assert client.get("/api/cache/stats").json()["hits"] >= 1
    assert client.get("/api/investigate", params={"window_minutes": 0}).status_code == 422


def test_dashboard_stats_are_cached(client):
    add("ERROR")
    assert {r["level"]: r["count"] for r in client.get("/api/stats/levels").json()} == {"ERROR": 1}
    add("ERROR")  # within TTL, served stale by design
    assert {r["level"]: r["count"] for r in client.get("/api/stats/levels").json()} == {"ERROR": 1}
    cache_mod.query_cache.clear()
    assert {r["level"]: r["count"] for r in client.get("/api/stats/levels").json()} == {"ERROR": 2}


# ---------------------------------------------------------------- MCP server

def call(tool, args):
    from mcp.client import Client
    import mcp_server

    async def run():
        async with Client(mcp_server.mcp) as c:
            return await c.call_tool(tool, args)

    result = asyncio.run(run())
    return json.loads(result.content[0].text)


def test_mcp_exposes_only_read_only_tools():
    from mcp.client import Client
    import mcp_server

    async def run():
        async with Client(mcp_server.mcp) as c:
            return [t.name for t in (await c.list_tools()).tools]

    names = asyncio.run(run())
    assert set(names) == {"investigate_incident", "search_logs", "get_error_patterns", "check_error_rate"}
    assert not any(w in n for n in names for w in ("ingest", "delete", "purge", "retention"))


def test_mcp_investigate_returns_cited_context():
    seed_incident()
    out = call("investigate_incident", {"question": "why is payments-api timeout failing?"})
    assert "Connection timeout to db-<n> after <n>ms" in out["context"]
    assert out["evidence"]["error_rate"] == 0.35


def test_mcp_search_and_patterns_and_clamping():
    seed_incident()
    out = call("search_logs", {"query": "payments-api", "level": "ERROR", "limit": 5000})
    assert out["total"] == 30 and out["returned"] == 30       # limit clamped to 100, only 30 exist
    pats = call("get_error_patterns", {"window_minutes": 60})
    assert pats["patterns"][0]["count"] == 30
    assert call("check_error_rate", {"window_minutes": 60})["triggered"] is True


# ---------------------------------------------------------------- storage selection + Elasticsearch

def test_storage_backend_selection(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "elasticsearch")
    storage.set_backend(None)
    assert storage.get_backend() is es_backend
    monkeypatch.setenv("STORAGE_BACKEND", "nonsense")
    storage.set_backend(None)
    with pytest.raises(ValueError):
        storage.get_backend()


class FakeIndices:
    def __init__(self):
        self.created = None
        self.present = False

    def exists(self, index):
        return self.present

    def create(self, index, **body):
        self.created = (index, body)


class FakeES:
    def __init__(self, response=None):
        self.indices = FakeIndices()
        self.response = response
        self.calls = []

    def search(self, index, **body):
        self.calls.append((index, body))
        return self.response

    def delete_by_query(self, index, query, refresh):
        self.calls.append(("dbq", query))
        return {"deleted": 7}


@pytest.fixture
def es():
    fake = FakeES()
    es_backend.set_client(fake)
    yield fake
    es_backend.set_client(None)


def test_es_creates_index_with_mapping_once(es):
    es_backend.init_db()
    index, body = es.indices.created
    assert index == es_backend.ES_INDEX and body["mappings"]["properties"]["level"]["type"] == "keyword"
    es.indices.created = None
    es.indices.present = True
    es_backend.init_db()
    assert es.indices.created is None


def test_es_search_builds_filtered_query_and_parses_hits(es):
    es.response = {"hits": {"total": {"value": 2}, "hits": [
        {"_id": "a1", "_source": {"timestamp": "2026-01-01T00:00:00", "source": "s", "host": None,
                                  "level": "ERROR", "message": "boom", "tags": {}}}]}}
    total, rows = es_backend.search_logs(query="timeout", level="error", source="pay", start="2026-01-01",
                                          page=3, page_size=10)
    _, body = es.calls[0]
    assert body["from"] == 20 and body["size"] == 10 and body["track_total_hits"] is True
    assert body["query"]["bool"]["must"][0]["match"]["message"]["query"] == "timeout"
    assert {"term": {"level": "ERROR"}} in body["query"]["bool"]["filter"]
    assert {"term": {"source": "pay"}} in body["query"]["bool"]["filter"]
    assert total == 2 and rows[0]["id"] == "a1" and rows[0]["message"] == "boom"


def test_es_aggregations_parse(es):
    es.response = {"aggregations": {"v": {"buckets": [{"key_as_string": "2026-01-01T00:00", "doc_count": 4}]}}}
    assert es_backend.volume_over_time("minute") == [{"bucket": "2026-01-01T00:00", "count": 4}]
    assert es.calls[0][1]["aggs"]["v"]["date_histogram"]["calendar_interval"] == "minute"

    es.response = {"aggregations": {"t": {"buckets": [{"key": "ERROR", "doc_count": 3}]}}}
    assert es_backend.level_breakdown() == [{"level": "ERROR", "count": 3}]
    assert es_backend.recent_level_counts(15) == {"ERROR": 3}
    assert es_backend.top_sources(5) == [{"source": "ERROR", "count": 3}]


def test_es_bulk_and_purge(es, monkeypatch):
    import elasticsearch.helpers as helpers
    seen = {}

    def fake_bulk(client, actions, refresh):
        seen["docs"] = [a["_source"] for a in actions]
        return len(seen["docs"]), []

    monkeypatch.setattr(helpers, "bulk", fake_bulk)
    n = es_backend.insert_logs_batch([LogEntry(source="s", level="warn", message="password=hunter2 x")])
    assert n == 1 and seen["docs"][0]["level"] == "WARN"
    assert "hunter2" not in seen["docs"][0]["message"]       # redaction applies before any backend
    assert es_backend.purge_older_than("2026-01-01") == 7


def test_investigation_runs_on_elasticsearch_backend(es, monkeypatch):
    monkeypatch.setattr(storage, "_backend", es_backend)

    def respond(index, **body):
        es.calls.append(body)
        if "aggs" in body and "t" in body["aggs"]:
            return {"aggregations": {"t": {"buckets": [{"key": "ERROR", "doc_count": 2}]}}}
        if "aggs" in body:
            return {"aggregations": {"v": {"buckets": []}}}
        return {"hits": {"total": {"value": 2}, "hits": [
            {"_id": f"e{i}", "_source": {"timestamp": f"2026-01-01T00:00:0{i}", "source": "pay",
                                         "level": "ERROR", "message": f"fail {i}", "tags": {}}}
            for i in range(2)]}}

    es.search = respond
    ev = investigate.investigate("pay failures", 60, use_cache=False)
    assert ev["partial_failures"] == {} and ev["top_error_patterns"][0]["pattern"] == "fail <n>"
