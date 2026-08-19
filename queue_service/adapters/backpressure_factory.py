"""Wiring: builds a BackpressureController out of the concrete adapters and Django settings.

The composition root for backpressure, mirroring admission_factory.py. It is the one place that
knows the p99 comes from Prometheus and the rate lives in Redis; core/backpressure.py is kept
ignorant of both, which is what lets the controller be tested against fakes.
"""

from django.conf import settings

from adapters.prometheus_latency import PrometheusLatencySource
from adapters.redis_rate_store import RedisRateStore
from core.backpressure import AIMDConfig, BackpressureController


def build_backpressure_controller() -> BackpressureController:
    """The controller this process will run, wired to Prometheus and Redis and the AIMD config."""
    return BackpressureController(
        latency=PrometheusLatencySource(
            base_url=settings.PROMETHEUS_URL,
            query=settings.BACKPRESSURE_P99_QUERY,
            timeout_seconds=settings.BACKPRESSURE_PROMETHEUS_TIMEOUT_SECONDS,
        ),
        rates=RedisRateStore(),
        cfg=AIMDConfig(
            target_p99_seconds=settings.BACKPRESSURE_TARGET_P99_SECONDS,
            rate_min=settings.BACKPRESSURE_RATE_MIN,
            rate_max=settings.BACKPRESSURE_RATE_MAX,
            increase_step=settings.BACKPRESSURE_INCREASE_STEP,
            decrease_factor=settings.BACKPRESSURE_DECREASE_FACTOR,
        ),
    )
