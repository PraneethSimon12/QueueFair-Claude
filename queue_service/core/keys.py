"""Redis key names for one event. Pure string construction — no IO, no redis import.

Every key in the system is built here, so the namespace exists in exactly one place. An inline
f-string typo would not raise: it would address a different, empty key, and the symptom would be
an event that silently behaves as though nobody had ever joined it.
"""

from dataclasses import dataclass

# Short on purpose. Every key name is stored per-entry in Redis's keyspace and shipped in every
# command; "queuefair" instead of "qf" would cost seven bytes on every one of them for nothing.
NAMESPACE = "qf"


@dataclass(frozen=True)
class EventKeys:
    """The keys holding everything known about one event (design.md §3).

    Frozen because a key set that changes after construction is a bug that would present as
    commands landing on another event's data.
    """

    event_id: str

    @property
    def queue(self) -> str:
        """Sorted set: member = queue token, score = arrival sequence."""
        return f"{NAMESPACE}:{self.event_id}:queue"

    @property
    def seq(self) -> str:
        """Counter: the next arrival sequence. The only source of ordering in the system."""
        return f"{NAMESPACE}:{self.event_id}:seq"

    @property
    def admitted(self) -> str:
        """Counter: total ever admitted. What makes O(1) position arithmetic possible."""
        return f"{NAMESPACE}:{self.event_id}:admitted"

    @property
    def bucket(self) -> str:
        """Hash: token-bucket state (`tokens`, `last_refill_ms`). Written only inside Lua."""
        return f"{NAMESPACE}:{self.event_id}:bucket"

    @property
    def config(self) -> str:
        """Hash: `rate_per_min`, `burst`, `batch_max`.

        In Redis rather than Django settings so an operator can change the admission rate live,
        without a restart (FR-13). Its existence is also what defines "this event exists" — the
        join script's 404 check is `EXISTS` on this key.
        """
        return f"{NAMESPACE}:{self.event_id}:config"

    @property
    def channel(self) -> str:
        """Pub/sub channel for admission announcements. A channel, not a key."""
        return f"{NAMESPACE}:{self.event_id}:events"

    def join_rate(self, client_id: str) -> str:
        """The fixed-window join-rate counter for one client on this event (rate_limit.lua).

        Per-(event, client), not global: the abuse it bounds — flooding *this* drop's queue — is
        per-event, and keying it per-event lets one IP legitimately queue for several events. The
        client_id may be an IPv6 address and so may contain ':'; that is harmless as a leaf value
        (Redis keys are binary-safe) and cannot collide with the fixed prefixes above.
        """
        return f"{NAMESPACE}:{self.event_id}:joinrate:{client_id}"

    def pass_for(self, queue_token: str) -> str:
        """Where an issued admission pass waits to be collected by its holder.

        The one per-WAITER key in the system; everything else in design.md §3 is per-event. It
        exists because v0 delivers passes by polling: once a waiter is popped off the sorted set
        they have no place in the queue, so /position needs somewhere to find what they were
        given. Self-limiting — each key carries the pass's own 60s TTL, so at 100 admissions per
        minute roughly 100 exist at any moment, and they disappear whether or not anyone collects.

        Phase 10's SSE fan-out pushes the pass at the moment of admission and makes collection
        unnecessary, but not this key: it is also what lets a waiter who was mid-reconnect during
        their admission still find their pass.
        """
        return f"{NAMESPACE}:{self.event_id}:pass:{queue_token}"
