"""
Generates synthetic telemetry logs and posts them to the ingest API in batches.
Useful for populating the platform with realistic data to test search & charts.

Usage:
    python ingest_simulator.py --count 5000 --api http://localhost:8000 [--api-key KEY]
"""
import argparse
import os
import random
from datetime import datetime, timedelta, timezone

from ingest_client import IngestClient

SOURCES = ["auth-service", "payments-api", "order-worker", "search-index", "gateway", "notifications"]
LEVELS = ["INFO", "INFO", "INFO", "WARN", "ERROR", "DEBUG"]
MESSAGES = [
    "Request completed successfully",
    "Connection timeout to upstream service",
    "Retrying failed request, attempt {n}",
    "User authentication succeeded",
    "User authentication failed: invalid token",
    "Database query took {n}ms",
    "Cache miss for key {n}",
    "Rate limit exceeded for client",
    "Health check passed",
    "Unhandled exception in request handler",
    "Message published to queue",
    "Worker picked up job {n}",
]


def generate_log(base_time: datetime, offset_seconds: int) -> dict:
    ts = base_time + timedelta(seconds=offset_seconds)
    msg_template = random.choice(MESSAGES)
    message = msg_template.format(n=random.randint(1, 9999)) if "{n}" in msg_template else msg_template
    return {
        "timestamp": ts.isoformat(),
        "source": random.choice(SOURCES),
        "host": f"host-{random.randint(1, 20)}",
        "level": random.choice(LEVELS),
        "message": message,
        "tags": {"region": random.choice(["us-east", "us-west", "eu-central"])},
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=2000, help="Number of log entries to generate")
    parser.add_argument("--api", type=str, default="http://localhost:8000", help="Base API URL")
    parser.add_argument("--api-key", default=os.environ.get("API_KEY"), help="API key (default: $API_KEY)")
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--hours-span", type=int, default=24, help="Spread logs over this many past hours")
    args = parser.parse_args()

    client = IngestClient(args.api, api_key=args.api_key)
    # Stored timestamps are naive UTC.
    base_time = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=args.hours_span)
    total_seconds = args.hours_span * 3600

    sent = 0
    for start in range(0, args.count, args.batch_size):
        n = min(args.batch_size, args.count - start)
        batch = [generate_log(base_time, random.randint(0, total_seconds)) for _ in range(n)]
        sent += client.post_batch(batch)
        print(f"Ingested {sent}/{args.count}")

    print("Done.")


if __name__ == "__main__":
    main()
