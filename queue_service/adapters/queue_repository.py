"""The Redis queue repository — every queue mutation goes through here.

The IO edge for queue state. `core/` never imports this; it receives plain integers and returns
plain values, which is what lets the arithmetic be tested with no Redis running.

There is deliberately no `QueueRepository` Protocol yet. Nothing in `core/` depends on this class
today, so an interface would have exactly one implementation and one caller — speculative
generality (Rule 11). It arrives in Phase 8, when the admission controller genuinely needs to be
unit-tested against an in-memory double.
"""

import json
from collections.abc import Sequence
from pathlib import Path

from redis.asyncio import Redis
from redis.commands.core import AsyncScript

from adapters.metrics import ADMISSIONS, ADMISSION_BATCH_SECONDS
from core.keys import EventKeys
from core.ports import AdmissionBatch, IssuedPass
from core.state import AdmittedOutcome, JoinOutcome, PositionOutcome

LUA_DIR = Path(__file__).resolve().parent.parent / "lua"

# Both scripts lead with a status code so the caller can tell the failure modes apart without
# parsing an error string. Shared vocabulary: 0 = the event is not configured.
_STATUS_UNKNOWN_EVENT = 0
# position.lua only: the event exists but this token is not in its queue.
_STATUS_UNKNOWN_TOKEN = 1
# position.lua only: 2 = still waiting, 3 = already admitted with a pass waiting to be collected.
_STATUS_WAITING = 2
_STATUS_ADMITTED = 3


class UnknownEvent(Exception):
    """No queue is configured for this event id (-> 404 unknown_event)."""


class UnknownToken(Exception):
    """The event exists, but this token holds no place in it (-> 404 unknown_token)."""


def _load_lua(filename: str) -> str:
    """Read a Lua script from lua/. Read once, at repository construction, never per call."""
    return (LUA_DIR / filename).read_text(encoding="utf-8")


class RedisQueueRepository:
    """Queue state in Redis. One instance per process (see get_queue_repository)."""

    def __init__(self, redis: Redis) -> None:
        # register_script() hashes the body now and calls EVALSHA later, shipping only the 40-char
        # SHA per invocation instead of the whole script. It also handles NOSCRIPT transparently:
        # if Redis restarts or the script cache is flushed, redis-py reloads the body and retries.
        # That combination is what makes build-plan §5's "never ship the script body per call"
        # true without us hand-rolling the cache-miss path.
        self._redis = redis
        self._join: AsyncScript = redis.register_script(_load_lua("join.lua"))
        self._position: AsyncScript = redis.register_script(_load_lua("position.lua"))
        self._admit_batch: AsyncScript = redis.register_script(_load_lua("admit_batch.lua"))

    async def join(self, event_id: str, queue_token: str) -> JoinOutcome | None:
        """Place `queue_token` in `event_id`'s queue, or return the place it already holds.

        Single responsibility: the one atomic Redis call. Does not mint tokens, does not decide
        what the waiter is shown, does not touch HTTP.

        Idempotent by construction — see the invariant header in lua/join.lua. Costs exactly one
        Redis round trip, which is the budget in build-plan §5.

        Returns: JoinOutcome, or None if the event is not configured (the caller's 404).
        Raises: RedisError if Redis is unreachable — deliberately not caught here, because a
                repository that swallows connection failures reports an empty queue instead of an
                outage.
        """
        keys = EventKeys(event_id)
        reply = await self._join(
            keys=[keys.queue, keys.seq, keys.admitted, keys.config],
            args=[queue_token],
        )

        if reply[0] == _STATUS_UNKNOWN_EVENT:
            return None

        _, sequence, joined, total_waiting, admitted_total, rate_per_min = reply
        return JoinOutcome(
            sequence=sequence,
            joined=bool(joined),
            total_waiting=total_waiting,
            admitted_total=admitted_total,
            rate_per_min=rate_per_min,
        )


    async def position(self, event_id: str, queue_token: str) -> PositionOutcome | AdmittedOutcome:
        """Where `queue_token` stands: still waiting, or already admitted with a pass waiting.

        Single responsibility: one atomic read of this waiter's situation. A waiting position
        comes from ZRANK — it counts the members actually ahead, so unlike the arithmetic in
        design.md §6 it cannot drift when waiters abandon the queue.

        Two return types rather than one with a nullable field, because the caller must branch
        anyway and an admitted waiter has no position to report — conflating them is how a
        rendered "position 0" reaches somebody's screen.

        Costs one Redis round trip. Raises rather than returning a sentinel, unlike join(),
        because two distinct failures must map to two distinct responses.

        Raises:
            UnknownEvent: no config hash for this event.
            UnknownToken: never queued here, or admitted and the pass has since expired.
            RedisError:   Redis unreachable.
        """
        keys = EventKeys(event_id)
        reply = await self._position(
            keys=[keys.queue, keys.admitted, keys.config, keys.pass_for(queue_token)],
            args=[queue_token],
        )

        if reply[0] == _STATUS_UNKNOWN_EVENT:
            raise UnknownEvent(event_id)
        if reply[0] == _STATUS_UNKNOWN_TOKEN:
            raise UnknownToken(queue_token)

        if reply[0] == _STATUS_ADMITTED:
            _, admission_json, total_waiting, admitted_total = reply
            return AdmittedOutcome(
                admission=json.loads(admission_json),
                total_waiting=total_waiting,
                admitted_total=admitted_total,
            )

        _, position, total_waiting, admitted_total, rate_per_min = reply
        return PositionOutcome(
            position=position,
            total_waiting=total_waiting,
            admitted_total=admitted_total,
            rate_per_min=rate_per_min,
        )


    async def admit_batch(self, event_id: str, now_ms: int) -> AdmissionBatch | None:
        """Consume rate budget and pop that many waiters off the front — atomically.

        Single responsibility: the one atomic call. Issues nothing, stores nothing, notifies
        nobody. The rate limiting, the pop and the counter increment happen inside
        lua/admit_batch.lua precisely so no two processes can do them in an interleaved order.

        `now_ms` is supplied by the caller rather than read here, so admission stays a pure
        function of its inputs and a fake clock makes the rate exactly assertable.

        Returns: AdmissionBatch (possibly empty — an empty bucket is ordinary), or None if the
                 event is not configured.
        """
        keys = EventKeys(event_id)
        with ADMISSION_BATCH_SECONDS.time():
            reply = await self._admit_batch(
                keys=[keys.queue, keys.admitted, keys.bucket, keys.config],
                args=[now_ms],
            )

        if reply[0] == _STATUS_UNKNOWN_EVENT:
            return None

        _, count, admitted_total, popped = reply
        # ZPOPMIN replies flat: [member, score, member, score, ...]. We want the members, and the
        # scores are the arrival sequences we already know we no longer need — the waiter has
        # left the queue, so their position is no longer a question anyone will ask.
        admitted_tokens = [popped[i] for i in range(0, len(popped), 2)]
        assert len(admitted_tokens) == count, "script reported a count its pop did not match"

        return AdmissionBatch(admitted_tokens=admitted_tokens, admitted_total=admitted_total)

    async def record_admissions(
        self, event_id: str, passes: Sequence[IssuedPass], ttl_seconds: int
    ) -> None:
        """Park each pass where its holder can collect it, until it expires.

        One pipelined round trip for the whole batch, not one per pass: at batch_max = 50 the
        difference is 50 round trips per tick versus 1, on a loop that runs every second in every
        process.

        The TTL is the pass's own lifetime, so the key cannot outlive the credential it holds.
        There is no cleanup path and there does not need to be one — that is the point of setting
        it here rather than reaping later.

        Also ANNOUNCES the new admitted_total on the event's pub/sub channel (Phase 10). The
        admission path is the only moment a waiter's position can drop, so it is where the SSE
        fan-out is triggered; subscribers recompute every waiter's position in memory from the
        broadcast integers (design.md §6), so this PUBLISH is fire-and-forget.
        """
        if not passes:
            return

        keys = EventKeys(event_id)
        async with self._redis.pipeline(transaction=False) as pipe:
            for issued in passes:
                pipe.set(
                    keys.pass_for(issued.queue_token),
                    json.dumps(
                        {
                            "pass": issued.jwt,
                            "expires_at": issued.expires_at,
                            "book_url": f"/events/{event_id}/book",
                        }
                    ),
                    ex=ttl_seconds,
                )
            # Read the announcement facts in the same round trip as the writes above.
            pipe.get(keys.admitted)
            pipe.zcard(keys.queue)
            pipe.hget(keys.config, "rate_per_min")
            results = await pipe.execute()

        await self._redis.publish(
            keys.channel,
            json.dumps(
                {
                    "admitted_total": int(results[-3] or 0),
                    "total_waiting": int(results[-2] or 0),
                    "rate_per_min": int(results[-1] or 0),
                }
            ),
        )
        ADMISSIONS.labels(event=event_id).inc(len(passes))


_repository: RedisQueueRepository | None = None


def get_queue_repository() -> RedisQueueRepository:
    """This process's single repository.

    One per process for the same reason as the Redis client: the repository holds the registered
    Lua script, and building a new one per request would re-read join.lua from disk and re-hash
    it on every join — on the hottest path in the system.
    """
    global _repository
    if _repository is None:
        from adapters.redis_client import get_redis

        _repository = RedisQueueRepository(get_redis())
    return _repository


def reset_queue_repository() -> None:
    """Drop the cached repository. For tests, which rebuild the client between event loops."""
    global _repository
    _repository = None
