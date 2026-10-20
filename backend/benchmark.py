"""
Benchmarks ingestion, search, dashboard aggregates and incident investigation against
the current storage backend at a configurable volume.

It measures, rather than assumes:
  * ingestion throughput (includes request validation and secret redaction)
  * full-text / filtered search latency
  * dashboard aggregate latency
  * incident investigation: sequential vs. concurrent fan-out vs. warm cache

Usage:
    python benchmark.py --rows 500000
"""
import argparse
import os
import random
import statistics
import tempfile
import time
from datetime import datetime, timedelta

os.environ["TESTING"] = "1"


def median_ms(fn, runs):
    times = []
    for _ in range(runs):
        t0 = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t0) * 1000)
    return statistics.median(times)


def run_benchmark(rows: int, seed: int = 7):
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.environ["DB_PATH"] = db_path

    import cache
    import database
    import investigate
    import storage

    database.DB_PATH = db_path
    storage.set_backend(None)
    storage.init_db()

    from models import LogEntry

    rnd = random.Random(seed)
    sources = ["auth-service", "payments-api", "order-worker", "search-index", "gateway", "notifications"]
    levels = ["INFO"] * 8 + ["WARN"] * 3 + ["ERROR"] * 2 + ["DEBUG"]
    templates = [
        "Request {n} completed in {m}ms",
        "Connection timeout to db-{m} after {n}ms",
        "Retrying failed request attempt {m} for order {n}",
        "Cache miss for key {n}",
        "User {n} authentication succeeded",
        "Invalid token for user {n}",
        "Worker picked up job {n}",
        "Rate limit exceeded for client 10.0.{m}.{k}",
    ]
    now = datetime.utcnow()

    print(f"Benchmarking with {rows:,} rows (backend: {storage.get_backend().__name__})")

    # --- Ingestion (validation + redaction + insert), in batches of 1000 ---
    t0 = time.perf_counter()
    batch, done = [], 0
    for _ in range(rows):
        msg = rnd.choice(templates).format(n=rnd.randint(1, 99999), m=rnd.randint(1, 250), k=rnd.randint(1, 250))
        ts = now - timedelta(seconds=rnd.randint(0, 24 * 3600))
        batch.append(LogEntry(source=rnd.choice(sources), level=rnd.choice(levels), message=msg, timestamp=ts))
        if len(batch) == 1000:
            storage.insert_logs_batch(batch)
            done += len(batch)
            batch = []
    if batch:
        storage.insert_logs_batch(batch)
        done += len(batch)
    ingest_time = time.perf_counter() - t0
    print(f"Ingestion (validate+redact+insert): {done:,} rows in {ingest_time:.1f}s "
          f"({done / ingest_time:,.0f} rows/sec)")

    words = ["timeout", "retrying", "completed", "token", "cache", "worker", "authentication"]
    print(f"Full-text search (top 50):      {median_ms(lambda: storage.search_logs(query=rnd.choice(words), page_size=50), 30):8.1f} ms median")
    print(f"Filtered search (level+source): {median_ms(lambda: storage.search_logs(level='ERROR', source=rnd.choice(sources), page_size=50), 30):8.1f} ms median")
    print(f"Dashboard stats (3 aggregates): {median_ms(lambda: (storage.volume_over_time(bucket='hour'), storage.level_breakdown(), storage.top_sources()), 5):8.1f} ms median")

    # --- Incident investigation: 24h window over the whole table ---
    window = 24 * 60
    q = "why is payments-api timeout failing?"
    fixed_now = datetime.utcnow()

    seq = median_ms(lambda: investigate.investigate(q, window, parallel=False, use_cache=False, now=fixed_now), 5)
    par = median_ms(lambda: investigate.investigate(q, window, parallel=True, use_cache=False, now=fixed_now), 5)
    investigate.investigate(q, window, now=fixed_now)  # warm the cache
    warm = median_ms(lambda: investigate.investigate(q, window, now=fixed_now), 20)

    print("\nIncident investigation (24h window, 5 sub-queries):")
    print(f"  sequential, cold cache : {seq:8.1f} ms")
    print(f"  concurrent, cold cache : {par:8.1f} ms  ({seq / par:.2f}x vs sequential)")
    print(f"  concurrent, warm cache : {warm:8.2f} ms  ({seq / warm:,.0f}x vs sequential)")
    print(f"  cache stats            : {cache.query_cache.stats()}")

    os.close(db_fd)
    os.remove(db_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=50000)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    run_benchmark(args.rows, args.seed)
