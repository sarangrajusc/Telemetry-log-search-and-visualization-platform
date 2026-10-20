"""
API key auth + in-memory rate limiting.

Auth: set API_KEY. Requests must send `X-API-Key: <value>`; the comparison is
constant-time (hmac.compare_digest) so response timing can't be used to guess the key.
If API_KEY is unset, auth is disabled (local dev only).

By default only write/admin endpoints require the key, so the bundled dashboard keeps
working. Set PROTECT_READS=1 to require it on search/stats/investigation endpoints too
(recommended anywhere logs may contain sensitive data).

Rate limiting: sliding-window counter per client IP, in-memory. Fine for a single
process; move to Redis before running multiple workers.
"""
import hmac
import os
import time
from collections import defaultdict, deque
from fastapi import Header, HTTPException, Request

API_KEY = os.environ.get("API_KEY")
PROTECT_READS = os.environ.get("PROTECT_READS", "0").lower() in ("1", "true", "yes")
RATE_LIMIT_REQUESTS = int(os.environ.get("RATE_LIMIT_REQUESTS", "100"))
RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", "60"))

_request_log: dict[str, deque] = defaultdict(deque)


def _key_ok(provided) -> bool:
    if provided is None:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), API_KEY.encode("utf-8"))


def require_api_key(x_api_key: str = Header(default=None)):
    if API_KEY is None:
        return
    if not _key_ok(x_api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def require_read_access(x_api_key: str = Header(default=None)):
    """Key required for read endpoints only when PROTECT_READS is enabled."""
    if API_KEY is None or not PROTECT_READS:
        return
    if not _key_ok(x_api_key):
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
