"""Sustained concurrent load on the booking service through Caddy, to give Prometheus a real
booking_request_seconds p99 for the backpressure demo. Idempotent replays of one token are fine —
each still runs HS256 verify + a DB lookup and is timed by the middleware.

    python booking_load.py <bearer_token> <duration_s> <concurrency>
"""

import sys
import threading
import time
import urllib.request

URL = "http://localhost:8080/events/coldplay-mumbai-2026/book"
TOKEN = sys.argv[1]
DURATION = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0
CONCURRENCY = int(sys.argv[3]) if len(sys.argv) > 3 else 20

_stop_at = time.time() + DURATION
_count = 0
_lock = threading.Lock()


def worker() -> None:
    global _count
    while time.time() < _stop_at:
        req = urllib.request.Request(
            URL, data=b"", method="POST", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read()
        except Exception:
            pass
        with _lock:
            _count += 1


threads = [threading.Thread(target=worker, daemon=True) for _ in range(CONCURRENCY)]
for t in threads:
    t.start()
for t in threads:
    t.join()
print(f"sent {_count} requests in {DURATION:.0f}s at concurrency {CONCURRENCY} "
      f"= {_count / DURATION:.0f} req/s")
