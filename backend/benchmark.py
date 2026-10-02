"""
Benchmarks ingestion and search performance against the current SQLite + FTS5
storage layer, at a configurable volume. Gives concrete numbers to back the
"when would this need a dedicated search engine" discussion in the README,
instead of leaving it as a theoretical tradeoff.

Usage:
    python benchmark.py --rows 100000
"""
import argparse
import random
import time
import os
import tempfile

os.environ["TESTING"] = "1"


def run_benchmark(rows: int):
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.environ["DB_PATH"] = db_path

    import database
    database.DB_PATH = db_path
    database.init_db()

    from models import LogEntry

    sources = ["auth-service", "payments-api", "order-worker", "search-index", "gateway"]
    levels = ["INFO", "INFO", "WARN", "ERROR", "DEBUG"]
    words = ["timeout", "retrying", "completed", "failed", "cache", "query", "connection", "token"]

    print(f"Benchmarking with {rows:,} rows...")

    # --- Ingestion ---
    entries = []
    for i in range(rows):
        msg = f"{random.choice(words)} event {i} {random.choice(words)}"
        entries.append(LogEntry(source=random.choice(sources), level=random.choice(levels), message=msg))

    start = time.perf_counter()
    batch_size = 1000
    for i in range(0, len(entries), batch_size):
        database.insert_logs_batch(entries[i:i + batch_size])
    ingest_time = time.perf_counter() - start
    print(f"Ingestion: {rows:,} rows in {ingest_time:.2f}s ({rows / ingest_time:,.0f} rows/sec)")

    # --- Full-text search ---
    start = time.perf_counter()
    for _ in range(50):
        database.search_logs(query=random.choice(words), page_size=50)
    fts_time = (time.perf_counter() - start) / 50
    print(f"Full-text search: {fts_time * 1000:.2f}ms avg over 50 queries")

    # --- Filtered search (level + source, no text query) ---
    start = time.perf_counter()
    for _ in range(50):
        database.search_logs(level=random.choice(levels), source=random.choice(sources), page_size=50)
    filter_time = (time.perf_counter() - start) / 50
    print(f"Filtered search (level+source): {filter_time * 1000:.2f}ms avg over 50 queries")

    # --- Aggregation (stats) ---
    start = time.perf_counter()
    for _ in range(20):
        database.volume_over_time(bucket="hour")
        database.level_breakdown()
        database.top_sources()
    agg_time = (time.perf_counter() - start) / 20
    print(f"Dashboard stats (3 aggregate queries): {agg_time * 1000:.2f}ms avg over 20 runs")

    os.close(db_fd)
    os.remove(db_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=50000)
    args = parser.parse_args()
    run_benchmark(args.rows)
