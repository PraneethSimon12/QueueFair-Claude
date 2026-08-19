"""The Redis rate limiter — runs rate_limit.lua and returns a plain RateLimitResult.

A separate adapter from RedisQueueRepository on purpose (Single Responsibility): rate limiting is
not queue state, and the join view depends on the two independently. Like the repository it holds a
registered script and so is one instance per process — re-reading and re-hashing the Lua on every
join would be the same waste get_queue_repository() avoids.

The limiter is deliberately generic: it limits *a key*, `limit` per `window_seconds`. The key is
built by the caller (EventKeys.join_rate) so the algorithm here knows nothing about joins — the same
limiter could throttle any endpoint later without change.
"""

from pathlib import Path

from redis.asyncio import Redis
from redis.commands.core import AsyncScript

from core.ratelimit import RateLimitResult

_LUA_DIR = Path(__file__).resolve().parent.parent / "lua"


class RedisRateLimiter:
    """Fixed-window rate limiting in Redis, via rate_limit.lua."""

    def __init__(self, redis: Redis) -> None:
        self._script: AsyncScript = redis.register_script(
            (_LUA_DIR / "rate_limit.lua").read_text(encoding="utf-8")
        )

    async def check(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        """Count one hit against `key` and decide if it is within `limit` per `window_seconds`.

        One atomic Redis round trip (the script is INCR + first-hit EXPIRE together). Raises
        RedisError if Redis is unreachable — deliberately not caught here; the caller decides
        whether an unreachable Redis should fail open or closed (see the join view).
        """
        allowed, count, retry_after = await self._script(
            keys=[key], args=[limit, window_seconds]
        )
        return RateLimitResult(
            allowed=bool(allowed), count=count, retry_after_seconds=retry_after
        )


_rate_limiter: RedisRateLimiter | None = None


def get_rate_limiter() -> RedisRateLimiter:
    """This process's single rate limiter, sharing the process Redis client."""
    global _rate_limiter
    if _rate_limiter is None:
        from adapters.redis_client import get_redis

        _rate_limiter = RedisRateLimiter(get_redis())
    return _rate_limiter


def reset_rate_limiter() -> None:
    """Drop the cached limiter. For tests, which rebuild the client between event loops."""
    global _rate_limiter
    _rate_limiter = None
