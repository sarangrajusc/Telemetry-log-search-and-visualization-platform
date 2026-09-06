"""
Deletes logs older than a configurable retention window.
Can be called via the admin API endpoint, or run standalone (e.g. as a daily cron job):

    python retention.py --days 30
"""
import argparse
from datetime import datetime, timedelta
from database import get_conn


def purge_old_logs(days: int) -> int:
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM logs WHERE timestamp < ?", (cutoff,))
        return cur.rowcount


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30, help="Delete logs older than this many days")
    args = parser.parse_args()
    deleted = purge_old_logs(args.days)
    print(f"Deleted {deleted} logs older than {args.days} days")
