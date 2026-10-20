"""
Elasticsearch storage backend. Mirrors the function surface of `database.py` so
`storage.py` can swap between them with STORAGE_BACKEND=elasticsearch.

Config (environment):
    ES_URL        default http://localhost:9200
    ES_API_KEY    optional API key (never hard-code credentials; see .env.example)
    ES_INDEX      default telemetry-logs

Notes:
  * Deep pagination uses from/size, which Elasticsearch caps at 10,000 hits
    (index.max_result_window). Beyond that, switch to search_after.
  * This module is exercised in tests with a fake client; it has not been run against
    a live cluster in this repo's CI.
"""
import os
from datetime import datetime, timedelta, timezone

ES_URL = os.environ.get("ES_URL", "http://localhost:9200")
ES_API_KEY = os.environ.get("ES_API_KEY")
ES_INDEX = os.environ.get("ES_INDEX", "telemetry-logs")

_client = None

MAPPING = {
    "mappings": {
        "properties": {
            "timestamp": {"type": "date"},
            "source": {"type": "keyword"},
            "host": {"type": "keyword"},
            "level": {"type": "keyword"},
            "message": {"type": "text"},
            "tags": {"type": "flattened"},
        }
    }
}

_INTERVAL = {"minute": "minute", "hour": "hour", "day": "day"}
_BUCKET_FORMAT = {"minute": "yyyy-MM-dd'T'HH:mm", "hour": "yyyy-MM-dd'T'HH", "day": "yyyy-MM-dd"}


def get_client():
    global _client
    if _client is None:
        from elasticsearch import Elasticsearch

        kwargs = {"api_key": ES_API_KEY} if ES_API_KEY else {}
        _client = Elasticsearch(ES_URL, **kwargs)
    return _client


def set_client(client):
    """Inject a client (used by tests)."""
    global _client
    _client = client


def init_db():
    client = get_client()
    if not client.indices.exists(index=ES_INDEX):
        client.indices.create(index=ES_INDEX, **MAPPING)


def _doc(entry):
    ts = entry.timestamp or datetime.now(timezone.utc).replace(tzinfo=None)
    return {
        "timestamp": ts.isoformat(),
        "source": entry.source,
        "host": entry.host,
        "level": entry.level.upper(),
        "message": entry.message,
        "tags": entry.tags or {},
    }


def insert_log(entry) -> str:
    resp = get_client().index(index=ES_INDEX, document=_doc(entry), refresh="wait_for")
    return resp["_id"]


def insert_logs_batch(entries) -> int:
    from elasticsearch import helpers

    actions = ({"_index": ES_INDEX, "_source": _doc(e)} for e in entries)
    ok, _ = helpers.bulk(get_client(), actions, refresh="wait_for")
    return ok


def _filters(level=None, source=None, start=None, end=None):
    filters = []
    if level:
        filters.append({"term": {"level": level.upper()}})
    if source:
        filters.append({"term": {"source": source}})
    if start or end:
        rng = {}
        if start:
            rng["gte"] = start
        if end:
            rng["lte"] = end
        filters.append({"range": {"timestamp": rng}})
    return filters


def search_logs(query=None, level=None, source=None, start=None, end=None, page=1, page_size=50):
    must = [{"match": {"message": {"query": query, "operator": "or"}}}] if query else []
    body = {
        "query": {"bool": {"must": must, "filter": _filters(level, source, start, end)}},
        "sort": [{"timestamp": {"order": "desc"}}],
        "from": (page - 1) * page_size,
        "size": page_size,
        "track_total_hits": True,
    }
    resp = get_client().search(index=ES_INDEX, **body)
    hits = resp["hits"]
    total = hits["total"]["value"] if isinstance(hits["total"], dict) else hits["total"]
    results = []
    for h in hits["hits"]:
        s = h["_source"]
        results.append({
            "id": h["_id"], "timestamp": s["timestamp"], "source": s["source"],
            "host": s.get("host"), "level": s["level"], "message": s["message"],
            "tags": s.get("tags") or {},
        })
    return total, results


def volume_over_time(bucket="hour", start=None, end=None):
    interval = _INTERVAL.get(bucket, "hour")
    body = {
        "size": 0,
        "query": {"bool": {"filter": _filters(start=start, end=end)}},
        "aggs": {"v": {"date_histogram": {
            "field": "timestamp", "calendar_interval": interval,
            "format": _BUCKET_FORMAT.get(bucket, _BUCKET_FORMAT["hour"]),
        }}},
    }
    resp = get_client().search(index=ES_INDEX, **body)
    return [{"bucket": b["key_as_string"], "count": b["doc_count"]}
            for b in resp["aggregations"]["v"]["buckets"]]


def _terms(field, size=50, filters=None):
    body = {
        "size": 0,
        "query": {"bool": {"filter": filters or []}},
        "aggs": {"t": {"terms": {"field": field, "size": size}}},
    }
    resp = get_client().search(index=ES_INDEX, **body)
    return resp["aggregations"]["t"]["buckets"]


def level_breakdown():
    return [{"level": b["key"], "count": b["doc_count"]} for b in _terms("level")]


def top_sources(limit=10):
    return [{"source": b["key"], "count": b["doc_count"]} for b in _terms("source", size=limit)]


def recent_level_counts(minutes=15):
    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=minutes)).isoformat()
    buckets = _terms("level", filters=[{"range": {"timestamp": {"gte": cutoff}}}])
    return {b["key"]: b["doc_count"] for b in buckets}


def purge_older_than(cutoff_iso: str) -> int:
    resp = get_client().delete_by_query(
        index=ES_INDEX, query={"range": {"timestamp": {"lt": cutoff_iso}}}, refresh=True
    )
    return resp.get("deleted", 0)
