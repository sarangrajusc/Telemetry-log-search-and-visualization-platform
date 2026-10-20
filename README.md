# Telemetry Log Search & Visualization Platform

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-REST-009688)
![MCP](https://img.shields.io/badge/MCP-server-8A2BE2)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

A platform for ingesting telemetry/log events, searching them, visualizing volume and
error trends, and **investigating incidents with an AI assistant** over an MCP server.
Built on FastAPI with pluggable storage (SQLite FTS5 or Elasticsearch).

## Features

- **RESTful API** (FastAPI): batch/single ingest, filtered full-text search, stats,
  alerting, retention and incident-investigation endpoints with validated inputs
- **AI-assisted incident investigation**: retrieves grounded evidence (error patterns,
  affected sources, timeline, keyword matches, log ids to cite) for a natural-language
  question; exposed over REST and as an **MCP server** so an assistant like Claude can
  query the logs directly
- **Pluggable storage**: SQLite + FTS5 by default, or Elasticsearch
  (`STORAGE_BACKEND=elasticsearch`)
- **Concurrency + caching**: investigation sub-queries run concurrently on a thread pool
  with per-task timeouts and graceful degradation; query results are held in a
  thread-safe TTL/LRU cache
- **Security**: secret redaction on ingest, constant-time API key checks, optional
  read protection, CORS off by default, rate limiting, hardened GitHub Actions
- **Live dashboard**: volume over time, level breakdown, top sources (Chart.js)
- **Ingestion paths**: REST, a file-tailing agent, and a synthetic generator
- **Error-rate alerting**, **retention job**, **Docker** support
- **58 automated tests** (pytest), run in CI on every push/PR

## Architecture

```
 log sources (REST / file_tailer.py / simulator)
        |  POST /api/logs/ingest(/batch)
        v
 [validate + redact secrets] --> [storage facade] --> SQLite FTS5  or  Elasticsearch
                                       ^
        +------------------------------+-------------------------------+
        |                              |                               |
  REST: search / stats          investigate.py                   mcp_server.py
  (+ TTL cache)                 5 concurrent sub-queries         read-only tools for
        |                       + cache + template clustering    AI assistants
        v                              |                               |
  dashboard (Chart.js)          evidence pack with log-id citations -> LLM client reasons over it
```

## Setup

```bash
cd backend
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Open http://localhost:8000. To load sample data: `python ingest_simulator.py --count 5000`.

Configuration is via environment variables; copy `.env.example` for the full list.

## Running tests

```bash
cd backend
TESTING=1 pytest -v
```

## API summary

| Endpoint | Method | Auth | Purpose |
|---|---|---|---|
| `/api/logs/ingest` | POST | API key* | Ingest one log entry |
| `/api/logs/ingest/batch` | POST | API key* | Ingest a batch (`{"logs": [...]}`) |
| `/api/logs/search` | GET | read** | Full-text + filtered search (`q`, `level`, `source`, `start`, `end`, `page`, `page_size`) |
| `/api/investigate` | GET | read** | Evidence pack for a question (`q`, `window_minutes`, `sample_size`) |
| `/api/stats/volume` | GET | read** | Time-bucketed counts (`bucket=minute\|hour\|day`) |
| `/api/stats/levels` | GET | read** | Counts per log level |
| `/api/stats/sources` | GET | read** | Top sources by volume |
| `/api/alerts/check` | GET | read** | Error rate over a rolling window vs. threshold |
| `/api/cache/stats` | GET | read** | Cache hits/misses/hit rate |
| `/api/admin/retention` | POST | API key | Delete logs older than N days |
| `/api/health` | GET | none | Health check |

\* Enforced only if `API_KEY` is set. \*\* Enforced only if `API_KEY` is set **and** `PROTECT_READS=1`.

Search accepts raw FTS5 syntax; if a query isn't valid FTS5 (e.g. `auth-service`), it falls
back to a literal match on its words instead of returning a 500.

## AI-assisted incident investigation

```bash
curl "localhost:8000/api/investigate?q=why+is+payments-api+failing&window_minutes=60"
```

Returns structured evidence plus a compact `context` block. Real output from a seeded test
incident (30 payment timeouts, 5 auth errors, 65 healthy requests):

```
Incident investigation: why is payments-api failing
Window: 2026-10-20T05:07:00 to 2026-10-20T06:07:00 UTC (60 min)
Logs in window: 100; levels: {'ERROR': 35, 'INFO': 65}; error rate: 35.0%
Peak volume bucket: 2026-10-20T06:04 (65 logs)
Sources with most errors (sampled): {'payments-api': 30, 'auth-service': 5}

Top error patterns:
  1. x30 "Connection timeout to db-<n> after <n>ms" sources={'payments-api': 30} first=... last=... [log:30]
  2. x5 "Invalid token for user <n>" sources={'auth-service': 5} first=... last=... [log:35]

Logs matching ['payments-api']: 30 total
  1. x30 "Connection timeout to db-<n> after <n>ms" [log:30]
```

**How the retrieval works.** The question is reduced to search terms; independent sub-queries
(level counts, ERROR sample, WARN sample, keyword matches, volume timeline) run concurrently;
messages are normalised into templates (numbers, IPs, ids replaced) so thousands of similar
lines collapse into a few patterns; each pattern keeps an example log id for citation.
This is keyword/BM25-style retrieval (FTS5 or Elasticsearch `match`), **not vector
embeddings**. Patterns are computed over a sample (`sample_size`); the response states the
sample size versus the true total.

### MCP server

```bash
cd backend && python mcp_server.py        # stdio transport
```

Tools (all read-only): `investigate_incident`, `search_logs`, `get_error_patterns`,
`check_error_rate`. Example client config (Claude Desktop / Claude Code):

```json
{"mcpServers": {"telemetry": {"command": "python",
  "args": ["/absolute/path/to/backend/mcp_server.py"],
  "env": {"DB_PATH": "/absolute/path/to/telemetry.db"}}}}
```

Log text is untrusted input (it can contain text that looks like instructions), so the server
tells clients to treat it strictly as data, exposes no write/delete tools, and secrets are
redacted at ingest before an assistant can ever see them.

## Elasticsearch backend

```bash
export STORAGE_BACKEND=elasticsearch ES_URL=http://localhost:9200 ES_API_KEY=...
```

`es_backend.py` implements the same operations (bool/filter queries, `date_histogram` and
`terms` aggregations, bulk indexing, delete-by-query retention). It is covered by unit tests
against a fake client; **it has not been run against a live cluster yet.** `from/size`
pagination is capped at 10,000 hits by Elasticsearch; use `search_after` beyond that.

## Concurrency and caching

- Investigation fans out 5 independent sub-queries on a thread pool and joins them with a
  per-task timeout (`INVESTIGATE_TASK_TIMEOUT`). A failed or slow sub-query is reported under
  `partial_failures` and the rest of the report is still returned.
- Results are memoised in a thread-safe TTL + LRU cache (`CACHE_TTL_SECONDS`, default 15s).
  Entries expire by TTL rather than on every write, so numbers can be up to one TTL stale;
  lower it if you need fresher data. The investigation window is quantised to 15s so
  back-to-back requests share cache keys.
- The concurrency model is Python threads (the Java equivalent would be `CompletableFuture`
  composition). SQLite releases the GIL while it executes queries, so reads overlap; a
  network store like Elasticsearch benefits more.

## Security

- **Secret redaction at ingest** (`redaction.py`): AWS/GitHub/Slack/Stripe/Google keys, JWTs,
  bearer tokens, URL credentials, private keys and `password=`/`token=`-style assignments are
  replaced with `[REDACTED:<kind>]` before storage; sensitive tag keys are masked. It is
  pattern-based defence in depth, not a substitute for not logging secrets.
- **Auth**: constant-time key comparison (`hmac.compare_digest`); `PROTECT_READS=1` extends the
  key requirement to search/stats/investigation; CORS is disabled unless `CORS_ORIGINS` is set.
- **Secrets hygiene**: `.env`, keys and local DBs are gitignored; `.env.example` holds
  placeholders only; the repo contents were scanned for credential patterns (none found) and
  CI runs gitleaks over full history.
- **GitHub Actions hardening**: every action pinned to a full commit SHA, read-only default
  `GITHUB_TOKEN` with per-job escalation only where needed (CodeQL), `persist-credentials: false`,
  job timeouts, concurrency cancellation, Dependabot for actions and pip, weekly CodeQL +
  gitleaks + `pip-audit`.
- **Fixed along the way**: a timezone bug where a naive-UTC datetime passed through
  `.timestamp()` shifted the query window by the machine's UTC offset (covered by a regression
  test that runs under several timezones).

## Performance (measured)

```bash
cd backend && python benchmark.py --rows 500000
```

Results at **500,000 rows**, SQLite + FTS5, single run on a shared cloud container
(your hardware will differ; medians over repeated runs):

| Operation | Result |
|---|---|
| Ingestion (validate + redact + insert) | ~12,000 rows/sec |
| Full-text search, top 50 | ~52 ms |
| Filtered search (level + source) | ~66 ms |
| Dashboard stats (3 aggregates, uncached) | ~342 ms |
| Incident investigation, 24h window, sequential | ~820 ms |
| Incident investigation, concurrent | ~483 ms (1.7x) |
| Incident investigation, warm cache | ~10 ms (~80x vs. sequential) |

Ingestion is lower than the earlier 50K benchmark (~32K rows/sec) because that run built
the entries outside the timer; this one includes validation and secret redaction.
The warm-cache figure still includes clustering the cached samples and assembling the report.

## Real ingestion (file tailing)

```bash
python file_tailer.py --file /path/to/app.log --api http://localhost:8000
```

## Retention and alerting

```bash
python retention.py --days 30       # or POST /api/admin/retention?days=30 (API key)
```

`GET /api/alerts/check` returns the error rate over a rolling window and posts to
`ALERT_WEBHOOK_URL` if it exceeds `ERROR_RATE_THRESHOLD`.

## Docker

```bash
docker compose up --build
```

## Next steps

1. Run the Elasticsearch backend against a live cluster and compare it with SQLite using
   `benchmark.py`.
2. Add embedding-based (vector) retrieval alongside keyword search for semantic matching.
3. Move rate limiting to a shared store (Redis) before running multiple workers.
4. Deploy to a public host for a live demo.

## License

MIT
