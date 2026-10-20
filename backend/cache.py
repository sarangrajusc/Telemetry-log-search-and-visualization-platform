"""
Small thread-safe TTL + LRU cache used to avoid re-running identical expensive
queries (dashboard aggregates, incident-investigation sub-queries).

Trade-off: entries are only invalidated by TTL (or an explicit clear), not on every
ingest. Clearing on each write would make the cache useless under steady ingestion,
so results can be up to `ttl` seconds stale. The default (15s) is fine for a
dashboard and for incident triage; lower it via CACHE_TTL_SECONDS if you need fresher
numbers.
"""
import functools
import os
import threading
import time
from collections import OrderedDict

DEFAULT_TTL = float(os.environ.get("CACHE_TTL_SECONDS", "15"))
DEFAULT_MAX_ENTRIES = int(os.environ.get("CACHE_MAX_ENTRIES", "512"))


class TTLCache:
    def __init__(self, ttl: float = DEFAULT_TTL, max_entries: int = DEFAULT_MAX_ENTRIES, clock=time.monotonic):
        self.ttl = ttl
        self.max_entries = max_entries
        self._clock = clock
        self._data: "OrderedDict[object, tuple[float, object]]" = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def lookup(self, key):
        """Return (hit, value). A cached value of None still counts as a hit."""
        now = self._clock()
        with self._lock:
            item = self._data.get(key)
            if item is not None:
                expires_at, value = item
                if expires_at > now:
                    self._data.move_to_end(key)
                    self.hits += 1
                    return True, value
                del self._data[key]
            self.misses += 1
        return False, None

    def get(self, key, default=None):
        hit, value = self.lookup(key)
        return value if hit else default

    def set(self, key, value):
        with self._lock:
            self._data[key] = (self._clock() + self.ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)

    def clear(self):
        with self._lock:
            self._data.clear()

    def stats(self) -> dict:
        with self._lock:
            total = self.hits + self.misses
            return {
                "entries": len(self._data),
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": round(self.hits / total, 4) if total else 0.0,
                "ttl_seconds": self.ttl,
                "max_entries": self.max_entries,
            }


# Shared cache for query results.
query_cache = TTLCache()


def _freeze(value):
    """Make arguments hashable so they can be used in a cache key."""
    if isinstance(value, dict):
        return tuple(sorted((k, _freeze(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple, set)):
        return tuple(_freeze(v) for v in value)
    return value


def cached(cache: TTLCache = None):
    """Decorator: memoize a function's result in `cache` keyed by its arguments."""

    def decorator(fn):
        target = cache or query_cache

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            key = (fn.__module__, fn.__qualname__, _freeze(args), _freeze(kwargs))
            hit, value = target.lookup(key)
            if hit:
                return value
            result = fn(*args, **kwargs)
            target.set(key, result)
            return result

        wrapper.cache = target
        return wrapper

    return decorator
