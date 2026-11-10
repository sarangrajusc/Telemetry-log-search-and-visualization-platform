"""
Tails a log file and ships new lines to the ingest API as they are written.

Expected line shape (anything else is ingested as INFO from source "unknown"):
    2026-09-10T12:00:01 ERROR auth-service Connection timeout to upstream

Robustness (the things that go wrong with real log files):
  * log rotation: when the file is replaced (new inode) or truncated, the tailer drains
    the old file and continues from the start of the new one
  * half-written lines: a line without a trailing newline is left for the next poll
  * outages: batches that fail to ship stay in a bounded buffer and are retried; when the
    buffer is full the oldest lines are dropped and counted, never blocking the app's logging
  * poison lines: if the server rejects a batch as invalid, it is split to isolate and drop
    only the bad line(s)
  * auth: sends X-API-Key from --api-key or the API_KEY environment variable
  * Ctrl-C flushes what is buffered before exiting

Usage:
    python file_tailer.py --file /var/log/myapp.log --api http://localhost:8000 [--api-key KEY]
"""
import argparse
import collections
import os
import re
import sys
import time

from ingest_client import AuthError, IngestClient, RejectedError, TransientError

LINE_PATTERN = re.compile(
    r"^(?P<timestamp>\S+)\s+(?P<level>INFO|WARN|WARNING|ERROR|DEBUG)\s+(?P<source>\S+)\s+(?P<message>.*)$"
)


def parse_line(line: str):
    line = line.rstrip("\r\n")
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
    return {"level": "INFO", "source": "unknown", "message": line}


class FileTailer:
    def __init__(self, path, client, batch_size=50, flush_interval=5.0, max_buffer=10000,
                 from_start=False, retry_cooldown=5.0, clock=time.monotonic, log=print):
        self.path = path
        self.client = client
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.max_buffer = max_buffer
        self.from_start = from_start
        self.retry_cooldown = retry_cooldown
        self._clock = clock
        self._log = log

        self.buffer = collections.deque()
        self.shipped = 0
        self.dropped_overflow = 0
        self.dropped_rejected = 0
        self._fh = None
        self._ino = None
        self._pos = 0
        self._last_flush = clock()
        self._blocked_until = 0.0
        self._first_open = True
        self._missing_seen = False

    # ---- reading ---------------------------------------------------------------

    def _open(self, from_start: bool) -> bool:
        try:
            fh = open(self.path, "rb")
        except FileNotFoundError:
            self._missing_seen = True    # when it appears, it is new: read it from the start
            return False
        st = os.fstat(fh.fileno())
        self._fh, self._ino = fh, st.st_ino
        self._pos = 0 if from_start else st.st_size
        fh.seek(self._pos)
        return True

    def _drain(self) -> int:
        """Read all complete lines currently available from the open handle."""
        n = 0
        while True:
            raw = self._fh.readline()
            if not raw:
                break
            if not raw.endswith(b"\n"):          # partial line: wait for the rest
                self._fh.seek(self._pos)
                break
            self._pos += len(raw)
            entry = parse_line(raw.decode("utf-8", errors="replace"))
            if entry:
                self._enqueue(entry)
                n += 1
        return n

    def _enqueue(self, entry):
        if len(self.buffer) >= self.max_buffer:
            self.buffer.popleft()
            self.dropped_overflow += 1
            if self.dropped_overflow % 1000 == 1:
                self._log(f"buffer full ({self.max_buffer}); dropping oldest lines "
                          f"(dropped so far: {self.dropped_overflow})")
        self.buffer.append(entry)

    def poll(self) -> int:
        """Read new lines, handling rotation and truncation. Returns lines read."""
        if self._fh is None:
            start = True if (self._missing_seen or not self._first_open) else self.from_start
            if not self._open(from_start=start):
                return 0
            self._first_open = False

        read = self._drain()

        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return read                          # rotated away; wait for the new file

        if st.st_ino != self._ino:               # replaced (rotation): old file is drained, switch
            self._log(f"{self.path} rotated; following the new file")
            self._fh.close()
            self._open(from_start=True)
            read += self._drain()
        elif st.st_size < self._pos:             # truncated in place
            self._log(f"{self.path} truncated; restarting from the beginning")
            self._fh.seek(0)
            self._pos = 0
            read += self._drain()
        return read

    # ---- shipping --------------------------------------------------------------

    def _ship(self, batch: list) -> bool:
        """Ship a batch, isolating poison lines. Returns False if the server is unavailable."""
        try:
            self.client.post_batch(batch)
            self.shipped += len(batch)
            return True
        except RejectedError as e:
            if len(batch) == 1:
                self.dropped_rejected += 1
                self._log(f"dropping line rejected by server: {batch[0]['message'][:80]!r} ({e})")
                return True
            mid = len(batch) // 2
            return self._ship(batch[:mid]) and self._ship(batch[mid:])
        except TransientError as e:
            self._log(f"ingest unavailable, will retry: {e}")
            return False

    def flush(self, force: bool = False) -> None:
        now = self._clock()
        if not self.buffer or now < self._blocked_until:
            return
        due = len(self.buffer) >= self.batch_size or (now - self._last_flush) >= self.flush_interval
        if not (due or force):
            return
        while self.buffer:
            batch = [self.buffer.popleft() for _ in range(min(self.batch_size, len(self.buffer)))]
            if not self._ship(batch):
                self.buffer.extendleft(reversed(batch))    # keep order; retry after cooldown
                self._blocked_until = self._clock() + self.retry_cooldown
                break
            if not force and len(self.buffer) < self.batch_size:
                break
        self._last_flush = self._clock()

    def run(self, should_stop=lambda: False, poll_interval: float = 0.5, sleep=time.sleep):
        try:
            while not should_stop():
                got = self.poll()
                self.flush()
                if not got:
                    sleep(poll_interval)
        except KeyboardInterrupt:
            pass
        finally:
            self.flush(force=True)
            if self.buffer:
                self._log(f"exiting with {len(self.buffer)} unshipped lines")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", required=True, help="Path to the log file to tail")
    parser.add_argument("--api", default="http://localhost:8000", help="Base API URL")
    parser.add_argument("--api-key", default=os.environ.get("API_KEY"), help="API key (default: $API_KEY)")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--flush-interval", type=float, default=5.0, help="Max seconds to hold a partial batch")
    parser.add_argument("--max-buffer", type=int, default=10000, help="Max buffered lines while the API is down")
    parser.add_argument("--from-start", action="store_true", help="Ship the file's existing contents too")
    args = parser.parse_args()

    client = IngestClient(args.api, api_key=args.api_key, max_retries=3)
    tailer = FileTailer(args.file, client, batch_size=args.batch_size, flush_interval=args.flush_interval,
                        max_buffer=args.max_buffer, from_start=args.from_start)
    print(f"Tailing {args.file} -> {args.api}" + (" (with API key)" if args.api_key else ""))
    try:
        tailer.run()
    except AuthError as e:
        print(f"Authentication failed: {e}", file=sys.stderr)
        sys.exit(2)
    print(f"Shipped {tailer.shipped} lines "
          f"(dropped: {tailer.dropped_overflow} overflow, {tailer.dropped_rejected} rejected)")


if __name__ == "__main__":
    main()
