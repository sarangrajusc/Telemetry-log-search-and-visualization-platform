# Telemetry Log Search & Visualization Platform

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

A full-stack platform for ingesting telemetry/log events, searching them full-text with
structured filters, and visualizing volume, error-rate, and source breakdowns on a live
dashboard. Built as a portfolio project to demonstrate backend API design, a pluggable
search/storage layer, and a lightweight data-viz frontend.

## Features

- **Ingestion API** — single or batched log ingestion via REST, with request validation
- **Full-text search** — SQLite FTS5-backed search over log messages, with filters for
  level, source, and time range
- **Live dashboard** — log volume over time, level breakdown, and top sources, all
  driven by the same search/stats API
- **Real ingestion path** — `file_tailer.py` tails an actual log file and ships new
  lines to the API as they're written
- **Synthetic data generator** — populate the platform with thousands of realistic
  logs in seconds for demos or load testing
- **Auth + rate limiting** — optional API key auth and per-IP rate limiting on ingest
- **Retention job** — delete logs older than a configurable window, via API or cron
- **Dockerized** — `Dockerfile` + `docker-compose.yml` for one-command local deployment

## Architecture

```
[ingest_simulator.py / any log source]
              |
              v  POST /api/logs/ingest(/batch)
        [FastAPI backend]  --SQLite + FTS5-->  [telemetry.db]
              |
              v  GET /api/logs/search, /api/stats/*
      [Dashboard: index.html + Chart.js]
```

- **Backend**: FastAPI (`backend/main.py`) exposes ingest, search, and stats endpoints.
- **Storage/Index**: SQLite with an FTS5 virtual table for full-text search over log messages
  (kept in sync via triggers). Swappable later for OpenSearch/Elasticsearch or Postgres — the
  `database.py` module is the only place that would need to change.
- **Frontend**: a single static HTML page with vanilla JS + Chart.js (no build step needed),
  served by FastAPI's `StaticFiles`.
- **Simulator**: `ingest_simulator.py` generates realistic synthetic logs so you can populate
  and demo the platform immediately.

## Setup

```bash
cd backend
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

# Run the API + dashboard
uvicorn main:app --reload --port 8000
```

Open http://localhost:8000 in a browser — that's the dashboard.

In a second terminal, generate sample data:

```bash
cd backend
python ingest_simulator.py --count 5000 --hours-span 24
```

Refresh the dashboard: search box, level/source filters, and three charts
(volume over time, level breakdown, top sources) will populate.

## API summary

| Endpoint | Method | Auth | Purpose |
|---|---|---|---|
| `/api/logs/ingest` | POST | API key* | Ingest one log entry |
| `/api/logs/ingest/batch` | POST | API key* | Ingest a batch (`{"logs": [...]}`) |
| `/api/logs/search` | GET | — | Full-text + filtered search (`q`, `level`, `source`, `start`, `end`, `page`, `page_size`) |
| `/api/stats/volume` | GET | — | Time-bucketed counts (`bucket=minute\|hour\|day`) |
| `/api/stats/levels` | GET | — | Counts per log level |
| `/api/stats/sources` | GET | — | Top sources by volume |
| `/api/admin/retention` | POST | API key | Delete logs older than N days |
| `/api/health` | GET | — | Health check |

\* Auth is only enforced if the `API_KEY` environment variable is set (unset = open for local dev).

## Real ingestion (file tailing)

```bash
python file_tailer.py --file /path/to/app.log --api http://localhost:8000
```

Watches the file for new lines, parses `timestamp level source message` when it can,
and falls back to treating unstructured lines as INFO-level messages from `"unknown"`.

## Auth & rate limiting

```bash
export API_KEY=your-secret-key
export RATE_LIMIT_REQUESTS=100        # per window, default 100
export RATE_LIMIT_WINDOW_SECONDS=60   # default 60s
uvicorn main:app --reload --port 8000
```

Ingest requests then require `X-API-Key: your-secret-key`.

## Retention

```bash
python retention.py --days 30
```

Or via the API: `POST /api/admin/retention?days=30` (requires API key).

## Docker

```bash
docker compose up --build
```

Runs the API (and serves the dashboard) at http://localhost:8000. Set `API_KEY` in your
shell before running to enable auth in the container.

## Next steps

1. Swap SQLite for OpenSearch/Elasticsearch once volume/query needs grow.
2. Consume from a real queue (Kafka/SQS) instead of file tailing for distributed services.
3. Add error-rate alerting (e.g., threshold breach over a rolling window).
4. Deploy to a public host (Render/Fly.io) for a live demo link.
5. Move rate limiting to a shared store (Redis) before running multiple workers.


## License

MIT — free to use, modify, and build on.
