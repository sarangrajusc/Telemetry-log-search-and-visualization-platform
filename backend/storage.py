"""
Storage facade. The API, investigation pipeline and MCP server only talk to this
module, so the backing store is a config choice:

    STORAGE_BACKEND=sqlite          (default; SQLite + FTS5)
    STORAGE_BACKEND=elasticsearch   (see es_backend.py)
"""
import os

import database

_backend = None


def get_backend():
    global _backend
    if _backend is None:
        name = os.environ.get("STORAGE_BACKEND", "sqlite").lower()
        if name == "elasticsearch":
            import es_backend
            _backend = es_backend
        elif name == "sqlite":
            _backend = database
        else:
            raise ValueError(f"Unknown STORAGE_BACKEND {name!r}; use 'sqlite' or 'elasticsearch'")
    return _backend


def set_backend(backend):
    """Override the backend (used by tests and benchmarks)."""
    global _backend
    _backend = backend


def init_db():
    return get_backend().init_db()


def insert_log(entry):
    return get_backend().insert_log(entry)


def insert_logs_batch(entries):
    return get_backend().insert_logs_batch(entries)


def search_logs(**kwargs):
    return get_backend().search_logs(**kwargs)


def volume_over_time(**kwargs):
    return get_backend().volume_over_time(**kwargs)


def level_breakdown():
    return get_backend().level_breakdown()


def top_sources(limit=10):
    return get_backend().top_sources(limit=limit)


def recent_level_counts(minutes=15):
    return get_backend().recent_level_counts(minutes=minutes)
