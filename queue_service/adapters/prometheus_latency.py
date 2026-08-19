"""LatencySource backed by Prometheus's HTTP query API.

The backpressure controller asks "what is the booking p99?"; this answers it by running a PromQL
`histogram_quantile` against the booking histogram and returning the scalar. Every failure mode —
Prometheus down, a malformed reply, an empty result, a NaN quantile — collapses to None, because
the control law reads None as "hold, do not probe up blind" and that is the safe reading of "I
could not tell how the booking service is doing".

WHY urllib IN A THREAD, AND NOT httpx/aiohttp (Rule 5)
    The control loop makes one GET every ~10s. A whole async HTTP dependency to avoid one blocking
    call that has nothing to block is a bad trade. urllib is stdlib; wrapping it in
    asyncio.to_thread keeps the event loop unblocked (CLAUDE.md §4) without adding anything to
    requirements. The pure parsing is split out as parse_p99_seconds so the fragile part — NaN and
    empty-result handling — is unit-testable with no network.
"""

import asyncio
import json
import math
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def parse_p99_seconds(payload: dict[str, Any]) -> float | None:
    """Pull the scalar out of a Prometheus /api/v1/query vector reply. None if there is no usable
    number — a failed query, an empty result set, or a NaN quantile (no samples in the window).

    Prometheus encodes a NaN as the literal string "NaN"; float("NaN") is a real float and truthy,
    so it would sail through as a plausible latency unless caught explicitly. That guard is the
    whole reason this function exists separately from the IO.
    """
    if payload.get("status") != "success":
        return None
    result = payload.get("data", {}).get("result", [])
    if not result:
        return None
    value_pair = result[0].get("value")  # [ <timestamp>, "<value>" ]
    if not value_pair or len(value_pair) < 2:
        return None
    try:
        value = float(value_pair[1])
    except (TypeError, ValueError):
        return None
    return None if math.isnan(value) else value


class PrometheusLatencySource:
    """Implements the LatencySource Protocol by querying Prometheus over HTTP."""

    def __init__(self, base_url: str, query: str, timeout_seconds: float) -> None:
        self._base_url = base_url.rstrip("/")
        self._query = query
        self._timeout = timeout_seconds

    async def booking_p99_seconds(self) -> float | None:
        """Query Prometheus for the booking p99. None on any failure — the loop treats that as
        HOLD, so an unreachable Prometheus can never cause the controller to admit faster."""
        try:
            payload = await asyncio.to_thread(self._query_prometheus)
        except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
            # URLError covers HTTPError (a 4xx/5xx from Prometheus) and connection failures; OSError
            # covers the socket timeout; JSONDecodeError a truncated body. All mean "no signal".
            return None
        return parse_p99_seconds(payload)

    def _query_prometheus(self) -> dict[str, Any]:
        """Blocking GET + JSON decode. Runs in a worker thread, never on the event loop."""
        url = f"{self._base_url}/api/v1/query?{urllib.parse.urlencode({'query': self._query})}"
        # URL is built from settings + a fixed query, not user input — S310 (SSRF) does not apply.
        with urllib.request.urlopen(url, timeout=self._timeout) as response:  # noqa: S310
            return json.load(response)
