# Deployment notes

Status: the Dockerfile and docker-compose.yml were validated (`docker compose config`, persistence
checked by simulation), but the image has not been built in CI and `render.yaml` has not been
deployed. Treat both as a starting point and check them on first use.

## Local or single host (Docker)

```bash
cp .env.example .env        # set API_KEY at least
docker compose up --build
```

- Dashboard and API: http://localhost:8000
- Data lives in the `telemetry-data` volume at `/data/telemetry.db` and survives restarts.
- The container runs as uid 10001. If you mount a host folder instead of the volume, make it
  writable by that uid; the app stops at startup with a clear message if it is not.
- Elasticsearch is optional: `docker compose --profile elasticsearch up --build`, then set
  `STORAGE_BACKEND=elasticsearch`. The bundled ES has security off and is bound to localhost only.

## Settings checklist

| Variable | Purpose |
|---|---|
| `API_KEY` | Required for writes. Unset means open writes (fine locally only). |
| `PROTECT_READS=1` | Also require the key for search, stats and investigation. The dashboard has a key box for this. |
| `CORS_ORIGINS` | Leave empty; the dashboard is same-origin. |
| `RATE_LIMIT_REQUESTS` / `RATE_LIMIT_WINDOW_SECONDS` | Per-IP limit on writes. |
| `DB_PATH` | SQLite file location (`/data/telemetry.db` in Docker). |
| `ALERT_WEBHOOK_URL`, `ERROR_RATE_THRESHOLD`, `ALERT_WINDOW_MINUTES` | Alerting. |

## Behind a proxy (Render, nginx, a load balancer)

Rate limiting keys on the client IP. Behind a proxy every request appears to come from the proxy, so
all users share one bucket. Setting `FORWARDED_ALLOW_IPS=*` makes uvicorn trust `X-Forwarded-For`,
which gives per-client limits, but it is only safe when the app is reachable exclusively through that
proxy; otherwise a client can spoof the header and dodge the limit. Prefer listing the proxy's IP.

## Render

`render.yaml` defines a Docker web service, a generated `API_KEY`, `PROTECT_READS=1`, and a 1 GB disk
at `/data`. Render only offers disks on paid plans; on the free plan remove the `disk:` block and
accept that data is lost on each deploy. Read the generated key in the Render dashboard and paste it
into the dashboard's key box.

## Limits to know about

- SQLite means one instance. Do not scale to several replicas; use the Elasticsearch backend for that.
- Rate limiting is in memory per process; use a shared store (Redis) before running multiple workers.
- Ingest from your hosts with `file_tailer.py --api https://<host> --api-key <key>`.
