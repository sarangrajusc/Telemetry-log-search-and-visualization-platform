"""
Shared HTTP client for shipping log batches to the platform's ingest API.

Used by file_tailer.py and ingest_simulator.py. Handles the things a naive
`requests.post` doesn't:

  * sends the API key (X-API-Key) when one is configured (flag or API_KEY env var)
  * retries transient failures (connection errors, 429, 5xx) with exponential backoff + jitter
  * fails fast, with a clear error, on auth failures (401/403)
  * distinguishes "the server rejected this data" (422) from "the server is unavailable",
    so callers can isolate a bad line instead of retrying it forever
"""
import os
import random
import time

import requests

BATCH_PATH = "/api/logs/ingest/batch"


class IngestError(Exception):
    """Base class for ingest failures."""


class AuthError(IngestError):
    """The API key is missing or wrong (HTTP 401/403). Retrying will not help."""


class RejectedError(IngestError):
    """The server rejected the payload as invalid (HTTP 400/422)."""


class TransientError(IngestError):
    """The server could not be reached or kept failing after all retries."""


class IngestClient:
    def __init__(self, base_url: str, api_key: str = None, max_retries: int = 5,
                 backoff_base: float = 0.5, backoff_max: float = 30.0, timeout: float = 10.0,
                 session=None, sleep=time.sleep, jitter=random.random):
        self.url = base_url.rstrip("/") + BATCH_PATH
        self.api_key = api_key if api_key is not None else os.environ.get("API_KEY")
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.timeout = timeout
        self.session = session or requests.Session()
        self._sleep = sleep
        self._jitter = jitter

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    def _delay(self, attempt: int, retry_after=None) -> float:
        if retry_after is not None:
            try:
                return min(float(retry_after), self.backoff_max)
            except ValueError:
                pass
        base = min(self.backoff_base * (2 ** attempt), self.backoff_max)
        return base * (0.5 + self._jitter() / 2)   # 50-100% of base, so clients don't retry in lockstep

    def post_batch(self, logs: list) -> int:
        """Send a batch. Returns the number ingested or raises an IngestError subclass."""
        last = "no attempt made"
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.session.post(self.url, json={"logs": logs}, headers=self._headers(),
                                         timeout=self.timeout)
            except requests.RequestException as e:
                last = f"{type(e).__name__}: {e}"
            else:
                code = resp.status_code
                if 200 <= code < 300:
                    return len(logs)
                if code in (401, 403):
                    raise AuthError(f"HTTP {code}: check API_KEY / --api-key")
                if code in (400, 422):
                    raise RejectedError(f"HTTP {code}: {resp.text[:300]}")
                if code == 429 or code >= 500:
                    last = f"HTTP {code}"
                    if attempt < self.max_retries:
                        self._sleep(self._delay(attempt, resp.headers.get("Retry-After")))
                        continue
                else:
                    raise IngestError(f"unexpected HTTP {code}: {resp.text[:300]}")
            if attempt < self.max_retries:
                self._sleep(self._delay(attempt))
        raise TransientError(f"gave up after {self.max_retries + 1} attempts ({last})")
