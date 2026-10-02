from fastapi import FastAPI, Query, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from typing import Optional
import os

import database
from models import LogEntry, LogBatch
from security import require_api_key, rate_limit
from retention import purge_old_logs
from alerting import check_error_rate

app = FastAPI(title="Telemetry Log Search & Visualization Platform")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup():
    database.init_db()


@app.post("/api/logs/ingest", dependencies=[Depends(require_api_key), Depends(rate_limit)])
def ingest_log(entry: LogEntry):
    log_id = database.insert_log(entry)
    return {"status": "ok", "id": log_id}


@app.post("/api/logs/ingest/batch", dependencies=[Depends(require_api_key), Depends(rate_limit)])
def ingest_batch(batch: LogBatch):
    if not batch.logs:
        raise HTTPException(status_code=400, detail="No logs provided")
    count = database.insert_logs_batch(batch.logs)
    return {"status": "ok", "ingested": count}


@app.get("/api/logs/search")
def search(
    q: Optional[str] = Query(None, description="Full-text search query"),
    level: Optional[str] = Query(None, description="Filter by level: INFO/WARN/ERROR/DEBUG"),
    source: Optional[str] = Query(None, description="Filter by source/service"),
    start: Optional[str] = Query(None, description="ISO timestamp lower bound"),
    end: Optional[str] = Query(None, description="ISO timestamp upper bound"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
):
    total, results = database.search_logs(
        query=q, level=level, source=source, start=start, end=end,
        page=page, page_size=page_size,
    )
    return {"total": total, "page": page, "page_size": page_size, "results": results}


@app.get("/api/stats/volume")
def stats_volume(
    bucket: str = Query("hour", description="minute | hour | day"),
    start: Optional[str] = None,
    end: Optional[str] = None,
):
    return database.volume_over_time(bucket=bucket, start=start, end=end)


@app.get("/api/stats/levels")
def stats_levels():
    return database.level_breakdown()


@app.get("/api/stats/sources")
def stats_sources(limit: int = 10):
    return database.top_sources(limit=limit)


@app.get("/api/alerts/check")
def alerts_check(window_minutes: Optional[int] = Query(None, ge=1)):
    return check_error_rate(window_minutes=window_minutes)


@app.post("/api/admin/retention", dependencies=[Depends(require_api_key)])
def run_retention(days: int = Query(30, ge=1, description="Delete logs older than this many days")):
    deleted = purge_old_logs(days)
    return {"status": "ok", "deleted": deleted}


@app.get("/api/health")
def health():
    return {"status": "healthy"}


# Serve the frontend dashboard at / (skipped during tests, which import the app without a frontend dir)
_frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(_frontend_dir) and not os.environ.get("TESTING"):
    app.mount("/", StaticFiles(directory=_frontend_dir, html=True), name="frontend")
