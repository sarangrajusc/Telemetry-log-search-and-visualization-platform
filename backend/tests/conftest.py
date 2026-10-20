"""Shared test setup: throwaway DB, no auth, and clean cache/backend state per test."""
import os
import tempfile

os.environ.setdefault("TESTING", "1")
_fd, _path = tempfile.mkstemp(suffix=".db")
os.environ["DB_PATH"] = _path
os.environ["STORAGE_BACKEND"] = "sqlite"
os.environ.pop("API_KEY", None)

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_state():
    import cache
    import security
    import storage

    cache.query_cache.clear()
    cache.query_cache.hits = 0
    cache.query_cache.misses = 0
    storage.set_backend(None)  # re-resolve from env on next use
    security._request_log.clear()
    yield
