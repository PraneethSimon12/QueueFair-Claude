# QueueFair — Design Doc

> **Why the system is built the way it is.** The RFC. This is the file to hand an interviewer.
>
> - [`product-spec.md`](product-spec.md) — *what* it does, and the fairness contract it promises
> - **`design.md`** — *why* it is built this way — you are here
> - [`build-plan.md`](build-plan.md) — *how* to build it, and the exact wire contract
> - [`decisions.md`](decisions.md) — the append-only decision log. **ADRs live there, not here.**
>   This file links to entries; it never restates them, because two copies of a decision is how
>   a spec goes stale.
> - [`loadtest-report.md`](loadtest-report.md) — every performance number and the run behind it
>
> **`product-spec.md` governs behaviour.** If this file disagrees with it, this file is wrong.

**Status:** booking service built through Step D1 · queue service designed, not started
**Last updated:** 2026-08-02

> ⚠️ **Read the status line literally.** Everything in §5–§9 below is *design*, not code. The
> only shipped parts are §4 (the trust boundary, verify side) and §10 (the booking transaction).
> [`resume-claims.md`](resume-claims.md) tracks exactly what is built versus claimed.

---

## Contents

1. [System shape](#1-system-shape)
2. [Functional requirements](#2-functional-requirements)
3. [The Redis data model](#3-the-redis-data-model)
4. [The trust boundary — the admission pass](#4-the-trust-boundary--the-admission-pass)
5. [Join — and the race that breaks fairness](#5-join--and-the-race-that-breaks-fairness)
6. [Position without a Redis call](#6-position-without-a-redis-call)
7. [Admission — the token bucket that removes leader election](#7-admission--the-token-bucket-that-removes-leader-election)
8. [SSE fan-out](#8-sse-fan-out)
9. [Horizontal scaling](#9-horizontal-scaling)
10. [The booking service](#10-the-booking-service)
11. [Failure modes](#11-failure-modes)
12. [Multi-region](#12-multi-region)
13. [What we intend to prove](#13-what-we-intend-to-prove)

---

## 1. System shape

```
                    ┌──────────────────────────────────────────┐
    60,000 people   │           QUEUE SERVICE                  │
    at 12:00:00 ───►│  async Django (ASGI) · no database       │
                    │                                          │
                    │  join · position · SSE · admit           │
                    └───────┬──────────────────────┬───────────┘
                            │                      │
                            │ all state            │ signs a 60s
                            ▼                      │ admission pass
                    ┌───────────────┐              │ (HS256)
                    │    REDIS 7    │              │
                    │ ZSET, HASH,   │              │  no network call
                    │ Lua, pub/sub  │              │  between services
                    └───────────────┘              │
                                                   ▼
                    ┌──────────────────────────────────────────┐
    ~100/min ──────►│         BOOKING SERVICE                  │
                    │  Django + DRF + PostgreSQL               │
                    │  verifies the pass · books one ticket    │
                    └──────────────────────────────────────────┘
```

Two services, one asymmetry that explains everything: **the queue service is built to hold a
crowd, the booking service is built to process one request correctly.** Holding is cheap per
person and hard at scale; processing is expensive per person and hard to get right. Splitting
them means each can be optimised for the thing it is actually bad at.

**The queue service has no database.** Its `DATABASES` setting is empty and the ORM is never
imported. This is a hard constraint, not an accident: Django's ORM is synchronous, so a single
ORM call from an async view either raises `SynchronousOnlyOperation` or — worse, if someone
"fixes" it with `sync_to_async` — silently moves the request onto a thread from the ASGI pool.
With tens of thousands of open SSE connections that is fatal. Having no `DATABASES` at all makes
the mistake fail at import time instead of under load. See [`decisions.md`](decisions.md),
2026-08-02, and CLAUDE.md §8.

**The two services never call each other.** Not once, on any path. That is the point of §4.

---

## 2. Functional requirements

Numbered so commits and tests can cite them. Traceable to
[`product-spec.md`](product-spec.md) — the FR is the engineering obligation, the product rule is
the promise it keeps.

### Queueing

| ID | Requirement | Keeps | Status |
|---|---|---|---|
| FR-1 | A first-time arrival is assigned a monotonically increasing arrival sequence and placed in the event's queue | rule 1, 2 | ☐ |
| FR-2 | A returning arrival presenting a known queue token resumes its **existing** sequence — never a new one | **F2** | ☐ |
| FR-3 | Concurrent join requests bearing the same queue token resolve to exactly one sequence | **F3** | ☐ |
| FR-4 | Arrival sequences are dense — no gaps — for a given event | prerequisite of FR-6 | ☐ |
| FR-5 | Queues are isolated per event; no operation on one can affect another | rule 4 | ☐ |
| FR-6 | Position is computable in O(1) with **zero** Redis round trips per connected waiter | assumption 9 | ☐ |
| FR-7 | Reported position is monotonically non-increasing for a given waiter | **F4** | ☐ |
| FR-8 | An authoritative position is available on demand and used to reconcile the computed one | correctness of FR-6 | ☐ |

### Admission

| ID | Requirement | Keeps | Status |
|---|---|---|---|
| FR-9 | Admission pops strictly from the front of the queue, lowest sequence first | **F1**, rule 6 | ☐ |
| FR-10 | The admission rate is enforced **globally** across all queue-service processes | rule 8 | ☐ |
| FR-11 | Rate check and queue pop are a **single atomic operation** — no interleaving can over-admit | rule 8 | ☐ |
| FR-12 | Each admitted waiter receives a signed pass carrying user, event, unique id, and a 60s expiry | rule 9 | ☐ |
| FR-13 | The admission rate is changeable at runtime, including to zero, without restart | rule 12 | ☐ |
| FR-14 | No waiter is ever admitted twice for one queue entry | rule 15 | ☐ |

### Transport

| ID | Requirement | Keeps | Status |
|---|---|---|---|
| FR-15 | Position updates are pushed over SSE; the client never polls | §4.2 | ☐ |
| FR-16 | A worker process holds **one** Redis pub/sub subscription regardless of connection count | assumption 9 | ☐ |
| FR-17 | Every pushed message carries absolute state, never a delta, so a dropped message is harmless | FR-7 | ☐ |
| FR-18 | A slow or stalled client cannot block delivery to any other client | — | ☐ |
| FR-19 | Connections receive a heartbeat frequently enough that proxies do not drop them | §8 trap | ☐ |
| FR-20 | A reconnecting client resumes with correct position and correct admission state | **F2**, Journey C | ☐ |

### Trust boundary

| ID | Requirement | Keeps | Status |
|---|---|---|---|
| FR-21 | The booking service verifies a pass with **no** call to the queue service and no shared session store | assumption 10 | ✅ |
| FR-22 | Verification pins the signature algorithm; `alg: none` and algorithm confusion are rejected | — | ✅ |
| FR-23 | A pass valid for one event is rejected by any other | rule 10 | ✅ |
| FR-24 | An expired pass is rejected | rule 11 | ✅ |
| FR-25 | Passes are issued **only** by the queue service, only after admission; no endpoint mints one on demand | §3 | ✅ (enforced by absence) |

### Booking

| ID | Requirement | Keeps | Status |
|---|---|---|---|
| FR-26 | One pass produces at most one booking; a replay returns the original | rule 15 | ✅ |
| FR-27 | One user holds at most one booking per event | rule 16 | ✅ |
| FR-28 | Recorded bookings never exceed capacity, under any concurrency | rule 17 | ✅ |
| FR-29 | Capacity is enforced by a database constraint, not only by application logic | rule 17 | ✅ |
| FR-30 | Sold-out and no-such-event are distinguishable to the client | Journey E | ✅ |

✅ = built and tested today · ☐ = designed, not built

---

## 3. The Redis data model

Five keys per event, all namespaced `qf:{event_id}:`. Nothing else exists — there is no other
store anywhere in the queue service.

| Key | Type | Holds | Why this type |
|---|---|---|---|
| `qf:E:seq` | String (counter) | The next arrival sequence | `INCR` is atomic and monotonic. This is the *only* source of ordering — see below. |
| `qf:E:queue` | **Sorted set** | member = queue token, score = arrival sequence | Ordered by score, `O(log N)` insert, `O(log N)` rank, and `ZPOPMIN` pops the front atomically. A list would give O(1) pop but no rank; a plain set has no order at all. |
| `qf:E:admitted` | String (counter) | Total ever admitted for this event | Makes position arithmetic possible (§6). |
| `qf:E:bucket` | Hash | `tokens`, `last_refill_ms` | Token-bucket state, read and written inside one Lua script (§7). |
| `qf:E:config` | Hash | `rate_per_min`, `burst`, `batch_max` | In Redis rather than Django settings so the operator can change the rate live (FR-13) without a restart. |
| `qf:E:events` | pub/sub channel | admission announcements | Not a key — a channel. One message per admission batch (§8). |

### Ordering comes from a counter, not a clock

`qf:E:seq` is an `INCR` counter, and arrival order is defined by it. It is **not** a timestamp.
Three reasons, in order of severity:

1. **Collisions.** At 60,000 arrivals in 60 seconds, millisecond timestamps collide constantly.
   Two people with the same score have *no defined order* in a sorted set beyond lexicographic
   member comparison — so ordering would silently fall back to comparing random token strings.
   That is not FIFO; it is a coin flip wearing FIFO's clothes.
2. **Clock skew.** With multiple queue-service processes, two clocks disagree by milliseconds
   at best. A person who arrived later at a fast-clocked box would sort ahead of someone who
   arrived earlier elsewhere — breaking **F1** invisibly, with no error anywhere.
3. **Density.** §6's position arithmetic requires sequences with no gaps. A counter gives that
   for free; a clock cannot.

`INCR` is atomic on a single Redis instance, so a single counter per event is a total order over
arrivals — which is exactly what "fair" means here.

---

## 4. The trust boundary — the admission pass

**Built and tested.** `booking_service/bookings/tokens.py`.

The queue service **issues** passes; the booking service **verifies** them; neither calls the
other. A pass is an HS256 JWT:

```json
{ "sub": "<user/queue token>", "event_id": "coldplay-mumbai-2026",
  "jti": "<unique id>", "iat": 1780000000, "exp": 1780000060 }
```

**Why this makes the system fast:** verification is a local HMAC over a few hundred bytes. No
database lookup, no cross-service call, no shared session store on the hot path. A shared session
store would recreate exactly the bottleneck the queue exists to remove.

**Why HS256 and not RS256:** both services are ours and share one secret, so asymmetry buys
nothing; RS256 signing cost shows up in p99 (CLAUDE.md §8). Full entry in
[`decisions.md`](decisions.md), 2026-07-18.

**Why the algorithm is pinned:** without `algorithms=["HS256"]`, a verifier trusts the token's own
`alg` header — and `{"alg":"none"}` with no signature is a total authentication bypass. Covered by
`test_alg_none_is_rejected`.

**Why no endpoint issues a pass:** the pass *is* proof you waited. An ungated "give me a token"
endpoint would let anyone skip the queue and hit the booking service directly, which is not a
missing feature but the total defeat of the product. Dev/test tokens come from a management
command, which needs shell access and has no network surface.
[`decisions.md`](decisions.md), 2026-07-28.

**Each claim earns its place:**

| Claim | Job | What breaks without it |
|---|---|---|
| `jti` | Becomes `Booking.token_jti`, which is `UNIQUE` | Replay creates a second booking (FR-26) |
| `event_id` | Compared to the URL's event | A pass for a cheap event books an expensive one (FR-23) |
| `exp` | 60-second window | Passes accumulate; admitting 100/min for 20 min eventually dumps 2,000 people at once (rule 11) |
| `sub` | Becomes `Booking.user_id`, unique per event | One person books repeatedly with fresh passes (FR-27) |
| `iat` | Diagnostics only | Nothing — it is there for debugging, and that is worth saying rather than pretending it is load-bearing |

---

## 5. Join — and the race that breaks fairness

**Not built.** This is where the project's central lesson lives.

### The naive version, and its broken interleaving

The obvious implementation is check-then-act:

```python
score = await redis.zscore(f"qf:{e}:queue", token)   # 1. am I already queued?
if score is None:
    seq = await redis.incr(f"qf:{e}:seq")            # 2. no — take a number
    await redis.zadd(f"qf:{e}:queue", {token: seq})  # 3. and join
```

Rahul double-taps, or his browser retries, sending two joins with the same token:

| | Request A | Request B | Redis state |
|---|---|---|---|
| t₁ | `ZSCORE` → `nil` | | not queued |
| t₂ | | `ZSCORE` → `nil` | not queued |
| t₃ | `INCR` → **100** | | seq = 100 |
| t₄ | `ZADD {tok: 100}` | | tok at **100** |
| t₅ | | `INCR` → **101** | seq = 101 |
| t₆ | | `ZADD {tok: 101}` | tok at **101** ← overwritten |

Rahul's position just got **worse because he double-tapped**. Both requests read the same state
before either wrote, and plain `ZADD` overwrites an existing score. That is a lost update across
a network round trip — the classic `GET → modify → SET` race (CLAUDE.md §8), and it breaks **F2**
in the direction that makes users angriest.

Worse, it is invisible: no error, no log line, no alert. The only symptom is one user's number
going up, which they will report and nobody will reproduce.

### Fix 1 — `ZADD NX`, and why it is not enough

`ZADD NX` only adds if the member is absent, so t₆ becomes a no-op and Rahul keeps score 100.
**This is genuinely correct for fairness** — and it is worth saying so plainly rather than
inventing a race that is not there.

Two real costs remain:

- **Three round trips per join** (`INCR`, `ZADD NX`, `ZSCORE` to learn the surviving score). On
  the hottest path in the system, at ~60,000 joins in the first minute, that is ~180,000 Redis
  round trips where 60,000 would do.
- **It burns sequence numbers.** Request B's `INCR` to 101 is wasted, so sequences develop gaps.
  Gaps are cosmetically harmless — **but §6's position arithmetic requires density.** With gaps,
  every waiter's computed position is inflated by the number of holes ahead of them, and it drifts
  further every time anyone double-taps.

So the round trips are a performance argument; the gaps are a **correctness** argument, and it is
the gaps that decide it.

### Fix 2 — one Lua script

```lua
-- join.lua
-- INVARIANT: a queue token is assigned an arrival sequence exactly once, ever, and sequences
-- for an event are dense. Both properties are relied on elsewhere: the first is fairness
-- promise F2/F3 (reconnecting never changes your place), the second is what makes O(1)
-- position arithmetic correct (design.md §6).
--
-- Atomic because Redis runs a script to completion before any other command: no other join can
-- observe the state between the ZSCORE and the ZADD.

local existing = redis.call('ZSCORE', KEYS[1], ARGV[1])
if existing then
    return {tonumber(existing), 0}          -- already queued; 0 = we did not join you again
end
local seq = redis.call('INCR', KEYS[2])     -- only INCR when we are actually going to add
redis.call('ZADD', KEYS[1], seq, ARGV[1])
return {seq, 1}
```

One round trip, no gaps, and the check-then-act window is gone because **Redis is single-threaded
and runs a script to completion**: no other command from any client interleaves.

FR-2, FR-3 and FR-4 are all this script.

### Abuse mitigation (v2) — you cannot manufacture a better place

The same script quietly delivers a fairness-under-abuse property: **reconnecting, refreshing, or
opening many tabs can never IMPROVE your position.** `ZADD NX` means a token that is already queued
keeps its original score, so a rejoin returns the exact place it held; and every genuinely new
identity draws the *next* sequence, which is strictly worse. There is no sequence of client actions
that lowers your own number — proven, not asserted, in `tests/test_ratelimit.py::ReconnectImmunity`.

What that does not stop is *volume*: one client hammering `join` to inflate the queue, exhaust
Redis, or fish for slots with a flood of identities. A v2 **per-(event, client) fixed-window rate
limit** (`lua/rate_limit.lua`) bounds it, returning 429 + `Retry-After`. It is config-gated
(off by default, enabled in the deployment) and keys on the client IP — the *rightmost*
X-Forwarded-For hop behind a trusted proxy, because XFF is client-settable and trusting the wrong
end is a free bypass. Its honest limit — a botnet with many IPs, and NAT sharing — and the atomicity
argument for the script are in `decisions.md` (2026-08-13).

---

## 6. Position without a Redis call

**Not built.** This is the design point that makes the per-connection cost story true.

### The obvious approach, and its cost

`ZRANK qf:E:queue <token>` returns the rank directly. It is O(log N) and exactly correct.

The problem is not the operation, it is the multiplier. Position must update for **every waiting
person, every time anyone is admitted**. With 20,000 connected waiters:

- polling `ZRANK` every 5 seconds → **4,000 Redis round trips per second**, all to compute
  numbers that are mostly unchanged
- and every one of them is a network hop from a Python process, so the cost is dominated by
  round-trip latency, not by Redis

Per-connection Redis cost that scales linearly with connection count is precisely the thing that
stops these systems at a few thousand connections.

### The arithmetic

Because the queue is strict FIFO and we only ever pop from the **front**, rank is derivable:

```
position = my_sequence − total_admitted
```

Why it holds: sequences are dense from 1..N (§5), and everyone admitted has a lower sequence than
anyone still waiting. So the people ahead of me are those with sequence < mine, minus those
already admitted:

```
ahead    = (my_seq − 1) − admitted
position = ahead + 1 = my_seq − admitted
```

Check it: the first arrival has `seq = 1`, `admitted = 0` → position 1 ✓. After one admission the
second arrival has `seq = 2`, `admitted = 1` → position 1 ✓.

**`my_sequence` is fixed for the life of the connection** — the process learns it once at join.
**`total_admitted` arrives in the pub/sub message.** So computing a position is one subtraction in
Python, with zero Redis involvement. 20,000 connections cost the same Redis traffic as one:

| | Redis ops/sec at 20K waiters |
|---|---|
| `ZRANK` polled every 5s | ~4,000 |
| This design (1 admission batch/sec) | **~1** |

That is the whole of the "per-connection cost stays flat" claim, and it is arithmetic — which is
why it can be written down now and **still has to be measured before it goes on a resume**
([`loadtest-report.md`](loadtest-report.md)).

### The three preconditions, and what breaks if each is violated

1. **Sequences must be dense.** Guaranteed by `join.lua` (§5). Violated → positions inflate by
   the number of gaps ahead.
2. **Removal must only happen at the front.** Violated → every position behind the removal is
   wrong, permanently.
3. **Counters must never reset.** Violated → positions go negative or leap. Both counters live in
   Redis with no TTL for exactly this reason.

### The cost of precondition 2: abandonment

People who close the tab stay in the queue and are eventually "admitted" into the void. Their
passes are never used, so the *effective* admission rate of real people is lower than configured.

The obvious fix — remove them from the middle on disconnect — is exactly what precondition 2
forbids. `ZREM` of a middle member makes `admitted` no longer equal "number of people ahead who
have left the queue", and every position behind them silently shifts. So abandonment is a real
inefficiency we accept in v1 rather than a bug we forgot.

The escape hatch, if it ever matters: keep a second counter of mid-queue removals *ahead of a
given sequence*, which is not a counter but a ranged sum — i.e. exactly what `ZRANK` already
does. At that point the honest answer is to go back to `ZRANK` and pay for it, and this section
becomes the reason we knew what we were paying for.

### Reconciliation

Arithmetic is the fast path, not the source of truth. `ZRANK` is authoritative and is used:

- once when a connection is established (so a reconnecting client is always correct — FR-20)
- periodically thereafter, on a slow cadence, per connection

Cost stays bounded: at 20,000 connections reconciling every 30 seconds, ~667 `ZRANK`/sec — still
an order of magnitude below polling, and it converts a silent-drift failure into a self-healing
one. If reconciliation ever *does* correct a position, that is a bug and it gets logged as one,
with `event_id` and `user_id` (CLAUDE.md §4).

**FR-7 (position never increases) is enforced at the point of display**: the process keeps the
last value it sent and clamps upward corrections. A user is never shown a number going backwards;
the discrepancy goes to the logs instead, where it belongs.

---

## 7. Admission — the token bucket that removes leader election

**Not built.**

Admission must satisfy two things at once: pop from the front (FR-9), and never exceed the global
rate (FR-10). Doing them as separate steps is what breaks.

### The broken interleaving

Two queue-service processes each run an admission loop. Rate is 100/min; both wake at the same
instant:

| | Process 1 | Process 2 | Result |
|---|---|---|---|
| t₁ | read bucket → 100 tokens | | |
| t₂ | | read bucket → 100 tokens | both believe they may admit 100 |
| t₃ | `ZRANGE 0..99` → members 1–100 | | |
| t₄ | | `ZRANGE 0..99` → members 1–100 | **the same 100 people** |
| t₅ | `ZREM` those 100 | | |
| t₆ | | `ZREM` those 100 (no-ops) | |
| t₇ | issue 100 passes | issue 100 passes | **200 passes for 100 people** |

Two distinct failures from one race:

- **Over-admission**: 200 admitted in a window sized for 100. The backpressure that is the
  system's entire reason to exist has been bypassed, and the booking service takes 2× its
  designed load.
- **Double admission** (FR-14): the same person gets two passes with two `jti`s. The booking
  service's `jti` uniqueness will not save them — two different `jti`s are two different requests
  — so it falls through to the `(event, user_id)` constraint, which turns a queue bug into a
  confusing 200-with-existing-booking on the booking side. A bug that surfaces two services away
  from its cause.
- And `admitted` is incremented twice for the same pop, so **every waiter's position arithmetic
  in §6 is now wrong** — a third failure, in a third component.

### The fix: refill, consume, and pop in one script

```lua
-- admit_batch.lua
-- INVARIANT: across ALL queue-service processes, at most `rate` waiters are admitted per
-- window for this event, and each waiter is popped exactly once.
--
-- Refill, consume and ZPOPMIN are one atomic step. If they were separate, two processes could
-- both pass the rate check (over-admission, 2x the load the booking service was sized for) and
-- both pop the same members (double admission + a corrupted `admitted` counter, which silently
-- breaks every waiter's computed position -- see design.md §6).
--
-- `now_ms` is passed in as ARGV rather than read via redis.call('TIME') so the script is a pure
-- function of its inputs: unit-testable with a fake clock, and trivially deterministic under
-- replication.
```

Steps, in one atomic execution: read `tokens`/`last_refill_ms` → refill by elapsed time, capped at
burst → `n = min(floor(tokens), batch_max, queue length)` → write back `tokens − n` → `ZPOPMIN n`
→ `INCRBY admitted n` → return the popped members.

### The consequence worth noticing

**Because the bucket is atomic, every process can run an admission loop safely. There is no
leader.**

The alternative design is a single designated admitter, elected with a Redis lock. That is easier
to reason about — one writer, no contention — and it is what most people reach for. It costs:
lease renewal, failover detection, a stall while the lease expires after a crash, and a genuine
single point of failure during that window.

Making the *operation* atomic instead of the *actor* exclusive removes all of it. Processes
contend on a Redis script that is already serialised; a crashed process admits nobody and the
others simply carry on. This also answers the sticky-vs-stateless question in §9 before it is
asked.

`ZPOPMIN key count` is itself atomic, so the pop alone would not need Lua. The script exists
because **the rate check and the pop must be atomic *together*** — that is the honest
justification, and "we used Lua because Redis operations aren't atomic" is the wrong answer to
give an interviewer.

### Dynamic backpressure (v2) — who sets the rate

Everything above enforces a rate; it does not decide one. `rate_per_min` lives in the event's
config hash, and `admit_batch.lua` re-reads it every tick (FR-13), so it can change mid-drop with
no restart. Phase 8 built that knob for a *human* operator. v2 automates the operator: a control
loop reads the booking service's p99 and moves the rate to hold it near an SLO.

The control law is **AIMD** — additive increase while the booking service is healthy, multiplicative
decrease the moment its p99 crosses the target — the same law TCP uses for congestion control, and
for the same reason: over-admitting is an outage of the thing the queue protects, under-admitting is
a slightly longer wait, so the response to the two must be asymmetric (shed load hard, reclaim it
gently). The p99 is real: the booking service records request latency in a Prometheus histogram, and
the controller reads `histogram_quantile(0.99, …)` from Prometheus. A missing signal means *hold*,
never probe up — raising load on a service you cannot see is how you melt it.

**One structural point worth the interview:** the admitter is deliberately leaderless (§7), but this
controller must run as a *single* instance. Its logic is a read-modify-write on `rate_per_min` with
a decision in between — not one atomic step — so two controllers would race and, being a control
loop, oscillate. Same system, opposite answer: atomicity where you can get it (the admitter),
exclusivity where you can't (the controller). Full rationale and the rejected alternatives
(proportional, PID; an in-process p99 gauge; fail-safe-decrease) are in `decisions.md`
(2026-08-13). Built and unit-tested (`core/backpressure.py`, 24 tests); the closed loop under live
load is the pending validation.

---

## 8. SSE fan-out

**Not built.** CLAUDE.md §8 calls the naive version "the #1 architectural mistake in Python SSE
services", and it is.

### The mistake

One Redis pub/sub subscription per connected client. 20,000 waiters → 20,000 Redis connections.
Redis's default `maxclients` is 10,000, so it fails outright — and long before that, each
connection costs a socket, a buffer, and a file descriptor on both ends.

### The design

**One subscriber task per worker process.** Not per connection.

```
Redis pub/sub  ──►  subscriber task (1 per process)
                         │
                         ├──► asyncio.Queue ──► SSE connection  (waiter 1)
                         ├──► asyncio.Queue ──► SSE connection  (waiter 2)
                         └──► ...                                (× 5,000)
```

Each SSE connection creates a bounded `asyncio.Queue`, registers it in a
`dict[event_id, set[Queue]]`, and iterates it, yielding SSE frames. The subscriber receives one
message and fans it out in memory.

Redis connections per process: **one**, regardless of connection count (FR-16).

### Messages carry state, not deltas

Each admission message carries the absolute `admitted` count, never "+3". This is what makes the
next two decisions safe:

- **A dropped message is harmless.** The following message is complete on its own. There is no
  resync protocol, no sequence gap detection, no replay buffer — none of that machinery is needed
  because there is no accumulated state to lose (FR-17).
- **A slow client can be dropped, not waited on.** Each queue is bounded; `put_nowait` on a full
  queue means that client is not keeping up, so we discard the *oldest* message for that client
  and move on. Nobody else is blocked by one stalled reader (FR-18).

The alternative — deltas — would be marginally smaller on the wire and would require every
consumer to be perfectly reliable, forever. That trade is obviously wrong here, and it is worth
being able to say why rather than just picking the right one.

### Django-specific mechanics

- `StreamingHttpResponse` with an **async** generator (Django 4.2+). A sync generator would be
  run in a thread — see CLAUDE.md §8.
- `MIDDLEWARE = []`. One non-`async_capable` middleware forces the whole request through
  `sync_to_async` and therefore the ASGI thread pool. Thousands of parked threads is how this
  service dies, and it would be a configuration change nobody associates with the symptom.
- `GZipMiddleware` and `ConditionalGetMiddleware` must never be added: both must consume the
  stream to do their job, so the client receives nothing until the generator ends — which for SSE
  is never.
- `X-Accel-Buffering: no` on the response, plus `proxy_buffering off` in Nginx/Caddy. Same class
  of bug one layer out (CLAUDE.md §8).
- Heartbeat `: ping\n\n` every ~15s or proxies drop idle connections. At 20,000 connections that
  is ~1,300 writes/sec carrying nothing — cheap per write, not obviously cheap in aggregate, so
  it is on the measurement list rather than assumed fine.

---

## 9. Horizontal scaling

**Demonstrated (2026-08-13, L2… see [`loadtest-report.md`](loadtest-report.md) L3).** Ran 3 queue
replicas behind Caddy load-balancing (`dynamic a` + round-robin): requests distributed 6/6/6, and a
single admission broadcast reached SSE streams on all three replicas (2/2/2) — the stateless property
below, confirmed rather than argued. A one-line finding fell out of it: the naive `reverse_proxy
queue:8001` resolves once and pins to one replica (effectively sticky), which is precisely the
mistake the "sticky vs stateless" question is about. What is *not* shown is a throughput number —
3 replicas share one laptop's CPU and one Redis.

The interesting question is sticky versus stateless load balancing, and this design answers it by
construction:

**No stickiness is required.** A waiter's place lives in Redis, keyed by their queue token. Every
process subscribes to the same pub/sub channels. So a reconnect can land on any process, a
process can be restarted mid-drop, and nothing is lost — the new process reads the token, gets
the sequence, and resumes (FR-20).

What that costs: every process holds a subscription to every event channel it has waiters for,
and every process fans out every message. At N processes the pub/sub delivery work is N× the
single-process amount. Fine at N=3; a reason to shard channels by event at much larger N.

What we would have had to build with sticky sessions: consistent hashing at the proxy, session
affinity that survives a process restart, and a rebalancing story. All of it avoided by keeping
per-connection state to exactly one integer (the arrival sequence) that can be re-read from Redis
at any time.

**Redis is the single point of failure**, and v1 says so out loud rather than implying otherwise.
Sentinel failover is a v2 item, and the honest statement is in §11.

---

## 10. The booking service

**Built and tested.** The design here is deliberately small, and it is where FR-26 through FR-30
already live. Full reasoning in [`decisions.md`](decisions.md), 2026-07-18.

Two things are worth restating because they are the parts interviewers ask about:

**Oversell is prevented by atomicity, not by locking.**

```python
rows = Event.objects.filter(
    pk=event_id, tickets_booked__lt=F("capacity"),
).update(tickets_booked=F("tickets_booked") + 1)
```

The `WHERE` is the capacity check and the `SET` is the claim, in one statement. Postgres serialises
concurrent updates to the same row, so there is no window between check and write. `rows == 1`
means a ticket was claimed; `rows == 0` means sold out. No `SELECT ... FOR UPDATE`, no held lock.
A `CheckConstraint (tickets_booked <= capacity)` is the database-level backstop.

**Double booking is prevented by idempotency, not by atomicity.** Two `UNIQUE` constraints —
`token_jti` and `(event, user_id)` — stop two genuinely different failures: a replayed request,
and a user re-admitted with a fresh pass.

These are different problems with different tools, and conflating them is a standard interview
trap: **overselling is concurrency → atomicity; double-booking is repetition → idempotency.**

---

## 11. Failure modes

What breaks, what the user sees, and whether we have actually handled it.

| Failure | Blast radius | Behaviour | Handled? |
|---|---|---|---|
| **Redis unavailable** | Total | Nobody joins, no positions, no admissions. Connected waiters see "Reconnecting…" and keep their last number. | **No — accepted SPOF in v1.** Sentinel is v2. Saying "Redis is HA" without having tested a failover would be a lie the load test would catch. |
| Queue-service process crashes | Its connections only | Clients reconnect elsewhere and resume from Redis (§9) | By design |
| All queue-service processes down | Total intake | Nobody can join. Already-issued passes still work — the booking service does not depend on the queue service (§4). | By design |
| Booking service down | Admitted users only | Passes are issued but bookings fail. Queue keeps queueing. Operator sets rate to 0. | Manual in v1; automatic in v2 (backpressure from booking p99) |
| Booking service slow | Admitted users | Same as above, degraded rather than failed | v2 |
| Postgres down | Bookings | Booking 5xx. Queue unaffected. | Not handled — acceptable, it is the thing being protected |
| Clock skew between processes | None | Ordering comes from `INCR`, not clocks (§3). Only the token bucket reads time, and it is monotonic per Redis key. | By design |
| Pub/sub message lost | One tick of freshness | Next message is absolute state (FR-17); periodic `ZRANK` reconciliation closes any gap (§6) | By design |
| Slow client | That client | Bounded queue drops its oldest messages (FR-18) | By design |
| A waiter abandons the queue | Effective rate | Still admitted in turn, pass unused; real throughput below configured (§6) | **Known, accepted, quantified in the load test** |
| Nginx buffers SSE | All connections | Nothing reaches any client; looks like a total outage | Config, CLAUDE.md §8 |
| A sync middleware is added | All connections | Requests move onto the ASGI thread pool; service dies at a few hundred connections | `MIDDLEWARE = []` + CLAUDE.md §8 |

---

## 12. Multi-region

Explicitly a non-goal (CLAUDE.md §1). One paragraph beats a broken implementation:

A global FIFO across regions requires a globally ordered counter, which means either one region
owns `INCR` — adding a cross-ocean round trip to the hottest path, and making the whole queue
unavailable when that region is — or an ordering protocol with real consensus behind it. The
cheaper and more honest design is **one queue per region**, each with its own counter and its own
admission rate, with the rates summing to what the booking service can take. Fairness then holds
*within* a region and not across them, and that is a product decision to be stated plainly on
screen, not a technical detail to be buried. Nobody in a queue can perceive cross-region ordering
anyway; what they can perceive is a queue that stalls because another continent is offline.

---

## 13. What we intend to prove

First numbers landed 2026-08-13 (L1 in [`loadtest-report.md`](loadtest-report.md)) — a local
single-process floor. Most rows are still ⏳; the two L1 touched are marked 📏 with their caveat.
This section exists so the load test has a target to falsify.

| Claim to test | Target | Method | Status |
|---|---|---|---|
| Concurrent SSE connections held on one box | 10,000 | k6, ramped, `ulimit -n` and `somaxconn` raised first | 📏 **3,000 held on 1 uvicorn worker, 0 failed** (L1 — a local floor, not the 10K target; k6 + Gunicorn still ⏳) |
| Concurrent SSE connections, 3 processes | 20,000 | as above, behind Nginx | ⏳ not run |
| Position-update p99, end to end | < 200 ms | timestamp in the admission message vs client receipt | ⏳ not run |
| Redis ops/sec is flat in connection count | flat | Redis `INFO commandstats` at 1K vs 10K connections | 📏 **flat: steady hold = 3 cmds at 500 / 1,500 / 3,000 connections** (L1; 1K→10K still ⏳) |
| Backpressure closed loop drives rate from booking p99 | both directions | live Docker stack; real Prometheus p99; observe `rate_per_min` | 📏 **yes, live (L2)**: overload 600→10, healthy 10→150; caveat — overload induced via the target, not real booking saturation |
| Memory per connection | to be discovered | RSS delta ÷ connections | ⏳ not run |
| No over-admission under concurrent admitters | exact | count issued passes vs configured rate, 3 processes | ⏳ not run |
| No oversell under true concurrency | exact | parallel booking load; `TransactionTestCase`, not `APITestCase` | ⏳ **D1 tests are sequential — this is explicitly not yet proven** |
| Heartbeat cost at scale | to be discovered | with and without heartbeats at 10K | ⏳ not run |
| Django ASGI per-request overhead vs Starlette | to be discovered | same endpoint, both frameworks | ⏳ not run |

Every one of these lands in [`loadtest-report.md`](loadtest-report.md) with the command that
produced it. **A number that is not in that file does not go on a resume** (CLAUDE.md Rule 7).

---

## Change log

| Date | Change | Why |
|---|---|---|
| 2026-08-02 | Created. FR-1..FR-30 with build status; Redis model; the join race and the admission race written out as interleavings; O(1) position arithmetic with its three preconditions; one-subscriber-per-process fan-out; failure modes with Redis named as the v1 SPOF. | Design doc promised by CLAUDE.md §5 and never written; queue service about to start |
| 2026-08-02 | Queue service specified as async Django (ASGI), not FastAPI | See `decisions.md` 2026-08-02 |
| 2026-08-13 | §13: first two rows measured (SSE connections held → 3,000 single-process floor; Redis ops flat across 500→3,000) | Phase 14 L1 run, see `loadtest-report.md` and `decisions.md` 2026-08-13 |
