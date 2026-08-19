"""RateStore backed by Redis — reads and writes `rate_per_min` in the event's config hash.

The only place the backpressure controller touches Redis. Because it is a RateStore (get + set),
core/backpressure.py stays ignorant of redis-py, exactly as core/admission.py is of it.
"""

from adapters.redis_client import get_redis
from core.keys import EventKeys


class RedisRateStore:
    """Implements the RateStore Protocol against the live config hash (qf:{event}:config)."""

    async def get_rate(self, event_id: str) -> int | None:
        """`rate_per_min` from the event's config hash, or None if the event has no config.

        A missing field returns None, which is our "event does not exist" signal — the same key
        admit_batch.lua's EXISTS check guards on. A present-but-unparseable value would be a config
        we did not write; we let int() raise rather than invent a rate for corrupted state.
        """
        raw = await get_redis().hget(EventKeys(event_id).config, "rate_per_min")
        return None if raw is None else int(raw)

    async def set_rate(self, event_id: str, rate_per_min: int) -> None:
        """Write the rate the controller decided on. admit_batch.lua re-reads config every tick,
        so this takes effect on the next admission tick with no restart — the FR-13 live knob."""
        await get_redis().hset(EventKeys(event_id).config, "rate_per_min", rate_per_min)
