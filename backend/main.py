from fastapi import FastAPI, Query, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from typing import Optional
import os

import storage
from cache import cached, query_cache
from models import LogEntry, LogBatch
from security import require_api_key, require_read_access, rate_limit
from retention import purge_old_logs
from alerting import check_error_rate
from investigate import investigate

app = FastAPI(title="Telemetry Log Search & Visualization Platform")

# CORS is off unless origins are listed explicitly (the bundled dashboard is same-origin,
# so it doesn't need it). Example: CORS_ORIGINS=https://dash.example.com,http://localhost:3000
_origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]
if _origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-API-Key"],
    )

reads = [Depends(require_read_access)]
writes = [Depends(require_api_key), Depends(rate_limit)]


@app.on_event("startup")
def startup():
    storage.init_db()


@app.post("/api/logs/ingest", dependencies=writes)
def ingest_log(entry: LogEntry):
    log_id = storage.insert_log(entry)
    return {"status": "ok", "id": log_id}


@app.post("/api/logs/ingest/batch", dependencies=writes)
def ingest_batch(batch: LogBatch):
    if not batch.logs:
        raise HTTPException(status_code=400, detail="No logs provided")
    count = storage.insert_logs_batch(batch.logs)
    return {"status": "ok", "ingested": count}


@app.get("/api/logs/search", dependencies=reads)
def search(
    q: Optional[str] = Query(None, description="Full-text search query"),
    level: Optional[str] = Query(None, description="Filter by level: INFO/WARN/ERROR/DEBUG"),
    source: Optional[str] = Query(None, description="Filter by source/service"),
    start: Optional[str] = Query(None, description="ISO timestamp lower bound"),
    end: Optional[str] = Query(None, description="ISO timestamp upper bound"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
):
    total, results = storage.search_logs(
        query=q, level=level, source=source, start=start, end=end,
        page=page, page_size=page_size,
    )
    return {"total": total, "page": page, "page_size": page_size, "results": results}


# Dashboard aggregates are cached briefly (CACHE_TTL_SECONDS) so repeated dashboard
# refreshes don't each re-scan the store.
@cached()
def _volume(bucket, start, end):
    return storage.volume_over_time(bucket=bucket, start=start, end=end)


@cached()
def _levels():
    return storage.level_breakdown()


@cached()
def _sources(limit):
    return storage.top_sources(limit=limit)


@app.get("/api/stats/volume", dependencies=reads)
def stats_volume(
    bucket: str = Query("hour", description="minute | hour | day"),
    start: Optional[str] = None,
    end: Optional[str] = None,
):
    return _volume(bucket, start, end)


@app.get("/api/stats/levels", dependencies=reads)
def stats_levels():
    return _levels()


@app.get("/api/stats/sources", dependencies=reads)
def stats_sources(limit: int = Query(10, ge=1, le=100)):
    return _sources(limit)


@app.get("/api/alerts/check", dependencies=reads)
def alerts_check(window_minutes: Optional[int] = Query(None, ge=1)):
    return check_error_rate(window_minutes=window_minutes)


@app.get("/api/investigate", dependencies=reads)
def investigate_incident(
    q: str = Query("", max_length=500, description="Natural-language question, e.g. 'why is payments-api failing?'"),
    window_minutes: int = Query(60, ge=1, le=10080),
    sample_size: int = Query(500, ge=50, le=5000),
):
    """Gather grounded evidence (error patterns, affected sources, timeline) for a question."""
    return investigate(question=q, window_minutes=window_minutes, sample_size=sample_size)


@app.get("/api/cache/stats", dependencies=reads)
def cache_stats():
    return query_cache.stats()


@app.post("/api/admin/retention", dependencies=[Depends(require_api_key)])
def run_retention(days: int = Query(30, ge=1, description="Delete logs older than this many days")):
    deleted = purge_old_logs(days)
    query_cache.clear()
    return {"status": "ok", "deleted": deleted}


@app.get("/api/health")
def health():
    return {"status": "healthy"}


# Serve the frontend dashboard at / (skipped during tests, which import the app without a frontend dir)
_frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(_frontend_dir) and not os.environ.get("TESTING"):
    app.mount("/", StaticFiles(directory=_frontend_dir, html=True), name="frontend")
