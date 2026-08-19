-- rate_limit.lua — an atomic fixed-window counter, one key per client per window.
--
-- INVARIANT THIS SCRIPT PROTECTS
--   At most `limit` hits are allowed per `window_seconds` for KEYS[1], AND the key always carries a
--   TTL so it self-expires. No window key can leak without an expiry and throttle a client forever.
--
-- WHY IT IS A SCRIPT — the same lesson as admit_batch.lua, in miniature
--   The operation is INCR, and on the FIRST hit of a window, EXPIRE. Those two must be one atomic
--   step. Done as two round trips, a crash or a preemption between them leaves a key at count >= 1
--   with NO expiry: the counter never resets and that client is throttled forever. Redis has no
--   single "increment, and set the TTL only if the key is new" primitive, so the atomicity has to
--   come from the script. "We used Lua because INCR isn't atomic" is the wrong answer — INCR is
--   atomic; it is INCR-*and*-EXPIRE-together that is not, and that is exactly what bites here.
--
-- WHY INCR EVEN WHEN ALREADY OVER THE LIMIT
--   A client hammering while blocked keeps incrementing the counter past `limit`. That is fine: the
--   key still expires at the window's end, so the count is bounded in time, and never
--   re-EXPIRE-ing means a flood cannot keep pushing the reset further out (which would let an
--   attacker extend their own penalty box indefinitely — harmless here, but sliding the TTL on
--   every hit is a classic self-inflicted bug worth NOT writing).
--
-- KEYS[1] the window counter key (caller builds it: qf:{event}:joinrate:{client})
-- ARGV[1] limit            max hits allowed per window
-- ARGV[2] window_seconds   the fixed window length
--
-- RETURNS {allowed, count, retry_after}
--   allowed     = 1 if this hit is within the limit, else 0
--   count       = hits so far in this window, including this one
--   retry_after = seconds until the window resets (the key's TTL); 0 when allowed

local count = redis.call('INCR', KEYS[1])
if count == 1 then
    -- First hit of a new window: stamp the TTL now, in the same atomic step as the INCR that
    -- created the key. This branch is the whole reason the script exists.
    redis.call('EXPIRE', KEYS[1], ARGV[2])
end

local limit = tonumber(ARGV[1])
if count > limit then
    local ttl = redis.call('TTL', KEYS[1])
    -- TTL returns -1 (no expiry) or -2 (no key). Neither should happen inside this atomic script
    -- once the key exists with an expiry, but if it ever did we must not hand back a negative
    -- Retry-After — fall back to a full window.
    if ttl < 0 then
        ttl = tonumber(ARGV[2])
    end
    return {0, count, ttl}
end

return {1, count, 0}
