"""
Tails a real log file and pushes new lines to the ingest API as they're written.
This replaces synthetic data with a real ingestion path.

Expects log lines roughly in the form:
    2026-09-10T12:00:01 ERROR auth-service Connection timeout to upstream

Falls back to treating the whole line as the message (level=INFO, source="unknown")
if it doesn't match that shape, so it tolerates messy real-world logs.

Usage:
    python file_tailer.py --file /var/log/myapp.log --api http://localhost:8000
"""
import argparse
import re
import time
import requests

LINE_PATTERN = re.compile(
    r"^(?P<timestamp>\S+)\s+(?P<level>INFO|WARN|WARNING|ERROR|DEBUG)\s+(?P<source>\S+)\s+(?P<message>.*)$"
)
VALID_LEVELS = {"INFO", "WARN", "ERROR", "DEBUG"}


def parse_line(line: str) -> dict:
    line = line.rstrip("\n")
    if not line.strip():
        return None

    match = LINE_PATTERN.match(line)
    if match:
        level = match.group("level")
        if level == "WARNING":
            level = "WARN"
        return {
            "timestamp": match.group("timestamp"),
            "level": level,
            "source": match.group("source"),
            "message": match.group("message"),
        }

    # Fallback: unstructured line, still worth capturing
    return {"level": "INFO", "source": "unknown", "message": line}


def tail(file_path: str, api_url: str, batch_size: int, flush_interval: float):
    batch = []
    last_flush = time.time()

    with open(file_path, "r") as f:
        f.seek(0, 2)  # start at end of file, only pick up new lines
        while True:
            line = f.readline()
            if line:
                entry = parse_line(line)
                if entry:
                    batch.append(entry)
            else:
                time.sleep(0.5)

            should_flush = len(batch) >= batch_size or (time.time() - last_flush) > flush_interval
            if batch and should_flush:
                try:
                    resp = requests.post(f"{api_url}/api/logs/ingest/batch", json={"logs": batch}, timeout=5)
                    resp.raise_for_status()
                    print(f"Shipped {len(batch)} log lines")
                except requests.RequestException as e:
                    print(f"Failed to ship batch: {e}")
                batch = []
                last_flush = time.time()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, help="Path to the log file to tail")
    parser.add_argument("--api", default="http://localhost:8000", help="Base API URL")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--flush-interval", type=float, default=5.0, help="Max seconds to hold a partial batch")
    args = parser.parse_args()

    print(f"Tailing {args.file} -> {args.api}")
    tail(args.file, args.api, args.batch_size, args.flush_interval)


if __name__ == "__main__":
    main()
