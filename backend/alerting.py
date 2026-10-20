"""
Error-rate alerting: checks the error rate over a recent rolling window and
fires a webhook notification if it crosses a threshold.

Design: this is a pull-based check (call it periodically — via the API endpoint
on a schedule, a cron job, or a background scheduler) rather than a push-based
stream processor. That's the right tradeoff for the current scale: no extra
infrastructure (no stream processing framework, no always-on worker), and the
threshold/window are simple enough that a periodic SQL aggregate is accurate.
If ingestion volume grows enough that polling the DB every N seconds becomes
expensive, this is the piece that would move to a streaming aggregation
instead (e.g. counting errors as they're ingested rather than re-querying).
"""
import os
import requests
from storage import recent_level_counts

ERROR_RATE_THRESHOLD = float(os.environ.get("ERROR_RATE_THRESHOLD", "0.1"))  # 10% of logs
ALERT_WINDOW_MINUTES = int(os.environ.get("ALERT_WINDOW_MINUTES", "15"))
ALERT_WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL")  # optional


def check_error_rate(window_minutes: int = None) -> dict:
    window = window_minutes or ALERT_WINDOW_MINUTES
    counts = recent_level_counts(minutes=window)
    total = sum(counts.values())
    errors = counts.get("ERROR", 0)
    rate = (errors / total) if total else 0.0
    triggered = rate >= ERROR_RATE_THRESHOLD and total > 0

    result = {
        "window_minutes": window,
        "total_logs": total,
        "error_count": errors,
        "error_rate": round(rate, 4),
        "threshold": ERROR_RATE_THRESHOLD,
        "triggered": triggered,
    }

    if triggered and ALERT_WEBHOOK_URL:
        _notify(result)

    return result


def _notify(result: dict):
    message = (
        f"Error rate alert: {result['error_rate'] * 100:.1f}% "
        f"({result['error_count']}/{result['total_logs']} logs) "
        f"over the last {result['window_minutes']} minutes "
        f"(threshold: {result['threshold'] * 100:.0f}%)"
    )
    try:
        requests.post(ALERT_WEBHOOK_URL, json={"text": message}, timeout=5)
    except requests.RequestException as e:
        print(f"Failed to send alert webhook: {e}")
