from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from typing import Optional

import database
from models import LogEntry, LogBatch

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


@app.post("/api/logs/ingest")
def ingest_log(entry: LogEntry):
    log_id = database.insert_log(entry)
    return {"status": "ok", "id": log_id}


@app.post("/api/logs/ingest/batch")
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


@app.get("/api/health")
def health():
    return {"status": "healthy"}


# Serve the frontend dashboard at /
app.mount("/", StaticFiles(directory="../frontend", html=True), name="frontend")
