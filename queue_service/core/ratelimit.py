"""The result of one rate-limit check. A tiny value type in core/ so the view and the tests speak
the same vocabulary the Redis adapter returns, without either importing redis-py.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class RateLimitResult:
    """One decision from rate_limit.lua.

    Frozen because a decision that changed after it was made is a bug that would present as a client
    being told 'allowed' and then throttled anyway.
    """

    allowed: bool
    count: int  # hits so far in this window, including the one just counted
    retry_after_seconds: int  # seconds until the window resets; 0 when allowed
