"""Prometheus metrics for the queue service (build-plan §3.1).

Multiprocess-aware: Gunicorn runs several worker processes, plus the admitter runs as its own
process, and each writes to PROMETHEUS_MULTIPROC_DIR; the /metrics endpoint aggregates them
(CLAUDE.md §8). The metric objects are defined once here, with no explicit registry — which is what
multiprocess mode requires — and imported wherever they are incremented.

Which process writes which:
  - QUEUE_DEPTH / SSE_CONNECTIONS / POSITION_DRIFT — the web workers (the subscriber and the SSE
    view live there); their gauges are cleaned up on worker exit via gunicorn.conf.py.
  - ADMISSIONS / ADMISSION_BATCH_SECONDS — the admitter process; a Counter and a Histogram survive
    that process ending, which is exactly why multiprocess counters are file-backed.
"""

from prometheus_client import Counter, Gauge, Histogram

# Waiters currently in an event's queue (ZCARD), refreshed on every admission broadcast. `livemax`
# so the aggregate is the largest live value across processes, not a sum of stale per-worker copies.
QUEUE_DEPTH = Gauge(
    "qf_queue_depth", "Waiters currently in the queue", ["event"], multiprocess_mode="livemax"
)

# Open SSE connections held by THIS process; `livesum` across workers = total connections.
SSE_CONNECTIONS = Gauge("qf_sse_connections", "Open SSE connections", multiprocess_mode="livesum")

# Admission passes issued.
ADMISSIONS = Counter("qf_admissions_total", "Admission passes issued", ["event"])

# Seconds to run one admit_batch Lua call (the atomic rate-limit + pop).
ADMISSION_BATCH_SECONDS = Histogram("qf_admission_batch_seconds", "Seconds per admission batch")

# Reconciliations that actually moved a position. Should stay near zero; a rising value means
# abandonment is higher than design.md §6 assumes, or a bug (build-plan §3.1).
POSITION_DRIFT = Counter("qf_position_drift_total", "Position reconciliations that corrected drift")
