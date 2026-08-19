"""Phase v2 — the join abuse gate, against a real Redis.

Two properties, tested end to end through POST /join:

  * the rate limit actually bounds one client and isolates clients from each other (rate_limit.lua);
  * the fairness guarantee the whole feature is named for — reconnecting or manufacturing identities
    can never IMPROVE your position — which is enforced by join.lua, so it is tested with the limiter
    OFF to show it holds on its own.

The limiter is off by default (settings.py), so these classes turn it on explicitly with
override_settings, and reset the rate-limit state they create — a per-IP counter is shared Redis
state exactly like the queue keys, so a test that mutated it and did not clean up would leak into
the next one.
"""

import socket
import unittest
from urllib.parse import urlparse

from django.conf import settings
from django.test import override_settings
from django.test.client import AsyncClient
from redis.asyncio import Redis

from adapters.queue_repository import reset_queue_repository
from adapters.rate_limiter import reset_rate_limiter
from adapters.redis_client import close_redis, get_redis
from core.keys import EventKeys
from core.validation import new_queue_token

EVENT_ID = "test-ratelimit-event"
KEYS = EventKeys(EVENT_ID)


def _redis_is_listening() -> bool:
    url = urlparse(settings.REDIS_URL)
    try:
        with socket.create_connection((url.hostname or "127.0.0.1", url.port or 6379), timeout=0.5):
            return True
    except OSError:
        return False


REDIS_UP = _redis_is_listening()
SKIP_REASON = f"no Redis listening at {settings.REDIS_URL} — start it to run rate-limit tests"


async def _reset(redis: Redis) -> None:
    """Clear the event AND every join-rate counter for it — the new shared state this feature adds."""
    await redis.delete(KEYS.queue, KEYS.seq, KEYS.admitted, KEYS.bucket, KEYS.config)
    async for key in redis.scan_iter(f"qf:{EVENT_ID}:joinrate:*"):
        await redis.delete(key)
    await redis.hset(KEYS.config, mapping={"rate_per_min": 100, "burst": 20, "batch_max": 50})


class _JoinTestBase(unittest.IsolatedAsyncioTestCase):
    """Shared setup: a fresh event, both process singletons reset, the join URL, and the limit for
    this class applied via override_settings (the class decorator form only works on Django's own
    SimpleTestCase, so we enter the override as a context manager instead)."""

    JOIN_LIMIT = 0  # subclasses set the limit they want to exercise
    TRUST_PROXY = False  # subclasses testing per-client isolation flip this on to use XFF

    async def asyncSetUp(self) -> None:
        self.enterContext(
            override_settings(
                JOIN_RATE_LIMIT=self.JOIN_LIMIT,
                JOIN_RATE_WINDOW_SECONDS=60,
                TRUST_PROXY=self.TRUST_PROXY,
            )
        )
        reset_queue_repository()
        reset_rate_limiter()  # the limiter caches a script bound to the client this test will close
        self.redis = get_redis()
        await _reset(self.redis)
        self.url = f"/api/queue/{EVENT_ID}/join"

    async def asyncTearDown(self) -> None:
        await _reset(self.redis)
        reset_queue_repository()
        reset_rate_limiter()
        await close_redis()


@unittest.skipUnless(REDIS_UP, SKIP_REASON)
class JoinRateLimitTests(_JoinTestBase):
    JOIN_LIMIT = 3

    async def test_requests_within_the_limit_are_allowed(self) -> None:
        client = AsyncClient()
        for i in range(3):
            self.assertEqual((await client.post(self.url)).status_code, 200, f"join {i + 1} of 3")

    async def test_the_request_over_the_limit_is_429_with_retry_after(self) -> None:
        client = AsyncClient()
        for _ in range(3):
            await client.post(self.url)

        blocked = await client.post(self.url)
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked.json(), {"detail": "rate_limited"})
        self.assertIn("Retry-After", blocked)
        self.assertGreater(
            int(blocked["Retry-After"]), 0, "Retry-After must tell the client when to come back"
        )


@unittest.skipUnless(REDIS_UP, SKIP_REASON)
class JoinRateLimitPerClientTests(_JoinTestBase):
    """Per-client isolation. TRUST_PROXY on, so each distinct X-Forwarded-For is a distinct client —
    which also exercises the proxy-trust path (core/clientid.py) end to end through the view."""

    JOIN_LIMIT = 3
    TRUST_PROXY = True

    async def test_each_client_ip_has_its_own_budget(self) -> None:
        """The limit is per client, not global — one abuser must not throttle everyone else. Five
        different client IPs each joining once, under a limit of 3, are all allowed."""
        for i in range(5):
            response = await AsyncClient().post(
                self.url, headers={"X-Forwarded-For": f"10.0.0.{i}"}
            )
            self.assertEqual(response.status_code, 200, f"client 10.0.0.{i} should be allowed")

    async def test_one_ip_over_budget_does_not_block_another(self) -> None:
        abuser = {"X-Forwarded-For": "10.9.9.9"}
        bystander = {"X-Forwarded-For": "10.9.9.10"}
        for _ in range(3):
            await AsyncClient().post(self.url, headers=abuser)

        self.assertEqual((await AsyncClient().post(self.url, headers=abuser)).status_code, 429)
        self.assertEqual(
            (await AsyncClient().post(self.url, headers=bystander)).status_code,
            200,
            "the bystander's budget is untouched by the abuser",
        )


@unittest.skipUnless(REDIS_UP, SKIP_REASON)
class JoinRateLimitDisabledTests(_JoinTestBase):
    JOIN_LIMIT = 0

    async def test_disabled_never_blocks_and_writes_no_state(self) -> None:
        """The default. Many joins from one client all pass, and — the point of the settings guard —
        no rate-limit key is created at all, so a deployment that has not opted in pays nothing."""
        client = AsyncClient()
        for _ in range(10):
            self.assertEqual((await client.post(self.url)).status_code, 200)

        leftover = [key async for key in self.redis.scan_iter(f"qf:{EVENT_ID}:joinrate:*")]
        self.assertEqual(leftover, [], "a disabled limiter must touch no rate-limit state")


@unittest.skipUnless(REDIS_UP, SKIP_REASON)
class ReconnectImmunityTests(_JoinTestBase):
    """The guarantee the feature is named for: reconnecting cannot improve your position. Limiter
    off (JOIN_LIMIT=0) — this property is join.lua's, and must hold on its own."""

    JOIN_LIMIT = 0

    async def test_rejoining_never_changes_your_place(self) -> None:
        """Refresh / SSE reconnect fires join again. It must return the same sequence and position
        every time, even as others pile in behind — never a better (lower) one."""
        me = AsyncClient()
        mine = (await me.post(self.url)).json()

        for _ in range(20):
            await AsyncClient().post(self.url)  # 20 others join behind me

        for _ in range(10):
            again = (await me.post(self.url)).json()
            self.assertEqual(again["sequence"], mine["sequence"], "sequence is fixed at arrival")
            self.assertEqual(again["position"], mine["position"], "position must not move on rejoin")
            self.assertIs(again["joined"], False, "a rejoin creates nothing")

    async def test_manufacturing_identities_cannot_get_you_ahead(self) -> None:
        """The multi-tab abuse: open many fresh identities hoping one lands earlier. Every new token
        gets a strictly LATER sequence, and the original joiner keeps position 1."""
        me = AsyncClient()
        mine = (await me.post(self.url)).json()
        self.assertEqual(mine["position"], 1)

        for _ in range(10):
            other = (await AsyncClient().post(f"{self.url}?t={new_queue_token()}")).json()
            self.assertGreater(
                other["sequence"], mine["sequence"], "a new identity can only ever be behind"
            )

        still_mine = (await me.post(self.url)).json()
        self.assertEqual(still_mine["position"], 1, "manufacturing identities did not move me")
        self.assertEqual(still_mine["sequence"], mine["sequence"])


if __name__ == "__main__":
    unittest.main()
