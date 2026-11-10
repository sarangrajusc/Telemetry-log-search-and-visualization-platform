"""Tests for the shared ingest client, the file tailer (rotation, outages, poison lines), the simulator."""
import os

import pytest
import requests
from fastapi.testclient import TestClient

import database
import ingest_client
import ingest_simulator
import security
from file_tailer import FileTailer, parse_line
from ingest_client import AuthError, IngestClient, RejectedError, TransientError
from models import LogEntry


@pytest.fixture(autouse=True)
def fresh_db():
    database.init_db()
    with database.get_conn() as conn:
        conn.execute("DELETE FROM logs")
        conn.execute("DELETE FROM logs_fts")
    yield


# ------------------------------------------------------------------ IngestClient

class Resp:
    def __init__(self, code, text="", headers=None):
        self.status_code, self.text, self.headers = code, text, headers or {}


class ScriptedSession:
    """Returns/raises scripted outcomes in order and records every call."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def make_client(*outcomes, **kw):
    sleeps = []
    session = ScriptedSession(*outcomes)
    client = IngestClient("http://api/", api_key=kw.pop("api_key", "k"), session=session,
                          sleep=sleeps.append, jitter=lambda: 1.0, **kw)
    return client, session, sleeps


def test_sends_api_key_header_and_correct_url():
    client, session, _ = make_client(Resp(200))
    assert client.post_batch([{"a": 1}]) == 1
    assert session.calls[0]["url"] == "http://api/api/logs/ingest/batch"
    assert session.calls[0]["headers"]["X-API-Key"] == "k"


def test_api_key_falls_back_to_environment(monkeypatch):
    monkeypatch.setenv("API_KEY", "from-env")
    client = IngestClient("http://api", session=ScriptedSession(Resp(200)))
    assert client._headers()["X-API-Key"] == "from-env"
    monkeypatch.delenv("API_KEY")
    assert "X-API-Key" not in IngestClient("http://api")._headers()


def test_retries_transient_errors_with_exponential_backoff():
    client, session, sleeps = make_client(
        Resp(503), requests.ConnectionError("down"), Resp(429), Resp(200), backoff_base=1.0)
    assert client.post_batch([{}]) == 1
    assert len(session.calls) == 4
    assert sleeps == [1.0, 2.0, 4.0]


def test_retry_after_header_is_honoured_and_capped():
    client, _, sleeps = make_client(
        Resp(429, headers={"Retry-After": "7"}), Resp(429, headers={"Retry-After": "999"}), Resp(200),
        backoff_max=30.0)
    client.post_batch([{}])
    assert sleeps == [7.0, 30.0]


def test_gives_up_after_max_retries():
    client, session, _ = make_client(*[Resp(500)] * 3, max_retries=2)
    with pytest.raises(TransientError, match="3 attempts"):
        client.post_batch([{}])
    assert len(session.calls) == 3


def test_auth_failure_is_immediate_and_not_retried():
    client, session, sleeps = make_client(Resp(401))
    with pytest.raises(AuthError):
        client.post_batch([{}])
    assert len(session.calls) == 1 and sleeps == []


def test_rejected_payload_is_not_retried():
    client, session, _ = make_client(Resp(422, "bad level"))
    with pytest.raises(RejectedError, match="bad level"):
        client.post_batch([{}])
    assert len(session.calls) == 1


# ------------------------------------------------------------------ parse_line

def test_parse_line_structured_and_fallback():
    assert parse_line("2026-01-01T00:00:00 WARNING gateway slow response\n") == {
        "timestamp": "2026-01-01T00:00:00", "level": "WARN", "source": "gateway", "message": "slow response"}
    assert parse_line("just some text\n") == {"level": "INFO", "source": "unknown", "message": "just some text"}
    assert parse_line("   \n") is None


# ------------------------------------------------------------------ FileTailer

class FakeClient:
    def __init__(self):
        self.batches = []
        self.fail_next = 0
        self.reject_if = None

    def post_batch(self, logs):
        if self.fail_next:
            self.fail_next -= 1
            raise TransientError("down")
        if self.reject_if and any(self.reject_if(x) for x in logs):
            raise RejectedError("HTTP 422")
        self.batches.append(list(logs))
        return len(logs)

    @property
    def messages(self):
        return [x["message"] for b in self.batches for x in b]


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def write(path, text, mode="a"):
    with open(path, mode) as f:
        f.write(text)


def line(i):
    return f"2026-01-01T00:00:{i:02d} INFO svc message-{i}\n"


@pytest.fixture
def logfile(tmp_path):
    p = tmp_path / "app.log"
    p.write_text(line(0))        # pre-existing content
    return str(p)


def make_tailer(path, client=None, **kw):
    client = client or FakeClient()
    kw.setdefault("clock", Clock())
    kw.setdefault("log", lambda *_: None)
    return FileTailer(path, client, **kw), client


def test_starts_at_end_by_default_and_ships_only_new_lines(logfile):
    t, c = make_tailer(logfile, batch_size=2)
    t.poll()
    write(logfile, line(1) + line(2))
    assert t.poll() == 2
    t.flush(force=True)
    assert c.messages == ["message-1", "message-2"]


def test_from_start_ships_existing_content(logfile):
    t, c = make_tailer(logfile, from_start=True)
    t.poll()
    t.flush(force=True)
    assert c.messages == ["message-0"]


def test_half_written_line_waits_for_newline(logfile):
    t, c = make_tailer(logfile)
    t.poll()
    write(logfile, "2026-01-01T00:00:05 INFO svc half")
    assert t.poll() == 0
    write(logfile, "-done\n")
    assert t.poll() == 1
    t.flush(force=True)
    assert c.messages == ["half-done"]


def test_rotation_drains_old_file_then_follows_new_one(logfile):
    t, c = make_tailer(logfile)
    t.poll()
    write(logfile, line(1))                       # written just before rotation
    os.rename(logfile, logfile + ".1")
    write(logfile, line(2) + line(3), mode="w")   # brand-new file, new inode
    t.poll()
    t.poll()
    t.flush(force=True)
    assert c.messages == ["message-1", "message-2", "message-3"]


def test_truncation_restarts_from_beginning(logfile):
    t, c = make_tailer(logfile)
    t.poll()
    write(logfile, line(1) + line(2))
    t.poll()
    write(logfile, line(9), mode="w")             # truncated in place, shorter than before
    t.poll()
    t.flush(force=True)
    assert c.messages == ["message-1", "message-2", "message-9"]


def test_file_that_appears_after_startup_is_read_from_the_start(tmp_path):
    path = str(tmp_path / "later.log")
    t, c = make_tailer(path)
    assert t.poll() == 0
    write(path, line(1) + line(2))
    t.poll()
    t.flush(force=True)
    assert c.messages == ["message-1", "message-2"]


def test_outage_keeps_lines_in_order_and_retries_after_cooldown(logfile):
    clock = Clock()
    t, c = make_tailer(logfile, batch_size=2, retry_cooldown=5.0, clock=clock)
    t.poll()
    c.fail_next = 1
    write(logfile, line(1) + line(2) + line(3))
    t.poll()
    t.flush(force=True)                           # fails: nothing lost
    assert c.batches == [] and len(t.buffer) == 3

    t.flush(force=True)                           # still inside cooldown: no attempt
    assert c.batches == []

    clock.t = 6.0
    t.flush(force=True)
    assert c.messages == ["message-1", "message-2", "message-3"]
    assert t.shipped == 3 and not t.buffer


def test_buffer_overflow_drops_oldest_and_counts(logfile):
    t, c = make_tailer(logfile, max_buffer=3)
    t.poll()
    write(logfile, "".join(line(i) for i in range(1, 7)))
    t.poll()
    assert t.dropped_overflow == 3
    t.flush(force=True)
    assert c.messages == ["message-4", "message-5", "message-6"]


def test_poison_line_is_isolated_and_the_rest_ships(logfile):
    t, c = make_tailer(logfile, batch_size=8)
    c.reject_if = lambda x: x["message"] == "message-3"
    t.poll()
    write(logfile, "".join(line(i) for i in range(1, 7)))
    t.poll()
    t.flush(force=True)
    assert sorted(c.messages) == ["message-1", "message-2", "message-4", "message-5", "message-6"]
    assert t.dropped_rejected == 1 and not t.buffer


def test_auth_error_stops_the_tailer(logfile):
    class Denied:
        def post_batch(self, logs):
            raise AuthError("HTTP 401")

    t, _ = make_tailer(logfile, client=Denied(), batch_size=1)
    t.poll()
    write(logfile, line(1))
    with pytest.raises(AuthError):
        t.run(should_stop=lambda: False, sleep=lambda s: None)


def test_run_flushes_remaining_lines_on_exit(logfile):
    t, c = make_tailer(logfile, batch_size=100, flush_interval=999)
    t.poll()                                      # open at end-of-file first, as run() would at startup
    ticks = iter([False, False, True])
    write_once = {"done": False}

    def should_stop():
        stop = next(ticks)
        if not write_once["done"]:
            write(logfile, line(1))
            write_once["done"] = True
        return stop

    t.run(should_stop=should_stop, sleep=lambda s: None)
    assert c.messages == ["message-1"]            # below batch_size, shipped by the final flush


# ------------------------------------------------------------------ end to end through the real API

class AppSession:
    """Lets IngestClient talk to the FastAPI app in-process."""

    def __init__(self, tc):
        self.tc = tc

    def post(self, url, json=None, headers=None, timeout=None):
        return self.tc.post("/" + url.split("://", 1)[1].split("/", 1)[1], json=json, headers=headers)


@pytest.fixture
def api(monkeypatch):
    import main
    monkeypatch.setattr(security, "API_KEY", "s3cret")
    return AppSession(TestClient(main.app))


def test_tailer_to_api_end_to_end_with_auth(logfile, api):
    client = IngestClient("http://t", api_key="s3cret", session=api, max_retries=0)
    t, _ = make_tailer(logfile, client=client, batch_size=10)
    t.poll()
    write(logfile, "2026-01-01T00:00:01Z ERROR payments charge failed password=hunter2\n" + line(2))
    t.poll()
    t.flush(force=True)

    total, rows = database.search_logs(page_size=10)
    assert total == 2
    msgs = {r["message"] for r in rows}
    assert "message-2" in msgs
    assert not any("hunter2" in m for m in msgs)                       # redacted on the way in
    err = next(r for r in rows if r["level"] == "ERROR")
    assert err["timestamp"] == "2026-01-01T00:00:01"                   # 'Z' normalised to naive UTC


def test_wrong_api_key_is_a_clear_auth_error(api):
    client = IngestClient("http://t", api_key="nope", session=api, max_retries=0)
    with pytest.raises(AuthError):
        client.post_batch([{"source": "s", "level": "INFO", "message": "x"}])


# ------------------------------------------------------------------ misc

def test_simulator_generates_valid_entries():
    from datetime import datetime
    for i in range(50):
        entry = ingest_simulator.generate_log(datetime(2026, 1, 1), i * 60)
        LogEntry(**entry)                                              # would raise if invalid


def test_model_normalises_aware_timestamps_to_naive_utc():
    e = LogEntry(source="s", level="info", message="m", timestamp="2026-01-01T05:00:00+05:00")
    assert e.timestamp.isoformat() == "2026-01-01T00:00:00" and e.timestamp.tzinfo is None


def test_init_db_creates_missing_directory_and_explains_unwritable_one(tmp_path, monkeypatch):
    target = tmp_path / "nested" / "dir" / "t.db"
    monkeypatch.setattr(database, "DB_PATH", str(target))
    database.init_db()
    assert target.exists()

    monkeypatch.setattr(database.os, "access", lambda *a, **k: False)
    with pytest.raises(RuntimeError, match="not writable"):
        database.init_db()
