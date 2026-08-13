"""Phase 10 fan-out: ONE subscriber per process, many in-memory queues, publish on admission.

These pin the property CLAUDE.md §8 cares about — a single Redis subscription fanning out to every
connected client — rather than the easy-but-fatal one-subscription-per-connection. Needs a real
Redis, so they skip when it is not up, matching the other integration tests.
"""

import asyncio
import json
import socket
import unittest

from django.conf import settings

from adapters.broadcaster import get_broadcaster, reset_broadcaster
from adapters.queue_repository import RedisQueueRepository
from adapters.redis_client import close_redis, get_redis
from core.keys import EventKeys
from core.ports import IssuedPass

EVENT = "test-fanout-event"


def _redis_is_listening() -> bool:
    try:
        host_port = settings.REDIS_URL.split("//", 1)[1].split("/", 1)[0]
        host, _, port = host_port.partition(":")
        with socket.create_connection((host, int(port or 6379)), timeout=0.5):
            return True
    except OSError:
        return False


REDIS_UP = _redis_is_listening()
SKIP = "requires a running Redis"


@unittest.skipUnless(REDIS_UP, SKIP)
class BroadcasterFanOutTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        reset_broadcaster()
        self.redis = get_redis()

    async def asyncTearDown(self) -> None:
        await self.redis.delete(EventKeys(EVENT).pass_for("tok"))
        broadcaster = get_broadcaster()
        if broadcaster._task is not None:
            broadcaster._task.cancel()
            try:
                await broadcaster._task
            except asyncio.CancelledError:
                pass
        reset_broadcaster()
        await close_redis()

    async def test_one_publish_reaches_every_inbox_through_a_single_subscriber(self) -> None:
        broadcaster = get_broadcaster()
        inboxes: list[asyncio.Queue[dict[str, object]]] = [asyncio.Queue() for _ in range(5)]
        for inbox in inboxes:
            broadcaster.register(EVENT, inbox)

        # THE property: five connections, exactly one subscriber task (not five subscriptions).
        self.assertIsNotNone(broadcaster._task)
        self.assertEqual(broadcaster.connection_count(), 5)
        await asyncio.sleep(0.3)  # let the lazy subscriber PSUBSCRIBE before we publish

        payload = {"admitted_total": 42, "total_waiting": 7, "rate_per_min": 100}
        await self.redis.publish(EventKeys(EVENT).channel, json.dumps(payload))

        for inbox in inboxes:  # one publish, delivered to all five
            self.assertEqual(await asyncio.wait_for(inbox.get(), timeout=2), payload)

    async def test_unregister_frees_the_slot(self) -> None:
        broadcaster = get_broadcaster()
        inbox: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        broadcaster.register(EVENT, inbox)
        self.assertEqual(broadcaster.connection_count(), 1)
        broadcaster.unregister(EVENT, inbox)
        self.assertEqual(broadcaster.connection_count(), 0)

    async def test_record_admissions_publishes_the_new_state(self) -> None:
        # The publish SIDE: recording an admission announces on the channel, so a connected inbox
        # receives it end to end (repository -> Redis PUBLISH -> subscriber -> fan-out -> inbox).
        broadcaster = get_broadcaster()
        inbox: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        broadcaster.register(EVENT, inbox)
        await asyncio.sleep(0.3)

        repo = RedisQueueRepository(self.redis)
        issued = IssuedPass(queue_token="tok", jwt="jwt", jti="jti", expires_at=9_999_999_999)
        await repo.record_admissions(EVENT, [issued], ttl_seconds=60)

        got = await asyncio.wait_for(inbox.get(), timeout=2)
        self.assertIn("admitted_total", got)
        self.assertIn("total_waiting", got)
        self.assertIn("rate_per_min", got)
