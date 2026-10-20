"""
AI-assisted incident investigation: retrieval + evidence assembly.

Given a question ("why are payments failing?") and a time window, this gathers
grounded evidence from the log store and returns it as structured data plus a compact
text `context` block with log-id citations. It is the "retrieval" half of RAG: an LLM
client (e.g. Claude via the MCP server in mcp_server.py) does the reasoning and
writes the answer from this evidence, so conclusions can be traced back to real log
lines.

Retrieval here is keyword/BM25-style (SQLite FTS5 or Elasticsearch `match`), combined
with message-template clustering so thousands of near-identical lines collapse into a
handful of patterns. It does not use vector embeddings.

Concurrency: the independent sub-queries run in parallel on a thread pool and are
joined with a per-task timeout. A failed or slow sub-query degrades that section of the
report instead of failing the whole investigation (the same idea as composing futures
with allOf + per-future fallbacks). Each sub-query result is also memoized in a short
TTL cache, so repeated or overlapping investigations don't re-hit the store.
"""
import os
import re
import time
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timedelta, timezone

import storage
from cache import cached, query_cache

MAX_WORKERS = int(os.environ.get("INVESTIGATE_WORKERS", "8"))
TASK_TIMEOUT_SECONDS = float(os.environ.get("INVESTIGATE_TASK_TIMEOUT", "20"))
WINDOW_QUANTUM_SECONDS = int(os.environ.get("INVESTIGATE_WINDOW_QUANTUM", "15"))

_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="investigate")

STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "why", "what", "when",
    "where", "which", "who", "how", "did", "do", "does", "to", "of", "in", "on", "for",
    "and", "or", "with", "from", "by", "at", "it", "this", "that", "these", "those",
    "there", "any", "all", "show", "me", "find", "get", "failing", "failed", "failure",
    "errors", "error", "logs", "log", "last", "recent", "happening", "happened",
}

_TEMPLATE_RULES = [
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<uuid>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "<ip>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<hex>"),
    (re.compile(r"\b[0-9a-f]{16,}\b"), "<hex>"),
    (re.compile(r"\d+"), "<n>"),
]


def question_terms(question: str, limit: int = 6) -> list:
    """Pull search terms from a natural-language question (drops stopwords, de-dupes)."""
    seen = OrderedDict()
    for tok in re.findall(r"[A-Za-z][\w-]{2,}", question or ""):
        low = tok.lower()
        if low not in STOPWORDS:
            seen.setdefault(low, None)
    return list(seen)[:limit]


def normalize_template(message: str) -> str:
    """Collapse variable parts (numbers, ids, IPs) so similar lines share one pattern."""
    out = message
    for rx, repl in _TEMPLATE_RULES:
        out = rx.sub(repl, out)
    return out.strip()


def cluster_patterns(rows, top: int = 8) -> list:
    """Group log rows by message template; return the most frequent patterns."""
    groups = {}
    for r in rows:
        key = normalize_template(r["message"])
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "pattern": key, "count": 0, "sources": Counter(), "levels": Counter(),
                "first_seen": r["timestamp"], "last_seen": r["timestamp"],
                "example": {"id": r["id"], "message": r["message"], "source": r["source"],
                            "timestamp": r["timestamp"]},
            }
        g["count"] += 1
        g["sources"][r["source"]] += 1
        g["levels"][r["level"]] += 1
        g["first_seen"] = min(g["first_seen"], r["timestamp"])
        g["last_seen"] = max(g["last_seen"], r["timestamp"])
    ranked = sorted(groups.values(), key=lambda g: g["count"], reverse=True)[:top]
    for g in ranked:
        g["sources"] = dict(g["sources"].most_common(5))
        g["levels"] = dict(g["levels"])
    return ranked


def _window(minutes: int, now: datetime = None):
    """Window [start, end]. `end` is quantized so back-to-back calls share cache keys."""
    # Timestamps in the store are naive UTC. Never call .timestamp() on a naive datetime:
    # Python would treat it as *local* time and shift the window by the machine's UTC offset.
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    epoch = now.replace(tzinfo=timezone.utc).timestamp()
    q = WINDOW_QUANTUM_SECONDS
    end = datetime.fromtimestamp((int(epoch) // q) * q, tz=timezone.utc).replace(tzinfo=None)
    start = end - timedelta(minutes=minutes)
    return start.isoformat(), end.isoformat()


# ---- Independent sub-queries (cached; safe to run concurrently) -------------------

@cached()
def _level_counts(minutes, end):
    return storage.recent_level_counts(minutes=minutes)


@cached()
def _level_sample(level, start, limit):
    total, rows = storage.search_logs(level=level, start=start, page_size=limit)
    return {"total": total, "rows": rows}


@cached()
def _keyword_sample(terms, start, limit):
    if not terms:
        return {"total": 0, "rows": []}
    from database import fts_safe_query

    if storage.get_backend().__name__ == "es_backend":
        query = " ".join(terms)
    else:
        query = fts_safe_query(" ".join(terms), operator="OR")
    total, rows = storage.search_logs(query=query, start=start, page_size=limit)
    return {"total": total, "rows": rows}


@cached()
def _timeline(start, bucket):
    return storage.volume_over_time(bucket=bucket, start=start)


def _run_tasks(tasks: dict, parallel: bool, timeout: float):
    """Run named zero-arg callables; return (results, errors). Never raises per-task."""
    results, errors = {}, {}
    if not parallel:
        for name, fn in tasks.items():
            try:
                results[name] = fn()
            except Exception as e:  # degrade this section, keep the rest
                errors[name] = f"{type(e).__name__}: {e}"
        return results, errors

    futures = {name: _executor.submit(fn) for name, fn in tasks.items()}
    deadline = time.monotonic() + timeout
    for name, fut in futures.items():
        try:
            results[name] = fut.result(timeout=max(0.0, deadline - time.monotonic()))
        except FutureTimeout:
            fut.cancel()
            errors[name] = f"timed out after {timeout:.0f}s"
        except Exception as e:
            errors[name] = f"{type(e).__name__}: {e}"
    return results, errors


def investigate(question: str = "", window_minutes: int = 60, sample_size: int = 500,
                parallel: bool = True, use_cache: bool = True, now: datetime = None) -> dict:
    t0 = time.perf_counter()
    start, end = _window(window_minutes, now)
    terms = tuple(question_terms(question))
    bucket = "minute" if window_minutes <= 180 else "hour"

    if not use_cache:
        query_cache.clear()

    tasks = {
        "levels": lambda: _level_counts(window_minutes, end),
        "errors": lambda: _level_sample("ERROR", start, sample_size),
        "warnings": lambda: _level_sample("WARN", start, sample_size),
        "keyword": lambda: _keyword_sample(terms, start, sample_size),
        "timeline": lambda: _timeline(start, bucket),
    }
    res, errors = _run_tasks(tasks, parallel, TASK_TIMEOUT_SECONDS)

    levels = res.get("levels", {})
    total_logs = sum(levels.values())
    err_total = levels.get("ERROR", 0)
    err_sample = res.get("errors", {"total": 0, "rows": []})
    warn_sample = res.get("warnings", {"total": 0, "rows": []})
    kw = res.get("keyword", {"total": 0, "rows": []})
    timeline = res.get("timeline", [])

    affected = Counter(r["source"] for r in err_sample["rows"])
    peak = max(timeline, key=lambda b: b["count"]) if timeline else None

    evidence = {
        "question": question,
        "window": {"start": start, "end": end, "minutes": window_minutes},
        "search_terms": list(terms),
        "level_counts": levels,
        "error_rate": round(err_total / total_logs, 4) if total_logs else 0.0,
        "total_logs_in_window": total_logs,
        "top_error_patterns": cluster_patterns(err_sample["rows"]),
        "top_warning_patterns": cluster_patterns(warn_sample["rows"], top=5),
        "affected_sources": dict(affected.most_common(8)),
        "keyword_matches": {
            "total": kw["total"],
            "patterns": cluster_patterns(kw["rows"], top=5),
        },
        "timeline": {"bucket": bucket, "points": timeline, "peak": peak},
        "sampling": {
            "errors_sampled": len(err_sample["rows"]), "errors_total": err_sample["total"],
            "sample_size": sample_size,
        },
        "partial_failures": errors,
    }
    evidence["context"] = build_context(evidence)
    evidence["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return evidence


def build_context(ev: dict) -> str:
    """Compact, citation-bearing text block for an LLM to reason over."""
    w = ev["window"]
    lines = [
        f"Incident investigation: {ev['question'] or '(general triage)'}",
        f"Window: {w['start']} to {w['end']} UTC ({w['minutes']} min)",
        f"Logs in window: {ev['total_logs_in_window']}; levels: {ev['level_counts']}; "
        f"error rate: {ev['error_rate'] * 100:.1f}%",
    ]
    peak = ev["timeline"]["peak"]
    if peak:
        lines.append(f"Peak volume bucket: {peak['bucket']} ({peak['count']} logs)")
    if ev["affected_sources"]:
        lines.append(f"Sources with most errors (sampled): {ev['affected_sources']}")
    s = ev["sampling"]
    if s["errors_total"] > s["errors_sampled"]:
        lines.append(f"Note: patterns below are from a sample of {s['errors_sampled']} of "
                     f"{s['errors_total']} errors.")
    lines.append("")
    lines.append("Top error patterns:")
    for i, p in enumerate(ev["top_error_patterns"], 1):
        ex = p["example"]
        lines.append(f"  {i}. x{p['count']} \"{p['pattern']}\" sources={p['sources']} "
                     f"first={p['first_seen']} last={p['last_seen']} [log:{ex['id']}]")
    if ev["search_terms"]:
        kw = ev["keyword_matches"]
        lines.append("")
        lines.append(f"Logs matching {ev['search_terms']}: {kw['total']} total")
        for i, p in enumerate(kw["patterns"], 1):
            ex = p["example"]
            lines.append(f"  {i}. x{p['count']} \"{p['pattern']}\" [log:{ex['id']}]")
    if ev["partial_failures"]:
        lines.append("")
        lines.append(f"Evidence gaps (sub-queries that failed): {ev['partial_failures']}")
    return "\n".join(lines)
