"""
Simple API key auth + in-memory rate limiting for the ingest endpoints.

Auth: set API_KEY env var. Requests must send header `X-API-Key: <value>`.
If API_KEY is unset, auth is disabled (useful for local dev).

Rate limiting: fixed-window counter per client IP, in-memory. Fine for a
single-process demo/portfolio deployment; swap for Redis-backed limiting
before running multiple workers or in production.
"""
import os
import time
from collections import defaultdict, deque
from fastapi import Header, HTTPException, Request

API_KEY = os.environ.get("API_KEY")  # None disables auth
RATE_LIMIT_REQUESTS = int(os.environ.get("RATE_LIMIT_REQUESTS", "100"))
RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", "60"))

_request_log: dict[str, deque] = defaultdict(deque)


def require_api_key(x_api_key: str = Header(default=None)):
    if API_KEY is None:
        return  # auth disabled locally
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def rate_limit(request: Request):
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()
    window = _request_log[client_ip]

    while window and now - window[0] > RATE_LIMIT_WINDOW_SECONDS:
        window.popleft()

    if len(window) >= RATE_LIMIT_REQUESTS:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: {RATE_LIMIT_REQUESTS} requests per {RATE_LIMIT_WINDOW_SECONDS}s",
        )
    window.append(now)
