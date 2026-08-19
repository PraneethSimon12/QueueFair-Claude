"""The interfaces `core/` depends on, and the vocabulary they speak.

This is Dependency Inversion in the literal sense: `AdmissionController` names what it needs,
`adapters/` satisfies it, and the controller can therefore be exercised with no Redis, no
network and no clock. `decisions.md` (2026-08-02, Phase 6) said this file would not be created
until something in `core/` genuinely needed it, because an interface with one implementation and
one caller is decoration. Phase 8 is that moment: the admission loop is real logic worth testing
in isolation, and the alternative is a test suite that can only run against a live Redis.

Protocols, not ABCs: the adapters never import this module, so there is nothing to subclass from.
Structural typing is what keeps the dependency arrow pointing the right way.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

class Clock(Protocol):
    """Milliseconds since the epoch.

    Injected rather than read from `time.time()` so admission is a pure function of its inputs —
    the same reason admit_batch.lua takes `now_ms` as ARGV instead of calling `redis.call('TIME')`.
    A fake clock turns "60 seconds at 100/min" from a slow, flaky test into an exact assertion.
    """

    def __call__(self) -> int: ...


@dataclass(frozen=True)
class AdmissionBatch:
    """What one atomic call to admit_batch.lua released."""

    admitted_tokens: list[str]
    admitted_total: int  # the event's running total AFTER this batch


@dataclass(frozen=True)
class IssuedPass:
    """One admission pass, minted for one waiter.

    `jwt` is the credential the booking service verifies; `queue_token` is who it belongs to and
    becomes the pass's `sub` claim, and therefore `Booking.user_id` on the other side.
    """

    queue_token: str
    jwt: str
    jti: str
    expires_at: int  # Unix seconds


class QueueRepository(Protocol):
    """Queue state, from the admission controller's point of view. Redis is an implementation
    detail the controller must not be able to see."""

    async def admit_batch(self, event_id: str, now_ms: int) -> AdmissionBatch | None:
        """Atomically consume rate budget and pop that many waiters. None if event is unknown."""
        ...

    async def record_admissions(
        self, event_id: str, passes: Sequence[IssuedPass], ttl_seconds: int
    ) -> None:
        """Make each pass retrievable by its holder until it expires."""
        ...


class PassIssuer(Protocol):
    """Mints admission passes. Synchronous: signing is CPU work, not IO, and pretending
    otherwise would put an await on the hot path that never yields."""

    def issue(self, event_id: str, queue_token: str, now_seconds: int) -> IssuedPass:
        """Sign one pass admitting `queue_token` to `event_id`."""
        ...


class LatencySource(Protocol):
    """Where the backpressure controller reads the booking service's health from.

    The controller must never learn that the number comes from Prometheus, or over HTTP — it asks
    for "the booking p99 in seconds" and gets a float or a None, and that is the whole contract.
    That is what lets `BackpressureController` be tested against a fake that just returns scripted
    numbers, with no Prometheus and no network (`tests/test_backpressure.py`).
    """

    async def booking_p99_seconds(self) -> float | None:
        """The booking service's current p99 request latency in seconds.

        Returns None when the signal is unavailable — the metric store is unreachable, or has no
        data in the window yet. None is a first-class value here, not an error: the control law
        (core/backpressure.py) treats it as "hold, do not probe up blind", so the source returning
        None must mean exactly that and never be conflated with "p99 is 0".
        """
        ...


class RateStore(Protocol):
    """How the backpressure controller reads and writes the admission rate it tunes.

    Read as well as write, on purpose: the controller re-reads the current rate every tick rather
    than remembering what it last wrote. That keeps it stateless (a restart resumes cleanly) and
    lets a human operator's manual `HSET` be picked up rather than stomped — the controller and the
    operator drive the same knob (FR-13).
    """

    async def get_rate(self, event_id: str) -> int | None:
        """The event's current admission rate (`rate_per_min` from its config hash), or None if the
        event has no config — i.e. it does not exist. None lets the loop distinguish "unknown
        event" from "rate is 0" (a paused-but-real drop)."""
        ...

    async def set_rate(self, event_id: str, rate_per_min: int) -> None:
        """Set the event's admission rate. Takes effect on the very next admit_batch.lua tick,
        which re-reads config every time — that live reload is the FR-13 knob this controller now
        drives in place of a human."""
        ...
