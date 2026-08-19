"""Prometheus instrumentation for the booking service — the p99 the backpressure loop reads.

The queue service's backpressure controller (core/backpressure.py) tunes the admission rate to hold
THIS service's p99 near an SLO. That control loop needs a p99 to read, and until now the booking
service exposed none. This module is that signal: a request-latency Histogram and a /metrics
endpoint for Prometheus to scrape.

TWO MODES, ONE ENDPOINT (this is why /metrics branches on an env var).
    The booking Docker image runs `gunicorn -w 4`, so each worker keeps its own counts and a plain
    /metrics would hand the scraper whichever worker answered — an undercount, and worse, a p99
    that jitters per scrape and would make the controller chase noise. So under gunicorn we run
    prometheus multiprocess mode (PROMETHEUS_MULTIPROC_DIR + the child_exit hook in
    gunicorn.conf.py), exactly as the queue service does since Phase 13, and /metrics aggregates
    every worker's files. On the single-process dev server the env var is unset and we serve the
    default registry directly. Same Histogram definition works for both — prometheus_client routes
    it to the multiprocess files when the env var is present at import.
"""

import os
import time
from collections.abc import Callable

from django.http import HttpRequest, HttpResponse
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Histogram,
    generate_latest,
    multiprocess,
)

# Buckets clustered around the ~100ms mock latency and the 200ms SLO, so histogram_quantile has its
# resolution exactly where the controller makes its decision. Coarser in the tail, where we only
# need to know "much too slow", not precisely how slow.
BOOKING_REQUEST_SECONDS = Histogram(
    "booking_request_seconds",
    "Booking service HTTP request latency in seconds (all requests except /metrics).",
    buckets=(0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.75, 1.0, 2.5, 5.0),
)

_METRICS_PATH = "/metrics"


class BookingMetricsMiddleware:
    """Times every request except the scrape itself and records it into BOOKING_REQUEST_SECONDS.

    Excluding /metrics keeps Prometheus's own scrape out of the latency it is measuring — otherwise
    the fast, frequent scrape would dilute the p99 and the controller would read the booking service
    as healthier than it is.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self._get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.path == _METRICS_PATH:
            return self._get_response(request)
        start = time.perf_counter()
        try:
            return self._get_response(request)
        finally:
            # In a finally so a 4xx/5xx (a sold-out 409, an auth 401) is still timed — an endpoint
            # that only gets slow while erroring must still show up in the p99.
            BOOKING_REQUEST_SECONDS.observe(time.perf_counter() - start)


def metrics_view(request: HttpRequest) -> HttpResponse:
    """Expose the registry for Prometheus. The middleware early-returns on this path, so the scrape
    does not measure itself.

    Under gunicorn (PROMETHEUS_MULTIPROC_DIR set) it aggregates every worker's files; on the dev
    server it serves the default in-process registry.
    """
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        payload = generate_latest(registry)
    else:
        payload = generate_latest()
    return HttpResponse(payload, content_type=CONTENT_TYPE_LATEST)
