"""The dynamic-backpressure control law — how fast to admit, given how the booking service feels.

Pure. No IO, no clock, no Redis, no HTTP. It takes three numbers and returns one: given the
current admission rate and the booking service's observed p99, what should the next rate be?
Everything that touches the outside world — reading the p99 from Prometheus, writing the rate to
the `config` hash — lives in adapters/ and arrives through the ports in ports.py. That boundary is
what lets the entire control law be checked against hand-written integers, the same discipline as
core/state.py (design.md §6) and core/admission.py.

WHY AIMD, AND NOT A PROPORTIONAL OR PID CONTROLLER
    This is the law TCP uses for congestion control, and for the same reason: the two error
    directions do not cost the same. Admitting too fast overloads the very service the queue
    exists to protect — a real outage. Admitting too slow just makes people wait a little longer —
    an annoyance. So we back off HARD the instant we see overload (multiplicative decrease) and
    reclaim capacity CAUTIOUSLY when healthy (additive increase). A proportional controller
    (rate * target / observed) reacts symmetrically and oscillates on noisy p99; a PID controller
    is more capable but its gains cannot be honestly defended in a portfolio project — "why is Kd
    0.3" has no good answer. AIMD has two intuitive knobs and converges to a stable sawtooth around
    the target. See decisions.md (2026-08-13, dynamic backpressure).

WHY A MISSING SIGNAL MEANS HOLD, NEVER PROBE UP
    observed_p99 is None when Prometheus is unreachable or has no data yet. The one thing we must
    not do is read "no signal" as "healthy" and ramp admissions up: that raises load on the booking
    service at exactly the moment we have lost the ability to see whether it is coping. So None
    holds the current rate. It does not decrease either — a monitoring blip is not a booking outage,
    and starving the queue for an unrelated Prometheus hiccup would be its own small failure. Hold
    is the balanced choice; the fail-safe-decrease alternative is recorded in the decision log.
"""

import asyncio
import logging
import math
from dataclasses import dataclass

from core.ports import LatencySource, RateStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AIMDConfig:
    """The two AIMD knobs, plus the bounds and the SLO they operate within.

    Frozen: next_rate is a pure function of (current_rate, p99, config), and a config that mutated
    mid-loop would make its output unreproducible — which is exactly what testing against integers
    is meant to prevent.
    """

    target_p99_seconds: float  # the booking-latency SLO the controller defends
    rate_min: int  # floor: never admit slower than this; queue must still drain (0 = allow pause)
    rate_max: int  # never admit faster than this — the operator's ceiling
    increase_step: int  # additive increase per healthy tick, admissions/min
    decrease_factor: float  # multiplicative decrease on overload, strictly 0 < f < 1


def clamp_rate(rate: int, cfg: AIMDConfig) -> int:
    """Force `rate` into [rate_min, rate_max].

    Every path out of next_rate goes through here, so the bound holds even when an additive or
    multiplicative step would land outside it — the controller can never write a rate the operator
    did not authorise.
    """
    return max(cfg.rate_min, min(cfg.rate_max, rate))


def next_rate(current_rate: int, observed_p99_seconds: float | None, cfg: AIMDConfig) -> int:
    """The next admission rate (admissions/min), from the booking p99. Pure AIMD.

    Responsibility: the control LAW only. It does not read the p99 (a LatencySource does), does not
    write the rate (a RateSetter does), and does not decide when to run (the loop does).

    Preconditions: 0 < cfg.decrease_factor < 1, cfg.rate_min <= cfg.rate_max, cfg.increase_step > 0.

    - observed_p99_seconds is None   -> HOLD:  clamp(current_rate). Never probe up without a signal.
    - observed_p99_seconds >  target -> DECREASE: clamp(floor(current_rate * decrease_factor)).
    - observed_p99_seconds <= target -> INCREASE: clamp(current_rate + increase_step).

    Always returns a value in [rate_min, rate_max].
    """
    if observed_p99_seconds is None:
        return clamp_rate(current_rate, cfg)
    if observed_p99_seconds > cfg.target_p99_seconds:
        # floor, not round: on overload we err toward admitting fewer, never more.
        return clamp_rate(math.floor(current_rate * cfg.decrease_factor), cfg)
    return clamp_rate(current_rate + cfg.increase_step, cfg)


class UnknownEventError(Exception):
    """No queue is configured for this event id — the same failure as core/admission.py's."""


class BackpressureController:
    """Drives one event's admission rate toward the SLO, tick by tick.

    Pure orchestration over the two ports: it reads the p99 (LatencySource) and the current rate
    (RateStore), applies `next_rate`, and writes the result back (RateStore). It knows nothing
    about Prometheus, HTTP or Redis — which is why the loop below is tested against fakes with no
    stack running.

    UNLIKE THE ADMITTER, THIS SHOULD RUN AS A SINGLE INSTANCE.
        The admitter is deliberately leaderless: N of them are safe because the rate check and the
        pop are one atomic Lua step, so Redis serialises them (design.md §7). This controller has
        no such protection. Two instances would read the same rate, one might halve it while the
        other adds a step, and they would fight — a read-modify-write race on `rate_per_min` with
        no atomicity around it. Backpressure is a control loop, and a control loop with two
        controllers oscillates. So: one process. Running several would need a lock or a leader,
        which is exactly the coordination the admitter was designed to avoid — a clean contrast
        worth being able to explain (decisions.md, 2026-08-13).
    """

    def __init__(self, latency: LatencySource, rates: RateStore, cfg: AIMDConfig) -> None:
        self._latency = latency
        self._rates = rates
        self._cfg = cfg

    async def adjust_once(self, event_id: str) -> int:
        """One control step: read current rate + booking p99, compute the next rate, write it.

        Responsibility: sequencing the control step. It does NOT define the control law (next_rate
        does) and does NOT decide the cadence (run_forever / the command does).

        Returns the rate now in force. Writes only when the rate actually changes — a held rate
        (healthy-but-capped, or a missing signal) costs no Redis write and no config churn.
        Raises: UnknownEventError if the event has no config.
        """
        current = await self._rates.get_rate(event_id)
        if current is None:
            raise UnknownEventError(event_id)

        p99 = await self._latency.booking_p99_seconds()
        decided = next_rate(current, p99, self._cfg)

        if decided != current:
            await self._rates.set_rate(event_id, decided)
            logger.info(
                "backpressure adjusted rate",
                extra={"event_id": event_id, "from": current, "to": decided, "p99": p99},
            )
        return decided

    async def run_forever(self, event_id: str, interval_seconds: float) -> None:
        """Adjust on a fixed tick until cancelled.

        The interval must be comfortably longer than the admission tick AND long enough for a rate
        change to show up in the p99 — otherwise the loop reacts to the previous decision's echo
        and oscillates, chasing its own tail rather than the booking service.

        Transient failures (Redis blip, Prometheus 500) are logged and retried, never raised: a
        controller that dies leaves the rate frozen at whatever it last wrote, which may be a value
        it had just halved during a spike. Ticking uselessly is far better than dying mid-backoff.
        """
        logger.info(
            "backpressure started", extra={"event_id": event_id, "interval": interval_seconds}
        )
        while True:
            try:
                await self.adjust_once(event_id)
            except asyncio.CancelledError:
                logger.info("backpressure stopping", extra={"event_id": event_id})
                raise
            except UnknownEventError:
                # Not transient — started for an event that does not exist. Keep ticking (the
                # operator may be about to create it) rather than exiting silently.
                logger.warning("backpressure: unknown event", extra={"event_id": event_id})
            except Exception:
                logger.exception("backpressure tick failed", extra={"event_id": event_id})

            await asyncio.sleep(interval_seconds)
