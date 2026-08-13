"""Per-process SSE fan-out: ONE Redis subscriber, many in-memory queues.

This class is the answer to the #1 mistake in CLAUDE.md §8 — "one Redis pub/sub subscription per
connection will kill us." There is exactly ONE subscriber task per worker process. It PSUBSCRIBEs
to every event's announcement channel once, and copies each admission announcement into every
connected client's in-memory asyncio.Queue. A client's SSE generator reads only from its own
queue and never touches Redis per tick (design.md §6).
"""

import asyncio
import json
import logging

from django.conf import settings
from redis import asyncio as aioredis
from redis.exceptions import RedisError

from adapters.metrics import QUEUE_DEPTH
from core.keys import NAMESPACE

logger = logging.getLogger(__name__)

# Bounded, so one slow browser cannot grow this process's memory without limit. On overflow the
# OLDEST frame is dropped: every frame carries absolute state, so the newest is always the whole
# truth and a skipped intermediate position is never missed.
QUEUE_MAXSIZE = 16


class Broadcaster:
    """Fans admission announcements out to this process's connected SSE clients."""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, object]]]] = {}
        self._task: asyncio.Task[None] | None = None

    def register(self, event_id: str, inbox: asyncio.Queue[dict[str, object]]) -> None:
        """Attach one SSE connection's queue, and make sure the subscriber task is running."""
        self._subscribers.setdefault(event_id, set()).add(inbox)
        if self._task is None or self._task.done():
            # Lazy start on the first connection, matching get_redis / get_queue_repository. The
            # task binds to the worker's own event loop — the loop this first request runs on,
            # which is the only loop the Redis client's pool may be used from (redis_client.py).
            self._task = asyncio.create_task(self._run())

    def unregister(self, event_id: str, inbox: asyncio.Queue[dict[str, object]]) -> None:
        """Detach a connection's queue when its SSE generator ends (or the browser disconnects)."""
        subscribers = self._subscribers.get(event_id)
        if subscribers is not None:
            subscribers.discard(inbox)
            if not subscribers:
                del self._subscribers[event_id]

    def connection_count(self) -> int:
        """Total connected clients across all events — for tests and metrics."""
        return sum(len(subscribers) for subscribers in self._subscribers.values())

    async def _run(self) -> None:
        """The single subscriber: PSUBSCRIBE once, fan every message out. Reconnects on Redis loss."""
        pattern = f"{NAMESPACE}:*:events"
        while True:
            # A DEDICATED connection with NO socket read timeout. A subscriber idles waiting for
            # messages, so the shared command client's 2s socket_timeout would fire on every quiet
            # interval and churn the subscription. A subscribed connection also cannot serve normal
            # commands, so it must be its own client anyway (decisions.md, Phase 10 note).
            client = aioredis.Redis.from_url(
                settings.REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=settings.REDIS_CONNECT_TIMEOUT_SECONDS,
            )
            pubsub = client.pubsub()
            try:
                await pubsub.psubscribe(pattern)
                async for message in pubsub.listen():
                    if message.get("type") == "pmessage":
                        self._dispatch(message["channel"], message["data"])
            except asyncio.CancelledError:
                await pubsub.aclose()
                await client.aclose()
                raise
            except RedisError:
                logger.warning("SSE subscriber lost Redis; reconnecting", exc_info=True)
            await pubsub.aclose()
            await client.aclose()
            await asyncio.sleep(1)  # brief backoff before re-subscribing

    def _dispatch(self, channel: str, data: str) -> None:
        # channel is "qf:<event_id>:events"; event_id is a slug and cannot contain a colon.
        event_id = channel.split(":", 2)[1]
        try:
            payload = json.loads(data)
        except (ValueError, TypeError):
            return
        try:
            QUEUE_DEPTH.labels(event=event_id).set(int(payload["total_waiting"]))
        except (KeyError, ValueError, TypeError):
            pass
        for inbox in self._subscribers.get(event_id, set()):
            try:
                inbox.put_nowait(payload)
            except asyncio.QueueFull:
                # Drop the oldest, keep the newest — absolute-state frames make this lossless in
                # effect: a client that missed an intermediate position still gets the current one.
                try:
                    inbox.get_nowait()
                    inbox.put_nowait(payload)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass


_broadcaster: Broadcaster | None = None


def get_broadcaster() -> Broadcaster:
    """This process's single Broadcaster — one per process, like the Redis client."""
    global _broadcaster
    if _broadcaster is None:
        _broadcaster = Broadcaster()
    return _broadcaster


def reset_broadcaster() -> None:
    """Drop the cached broadcaster. For tests, which rebuild across event loops."""
    global _broadcaster
    _broadcaster = None
