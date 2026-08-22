import sqlite3
import json
import os
from datetime import datetime
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(__file__), "telemetry.db")


def init_db():
    """Create the logs table and FTS5 virtual table if they don't exist."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            source TEXT NOT NULL,
            host TEXT,
            level TEXT NOT NULL,
            message TEXT NOT NULL,
            tags TEXT
        )
    """)

    # FTS5 virtual table indexed over the message + source, linked via rowid
    cur.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS logs_fts USING fts5(
            message, source, content='logs', content_rowid='id'
        )
    """)

    # Keep FTS index in sync with the logs table
    cur.execute("""
        CREATE TRIGGER IF NOT EXISTS logs_ai AFTER INSERT ON logs BEGIN
            INSERT INTO logs_fts(rowid, message, source) VALUES (new.id, new.message, new.source);
        END
    """)
    cur.execute("""
        CREATE TRIGGER IF NOT EXISTS logs_ad AFTER DELETE ON logs BEGIN
            INSERT INTO logs_fts(logs_fts, rowid, message, source) VALUES('delete', old.id, old.message, old.source);
        END
    """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON logs(timestamp)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_logs_level ON logs(level)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_logs_source ON logs(source)")

    conn.commit()
    conn.close()


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def insert_log(entry) -> int:
    ts = entry.timestamp or datetime.utcnow()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO logs (timestamp, source, host, level, message, tags) VALUES (?, ?, ?, ?, ?, ?)",
            (ts.isoformat(), entry.source, entry.host, entry.level.upper(), entry.message,
             json.dumps(entry.tags or {})),
        )
        return cur.lastrowid


def insert_logs_batch(entries) -> int:
    with get_conn() as conn:
        rows = [
            (
                (e.timestamp or datetime.utcnow()).isoformat(),
                e.source, e.host, e.level.upper(), e.message, json.dumps(e.tags or {}),
            )
            for e in entries
        ]
        conn.executemany(
            "INSERT INTO logs (timestamp, source, host, level, message, tags) VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        return len(rows)


def search_logs(query: str = None, level: str = None, source: str = None,
                 start: str = None, end: str = None, page: int = 1, page_size: int = 50):
    offset = (page - 1) * page_size
    where = []
    params = []

    base = "FROM logs l"
    if query:
        base = "FROM logs_fts f JOIN logs l ON f.rowid = l.id"
        where.append("logs_fts MATCH ?")
        params.append(query)

    if level:
        where.append("l.level = ?")
        params.append(level.upper())
    if source:
        where.append("l.source = ?")
        params.append(source)
    if start:
        where.append("l.timestamp >= ?")
        params.append(start)
    if end:
        where.append("l.timestamp <= ?")
        params.append(end)

    where_clause = f"WHERE {' AND '.join(where)}" if where else ""

    with get_conn() as conn:
        total = conn.execute(f"SELECT COUNT(*) {base} {where_clause}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT l.id, l.timestamp, l.source, l.host, l.level, l.message, l.tags "
            f"{base} {where_clause} ORDER BY l.timestamp DESC LIMIT ? OFFSET ?",
            params + [page_size, offset],
        ).fetchall()

    results = []
    for r in rows:
        results.append({
            "id": r["id"], "timestamp": r["timestamp"], "source": r["source"],
            "host": r["host"], "level": r["level"], "message": r["message"],
            "tags": json.loads(r["tags"]) if r["tags"] else {},
        })
    return total, results


def volume_over_time(bucket: str = "hour", start: str = None, end: str = None):
    """Bucket log counts by minute/hour/day for charting."""
    fmt = {"minute": "%Y-%m-%dT%H:%M", "hour": "%Y-%m-%dT%H", "day": "%Y-%m-%d"}.get(bucket, "%Y-%m-%dT%H")
    where = []
    params = []
    if start:
        where.append("timestamp >= ?")
        params.append(start)
    if end:
        where.append("timestamp <= ?")
        params.append(end)
    where_clause = f"WHERE {' AND '.join(where)}" if where else ""

    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT strftime('{fmt}', timestamp) as bucket, COUNT(*) as count "
            f"FROM logs {where_clause} GROUP BY bucket ORDER BY bucket",
            params,
        ).fetchall()
    return [{"bucket": r["bucket"], "count": r["count"]} for r in rows]


def level_breakdown():
    with get_conn() as conn:
        rows = conn.execute("SELECT level, COUNT(*) as count FROM logs GROUP BY level").fetchall()
    return [{"level": r["level"], "count": r["count"]} for r in rows]


def top_sources(limit: int = 10):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT source, COUNT(*) as count FROM logs GROUP BY source ORDER BY count DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [{"source": r["source"], "count": r["count"]} for r in rows]
