import argparse
from datetime import datetime, timedelta
import storage
from database import get_conn


def purge_old_logs(days: int) -> int:
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    backend = storage.get_backend()
    if hasattr(backend, "purge_older_than"):  # e.g. Elasticsearch delete_by_query
        return backend.purge_older_than(cutoff)
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM logs WHERE timestamp < ?", (cutoff,))
        return cur.rowcount


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()
    deleted = purge_old_logs(args.days)
    print(f"Deleted {deleted} logs older than {args.days} days")
