# Telemetry Log Search & Visualization Platform

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688)
![Tests](https://img.shields.io/badge/tests-17%20passing-brightgreen)
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
- **Error-rate alerting** — checks the error rate over a rolling window and can fire a
  webhook notification if it crosses a threshold
- **Automated tests** — 17 pytest tests covering ingestion, search, stats, auth, rate
  limiting, and alerting, run automatically on every push via GitHub Actions
- **Dockerized** — `Dockerfile` + `docker-compose.yml` for one-command local deployment

## Setup

```bash
cd backend
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

uvicorn main:app --reload --port 8000
```

Open http://localhost:8000 for the dashboard.

In a second terminal, generate sample data:

```bash
cd backend
python ingest_simulator.py --count 5000 --hours-span 24
```

## Running tests

```bash
cd backend
TESTING=1 pytest -v
```

17 tests covering ingestion validation, full-text and filtered search, pagination,
stats aggregation, alerting, API key auth, and rate limiting. Runs automatically on
every push/PR via the GitHub Actions workflow in `.github/workflows/tests.yml`.

## API summary

| Endpoint | Method | Auth | Purpose |
|---|---|---|---|
| `/api/logs/ingest` | POST | API key* | Ingest one log entry |
| `/api/logs/ingest/batch` | POST | API key* | Ingest a batch (`{"logs": [...]}`) |
| `/api/logs/search` | GET | — | Full-text + filtered search (`q`, `level`, `source`, `start`, `end`, `page`, `page_size`) |
| `/api/stats/volume` | GET | — | Time-bucketed counts (`bucket=minute\|hour\|day`) |
| `/api/stats/levels` | GET | — | Counts per log level |
| `/api/stats/sources` | GET | — | Top sources by volume |
| `/api/alerts/check` | GET | — | Error rate over a rolling window; flags if above threshold |
| `/api/admin/retention` | POST | API key | Delete logs older than N days |
| `/api/health` | GET | — | Health check |

\* Auth is only enforced if the `API_KEY` environment variable is set (unset = open for local dev).

## Real ingestion (file tailing)

```bash
python file_tailer.py --file /path/to/app.log --api http://localhost:8000
```

## Auth & rate limiting

```bash
export API_KEY=your-secret-key
export RATE_LIMIT_REQUESTS=100
export RATE_LIMIT_WINDOW_SECONDS=60
uvicorn main:app --reload --port 8000
```

## Error-rate alerting

```bash
export ERROR_RATE_THRESHOLD=0.1    # 10%
export ALERT_WINDOW_MINUTES=15
export ALERT_WEBHOOK_URL=https://hooks.slack.com/...   # optional
```

`GET /api/alerts/check` returns the current error rate and whether it's over threshold,
and posts to the webhook if so. Designed to be called periodically (cron, a scheduled
task, or a monitoring tool) rather than run as an always-on process — see the docstring
in `alerting.py` for the reasoning and when that tradeoff would need to change.

## Retention

```bash
python retention.py --days 30
```

Or via the API: `POST /api/admin/retention?days=30` (requires API key).

## Performance benchmark

```bash
python benchmark.py --rows 100000
```

Measured results at 50,000 rows (single-process, SQLite + FTS5, this machine):

| Operation | Result |
|---|---|
| Ingestion | ~32,000 rows/sec (batched) |
| Full-text search | ~14ms avg |
| Filtered search (level + source) | ~9ms avg |
| Dashboard stats (3 aggregate queries) | ~35ms avg |

These numbers are what justify staying on SQLite for now rather than standing up
Elasticsearch/OpenSearch: query latency is well under what a dashboard or search UI
needs at this volume. The point where that would change is high-concurrency write
load or much larger datasets (10M+ rows) where FTS5's single-writer model and lack of
distributed indexing start to show. Re-run `benchmark.py` at larger `--rows` values to
see where that curve bends on your own data.

## Docker

```bash
docker compose up --build
```

## Architecture

```
[ingest_simulator.py / file_tailer.py / any log source]
              |
              v  POST /api/logs/ingest(/batch)
        [FastAPI backend]  --SQLite + FTS5-->  [telemetry.db]
              |
              v  GET /api/logs/search, /api/stats/*, /api/alerts/check
      [Dashboard: index.html + Chart.js]
```

## Next steps

1. Swap SQLite for OpenSearch/Elasticsearch if the benchmark above stops holding at
   real production volume.
2. Consume from a real queue (Kafka/SQS) instead of file tailing for distributed services.
3. Move rate limiting to a shared store (Redis) before running multiple workers.
4. Deploy to a public host (Render/Fly.io) for a live demo link.

## License

MIT — free to use, modify, and build on.
