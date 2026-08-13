"""What a waiter is told, and the arithmetic behind it. Pure — no IO, no Redis, no Django.

This module is why `core/` exists. Every number a waiter sees is computed here from plain
integers, which is what makes it testable without a running Redis and what will let the SSE
fan-out (Phase 10) recompute positions in memory rather than asking Redis per client.
"""

import math
from dataclasses import asdict, dataclass
from enum import StrEnum


class WaiterState(StrEnum):
    """The `state` field of QueueState (build-plan §2)."""

    WAITING = "waiting"
    ADMITTED = "admitted"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class JoinOutcome:
    """The raw facts a join returns from Redis, before any presentation.

    Lives in core/, not adapters/, so the arithmetic below can be tested against hand-written
    values with no Redis. adapters/ importing core/ is the allowed direction; the reverse is not
    (CLAUDE.md §5, enforced by tests/test_invariants.py).
    """

    sequence: int
    joined: bool  # True on first placement, False when resuming an existing place
    total_waiting: int
    admitted_total: int
    rate_per_min: int


@dataclass(frozen=True)
class PositionOutcome:
    """The raw facts a position lookup returns from Redis.

    `position` here is AUTHORITATIVE — it came from ZRANK, which counts the members actually
    ahead of you — as opposed to `position_from()` below, which is the cheap arithmetic the SSE
    path will use and which can drift. Keeping them distinct types stops the two from being
    confused at the point where Phase 11 reconciles one against the other.
    """

    position: int
    total_waiting: int
    admitted_total: int
    rate_per_min: int


@dataclass(frozen=True)
class AdmittedOutcome:
    """The raw facts for a waiter who has already been admitted and whose pass is waiting.

    `admission` is the opaque payload the queue service parked at admission time — `pass`,
    `expires_at`, `book_url`. It is carried through verbatim rather than re-parsed: this service
    signed it, the booking service verifies it, and nothing in between has any business
    interpreting its contents.
    """

    admission: dict[str, object]
    total_waiting: int
    admitted_total: int


@dataclass(frozen=True)
class QueueState:
    """What a waiter is told — in the join response and in every SSE `position` frame."""

    position: int
    total_waiting: int
    admitted_total: int
    eta_seconds: int | None
    state: WaiterState

    def as_dict(self) -> dict[str, object]:
        """The wire shape (build-plan §2). StrEnum serialises as its value."""
        return asdict(self)


def position_from(sequence: int, admitted_total: int) -> int:
    """How many people are ahead of you, plus you: `my_seq - admitted`.

    This is design.md §6's whole trick — position is arithmetic on two integers we already have,
    not a ZRANK per waiter, which is what keeps Redis cost flat as connections grow.

    It is only correct because sequences are dense (join.lua's invariant 2) and `admitted` counts
    exactly the people popped from the front. Clamped at 1: position 0 or negative would mean
    more people were admitted than were ever sequenced, which is not a number to show a user.
    """
    return max(1, sequence - admitted_total)


def clamp_position(computed: int, last_shown: int) -> int:
    """Never let a DISPLAYED position increase (FR-7 / fairness promise F4).

    The arithmetic position only moves down as `admitted` grows, but a reconciliation against the
    authoritative ZRANK could momentarily compute a higher number (an abandonment counted late, a
    read straddling an admission batch). A position going UP is the one thing users notice and
    never forgive, so the displayed value is pinned to its previous low even when the correction
    disagrees. The corrected sequence is still kept internally — only what we SHOW is clamped.
    """
    return min(computed, last_shown)


def eta_seconds_for(position: int, rate_per_min: int) -> int | None:
    """Roughly how long until this waiter is admitted, in seconds. None when unknowable.

    None — not 0, and not infinity — when the rate is 0, because the drop is paused and there is
    no honest estimate to give. A 0 would render as "any moment now", which is the opposite of
    the truth.

    Always an ESTIMATE. The rate changes on purpose (FR-13), so the UI must render this hedged
    ("about 20 minutes") and never as a countdown (product-spec rule 21).
    """
    if rate_per_min <= 0:
        return None
    return math.ceil(position / rate_per_min * 60)


def state_from(outcome: JoinOutcome) -> QueueState:
    """Turn the raw facts of a join into what the waiter is shown."""
    position = position_from(outcome.sequence, outcome.admitted_total)
    return QueueState(
        position=position,
        total_waiting=outcome.total_waiting,
        admitted_total=outcome.admitted_total,
        eta_seconds=eta_seconds_for(position, outcome.rate_per_min),
        state=WaiterState.WAITING,
    )


def state_from_position(outcome: PositionOutcome) -> QueueState:
    """Same shape as state_from(), but the position is the authoritative ZRANK, used as given.

    Two builders rather than one with a flag: the difference between "computed" and "measured"
    position is the entire subject of design.md §6, and a boolean parameter would hide it at
    exactly the call sites where it needs to be legible.
    """
    return QueueState(
        position=outcome.position,
        total_waiting=outcome.total_waiting,
        admitted_total=outcome.admitted_total,
        eta_seconds=eta_seconds_for(outcome.position, outcome.rate_per_min),
        state=WaiterState.WAITING,
    )


def state_from_admission(outcome: AdmittedOutcome) -> QueueState:
    """What an already-admitted waiter is told about the queue they have just left.

    `position` is 0 and `eta_seconds` is None, and both are deliberate: there is no position to
    hold when you are no longer in the sorted set, and no waiting left to estimate. This is the
    one place `position` is allowed below 1 — `position_from()` clamps precisely because a
    sub-1 position anywhere else means the arithmetic went wrong.
    """
    return QueueState(
        position=0,
        total_waiting=outcome.total_waiting,
        admitted_total=outcome.admitted_total,
        eta_seconds=None,
        state=WaiterState.ADMITTED,
    )
