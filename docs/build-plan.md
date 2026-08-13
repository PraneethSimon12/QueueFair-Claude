# QueueFair — Build Plan & Wire Contract

> **The execution doc.** Every phase in order, and the exact URL, payload and response for every
> endpoint. Open this while building.
>
> - [`product-spec.md`](product-spec.md) — *what* it does (behaviour, agreed)
> - [`design.md`](design.md) — *why* it is built this way
> - **`build-plan.md`** — *how* to build it, in what order, with what contracts — you are here
> - [`decisions.md`](decisions.md) — the decision log
> - [`../CLAUDE.md`](../CLAUDE.md) — the guardrails, including the teaching protocol
>
> **§3 is the single source of truth for the wire format.** If `design.md` disagrees, this file
> wins on the wire and `design.md` gets corrected.
>
> ⚠️ **This file does not replace CLAUDE.md Rule 1.** Each phase still starts with: list the
> concepts, ask what I already know, teach the gaps, get a yes. A written plan is not consent to
> skip the teaching — it is what we teach *from*.

**Status:** Phases 0–13 built — metrics + Prometheus/Grafana · Phase 14 (k6 load tests) next
**Last updated:** 2026-08-02

---

## Contents

1. [Ground rules](#1-ground-rules)
2. [Shared shapes](#2-shared-shapes)
3. [Complete API surface](#3-complete-api-surface)
4. [Error codes](#4-error-codes)
5. [Performance budget](#5-performance-budget)
6. [The phases](#6-the-phases)
7. [Frontend integration](#7-frontend-integration)
8. [Validation rules](#8-validation-rules)

---

## 1. Ground rules

### Responses are plain JSON with meaningful HTTP status codes

No success/error envelope. The booking service already answers this way (`{"detail": "..."}` plus
a real status code, which is DRF's idiom), and inventing a wrapper for the queue service alone
would give one system two response grammars.

```jsonc
// success — the resource, directly
{ "position": 8391, "total_waiting": 61004, "eta_seconds": 1200 }

// failure — a detail string, and the status code carries the meaning
{ "detail": "unknown queue token" }
```

> This is a deliberate departure from the wrap-everything convention used elsewhere. The reason
> it is right *here*: there are eight endpoints, both services are ours, and the clients are one
> HTML page and one k6 script. The reason to wrap — many clients that must branch on a single
> shape — does not apply.

### Identity

A waiter is identified by a **queue token**: 32 lowercase hex characters, minted by the server on
first join, returned in the response body and set as an `HttpOnly` cookie **named** per event
(`qf_{event_id}`) at `Path=/`. The token is the place in the queue. There are no accounts
(`product-spec.md` §9).

> Corrected 2026-08-02. This paragraph previously said the cookie was "scoped to the event path",
> which contradicted the `Set-Cookie` line in §3.1 (`Path=/`). §3 is the wire truth, so the prose
> was wrong: isolation between events comes from the cookie **name**, not its path — the queue
> endpoints all live under `/api/queue/`, so a per-event path would buy nothing and would break
> the moment a URL moved.

Every request after join carries it — cookie preferred, `?t=` query parameter accepted because
`EventSource` cannot set headers.

### Two different tokens — do not confuse them

| | Queue token | Admission pass |
|---|---|---|
| What | 32 hex chars, opaque | HS256 JWT |
| Issued by | queue service, on join | queue service, on admission |
| Means | "this is my place in line" | "I waited and it is my turn" |
| Lifetime | the drop | **60 seconds** |
| Verified by | queue service (Redis lookup) | booking service (local HMAC, no lookup) |
| If leaked | someone else can watch your position | someone else can book in your name |

### Logging

Every log line on the queue path carries `event_id` and `user_id`, JSON-structured, no bare
`print` (CLAUDE.md §4).

---

## 2. Shared shapes

**`QueueState`** — what a waiter is told, in the join response and in every SSE `position` frame.

```json
{ "position": 8391,
  "total_waiting": 61004,
  "admitted_total": 15790,
  "eta_seconds": 1200,
  "state": "waiting" }
```

`state` is `waiting` | `admitted` | `expired` | `unknown`.
`eta_seconds` is `position ÷ current admission rate`, **rounded up**, and is **an estimate** — the
UI must render it hedged ("about 20 minutes"), never as a countdown (`product-spec.md` F5,
rule 21).

**`eta_seconds` is nullable.** When `rate_per_min` is `0` the drop is paused (FR-13) and the
field is `null` — not `0`, which would render as "any moment now", the exact opposite of the
truth. Clients must handle `null` as "no estimate available". *Added 2026-08-02, Phase 6: the
paused case had no defined wire representation.*

**`Admission`** — the payload of getting in.

```json
{ "pass": "eyJhbGciOiJIUzI1NiIs...",
  "expires_at": 1780000060,
  "book_url": "/events/coldplay-mumbai-2026/book" }
```

**`Booking`** — the booking service's response. **Already built** —
`booking_service/bookings/views.py::_serialize`. Do not change it without changing that function.

```json
{ "booking_id": 41, "event_id": "coldplay-mumbai-2026", "user_id": "u-8391",
  "jti": "9f2c...", "created_at": "2026-08-02T12:04:11.221Z", "status": "confirmed" }
```

---

## 3. Complete API surface

**Eight endpoints.** Queue service base `/api/queue/`; booking service is mounted separately.

### 3.1 Queue service

#### `POST /api/queue/{event_id}/join`

Join, or resume an existing place. **Idempotent** — this is FR-2 and FR-3, and the whole of
fairness promise F2.

Request: empty body. Queue token from cookie or `?t=`, if the client has one.

```json
{ "queue_token": "3f1c8e2a94b7...",
  "sequence": 24181,
  "position": 24181,
  "total_waiting": 61004,
  "admitted_total": 0,
  "eta_seconds": 1200,
  "state": "waiting",
  "joined": true }
```

`joined` is `true` on first placement, `false` when resuming — the *only* difference a refresh
makes. Sets `Set-Cookie: qf_{event_id}=<token>; HttpOnly; SameSite=Lax; Path=/`.

- **200** always on success, whether newly joined or resumed. Not 201: a resume creates nothing,
  and a client must not be able to tell the two apart by status code.
- Errors: `404 unknown_event` (also returned for a **malformed** event id — a malformed and an
  unknown id are the same thing to a client, and separating them leaks which events exist) ·
  `405 method_not_allowed` · `503 redis_unavailable`.
- **A malformed queue token is replaced, not rejected.** Unlike every other endpoint (§8), join
  treats an unparseable `?t=`/cookie as no token at all and mints a fresh one. Join is the front
  door: 404-ing a client over garbage it has no way to fix would strand it permanently.
  *Clarified 2026-08-02, Phase 6 — §8's table and §3.1 disagreed for this one endpoint.*

#### `GET /api/queue/{event_id}/position`

The v0 polling endpoint, kept after SSE lands as a debugging and fallback path.

Query: `?t=<queue_token>` (or cookie)
→ `QueueState`, authoritative (`ZRANK`), not the arithmetic.

- **200** `QueueState` · **404** `unknown_event` · **404** `unknown_token` · **405** · **503**

**When the waiter has already been admitted**, the body is a `QueueState` with `state:
"admitted"`, `position: 0`, `eta_seconds: null`, plus an extra `admission` object:

```json
{ "position": 0, "total_waiting": 61001, "admitted_total": 3,
  "eta_seconds": null, "state": "admitted",
  "admission": { "pass": "eyJhbGciOiJIUzI1NiIs...",
                 "expires_at": 1780000060,
                 "book_url": "/events/coldplay-mumbai-2026/book" } }
```

*Added 2026-08-02, Phase 8.* Admission removes the waiter from the sorted set, so `ZRANK` cannot
distinguish "just reached the front" from "never queued" — and answering a person who has just
been admitted with `unknown_token` is the most alarming thing this system could say, at the worst
possible moment. Phase 10's SSE `admitted` frame becomes the primary delivery path; this stays as
the fallback, and is the only way a waiter who was mid-reconnect during their admission can find
their pass.

Once the pass's 60s TTL expires the waiter falls back to **404** `unknown_token`. `expired` and
`unknown` are genuinely indistinguishable at that point, and Journey D's "your turn passed,
rejoin" is the honest reading of both.

#### `GET /api/queue/{event_id}/stream`

The SSE endpoint. The interesting one.

Query: `?t=<queue_token>`
Response headers — every one of these load-bearing:

```
Content-Type: text/event-stream
Cache-Control: no-cache
Connection: keep-alive
X-Accel-Buffering: no      ← without this, Nginx buffers and the client receives nothing
```

Frames:

```
retry: 3000

event: position
data: {"position":8391,"total_waiting":61004,"admitted_total":15790,"eta_seconds":1200,"state":"waiting"}

event: admitted
data: {"pass":"eyJhbGciOiJIUzI1NiIs...","expires_at":1780000060,"book_url":"/events/coldplay-mumbai-2026/book"}

: ping
```

- `retry: 3000` is sent once on connect so the browser reconnects after 3s rather than its
  default 3s-with-backoff-we-do-not-control.
- `: ping` every **15 seconds** — a comment frame, ignored by `EventSource`, present only so
  proxies do not drop an idle connection (CLAUDE.md §8).
- **No `id:` field, and `Last-Event-ID` is ignored.** Every frame carries absolute state
  (`design.md` FR-17), so there is nothing to replay. A reconnecting client gets the current
  truth in its first frame, which is strictly better than a replayed history.
- The stream closes after an `admitted` frame. There is nothing further to say.

Errors are HTTP, before the stream opens: **404** `unknown_event` / `unknown_token`. Once the
stream is open, failures are frames, not status codes — the status is already sent.

#### `POST /api/queue/{event_id}/rate` — operator only

Change the admission rate at runtime (FR-13, rule 12).

```json
{ "rate_per_min": 100, "burst": 20, "batch_max": 50 }
```

`rate_per_min: 0` pauses the drop. Auth: a shared operator secret in `X-Operator-Key`, compared
with `hmac.compare_digest`. **Not** the admission secret — a different key for a different
trust level.

- **200** the new config · **401** `bad_operator_key` · **400** `validation_error`

#### `GET /healthz`

`{"status": "ok", "redis": "ok"}` · **200** / **503**. Checks Redis with `PING`, because a
process that cannot reach Redis is not healthy no matter how well it is running.

#### `GET /metrics`

Prometheus text format. prometheus-client in **multiprocess mode** with a shared directory —
required under Gunicorn workers, and fiddly (CLAUDE.md §8).

Series: `qf_queue_depth{event}`, `qf_admitted_total{event}`, `qf_sse_connections{event}`,
`qf_admission_batch_seconds`, `qf_redis_command_seconds{command}`, `qf_position_drift_total`
(a reconciliation that actually corrected something — see `design.md` §6; this should be zero,
and a non-zero value is a bug, not a metric).

### 3.2 Booking service — **already built**

#### `POST /events/{event_id}/book`

Header: `Authorization: Bearer <admission pass>`. Body: empty.

| Status | Meaning | Body |
|---|---|---|
| **201** | New booking | `Booking` |
| **200** | Idempotent replay — same pass, same booking | `Booking` |
| **401** | Missing / malformed / invalid / expired pass | `{"detail": "..."}` |
| **403** | Valid pass, wrong event | `{"detail": "admission token is for a different event"}` |
| **404** | No such event | `{"detail": "no such event"}` |
| **409** | Sold out | `{"detail": "event is sold out"}` |

Note the URL has **no trailing slash**: `APPEND_SLASH` cannot redirect a POST without losing the
body, so clients must hit the exact path.

---

## 4. Error codes

| HTTP | `detail` | Meaning |
|---|---|---|
| 400 | `validation_error` | Malformed body or query parameter |
| 401 | invalid/expired pass | Booking service, pass verification failed |
| 401 | `bad_operator_key` | Rate endpoint, wrong operator key |
| 403 | wrong event | Pass is valid but for another event |
| 404 | `unknown_event` | No queue configured for this event id |
| 404 | `unknown_token` | Queue token not in this event's queue |
| 409 | sold out | Booking service, at capacity |
| 503 | `redis_unavailable` | Queue service cannot reach Redis — the v1 SPOF (`design.md` §11) |

---

## 5. Performance budget

Redis round trips per operation. These are targets that
[`loadtest-report.md`](loadtest-report.md) must confirm; a regression here is the difference
between the design working and not.

| Operation | Redis round trips | How |
|---|---|---|
| `POST /join` | **1** | one `EVALSHA join.lua` — see `design.md` §5 for why not 3 |
| `GET /position` | **1** | one `EVALSHA position.lua` — `ZRANK` + `ZCARD` + admitted + rate. *Was budgeted at 2 (`ZSCORE` + `ZRANK`); `ZRANK` alone answers both "am I queued" and "where", and folding the rest into the same script makes one call do the work of four. The reason it is a script is not the round trip, it is the **atomic snapshot** — see Phase 7.* |
| SSE **per connected client per tick** | **0** | position is arithmetic in memory (`design.md` §6) |
| SSE per **process** | **1 subscription**, total | one subscriber task, not one per connection (FR-16) |
| Admission batch | **1** | one `EVALSHA admit_batch.lua` — refill + pop + counter, atomic |
| Reconciliation | 1 per connection per **30 s** | `ZRANK`, ~667/s at 20K connections |
| `POST /book` | 0 Redis; **≤ 4** Postgres | existence check, conditional UPDATE, INSERT, (replay lookup) |

**The rules that keep these true:**

- Every Lua script is loaded once and invoked by `EVALSHA`; never ship the script body per call
- No `KEYS *`, no `SMEMBERS` on the queue, no `ZRANGE 0 -1` — nothing O(N) on the hot path
- The queue length in `QueueState` comes from `ZCARD` **inside** the admission script's result,
  broadcast to everyone, not read per client
- Never `await` Redis inside the SSE send loop — the send loop reads only from its
  `asyncio.Queue`
- Connection registry lookups are dict/set operations, never a scan

---

## 6. The phases

Build in order — later phases import earlier ones. Every phase leaves the system runnable
(CLAUDE.md Rule 10).

Each phase begins with the Rule 1 concept check and ends with the Rule 9 understanding check,
logged to [`interview-prep.md`](interview-prep.md).

### ✅ Phase 0 — Booking service skeleton

Venv, Django project, `.env` + `.env.example` with a hand-rolled loader, PostgreSQL with a
least-privilege role.
**Done when:** `manage.py check` passes against Postgres. — **done 2026-07-18**

### ✅ Phase 1 — `Event` and `Booking` with DB-level integrity

Slug natural key; `tickets_booked` counter; `CheckConstraint (tickets_booked <= capacity)`;
`UNIQUE(token_jti)`; `UNIQUE(event, user_id)`; `on_delete=PROTECT`.
**Done when:** the migration applies and every constraint exists in Postgres. — **done**

### ✅ Phase 2 — Admission-pass verification

`tokens.py` — pure function, secret passed as an argument, `algorithms=["HS256"]` pinned,
required claims enforced.
**Done when:** unit tests pass **without a database** — the run prints *"Skipping setup of unused
database(s)"*. — **done**

### ✅ Phase 3 — `POST /events/{id}/book`

DRF authentication class; atomic conditional UPDATE; idempotent replay; 201/200/401/403/404/409.
**Done when:** 16 tests green. — **done**

### ✅ Phase 4 — Oversell under **true** concurrency

**This phase exists because Phase 3's tests do not prove what they look like they prove.**
`APITestCase` wraps each test in a transaction that is rolled back, and the requests run
sequentially — so they demonstrate correctness and idempotency but **say nothing about
concurrency**. The claim "no oversell under load" is currently unproven.

**Build:** a `TransactionTestCase` (or a live server plus a parallel client) that fires N
simultaneous bookings at an event with capacity M < N, and asserts exactly M bookings exist and
`tickets_booked == M`.
**Done when:** the test fails against a deliberately broken read-modify-write implementation and
passes against the real one. A concurrency test that has never failed has not been shown to test
anything.

**Done 2026-08-02.** `bookings/test_concurrency.py`, 3 tests, suite now 19 green. 60 threads
released by a `threading.Barrier` against a capacity-20 event. Real implementation: exactly 20
`created`, 40 clean `SoldOut`, counter and rows both 20. Mutation proof (run against the broken
read-modify-write, then discarded):

```
BROKEN IMPL: capacity=20 contenders=60 -> created=60 soldout=0 tickets_booked=1 booking_rows=60
AssertionError: 60 != 20
```

60 increments, **one** recorded — a lost update, and `CheckConstraint (1 <= 20)` was satisfied
throughout. That is why the assertion compares the counter against `COUNT(bookings)` rather than
against `capacity`: the counter alone cannot see this bug.

### ✅ Phase 5 — Queue service skeleton

Second Django project, ASGI, **`DATABASES = {}`**, **`MIDDLEWARE = []`**, `redis.asyncio` client
built once at startup, `/healthz`, Uvicorn under Gunicorn.
**Done when:** `/healthz` returns Redis `ok` from an `async def` view, and importing the ORM
anywhere in the service fails.

**Done 2026-08-02.** 12 tests green. Against a live Uvicorn on :8001 —

```
HTTP 200 {"status": "ok", "redis": "ok"}     unknown path -> 404
```

`tests/test_invariants.py` pins the four constraints so they cannot be edited away silently:
`MIDDLEWARE == []`, the dummy DB backend, no WSGI entry point, and `core/` importing nothing
from the IO edge. That last one was shown to fail on demand (a throwaway `core/_violation.py`
produced `core/_violation.py:1 imports adapters.redis_client`, then was deleted).

**Three corrections the build made to its own assumptions** — each is in `decisions.md`:
`DATABASES = {}` does not remove the connection, Django injects a **dummy backend**;
`settings.DATABASES` is mutated **in place**, so asserting `== {}` is order-dependent;
and `SimpleTestCase` blocks queries itself, so the obvious version of the ORM test passes even
with a real PostgreSQL configured.

**Deviation from the phase text:** no Gunicorn. It is POSIX-only (`fcntl`) and cannot run on the
Windows dev box, so local runs are a single Uvicorn process and `gunicorn + UvicornWorker`
arrives with Docker in Phase 12 — the first time this service runs on Linux. Recorded in
`requirements.txt` rather than left as a silent gap.

### ✅ Phase 6 — `join.lua` and `POST /join`

The idempotent join, the fairness core. Write the broken check-then-act version **first**,
demonstrate the interleaving from `design.md` §5 with two concurrent clients, then fix it.
**Done when:** joining 1,000 times with the same token yields one sequence and no gaps in
`qf:E:seq`, and the broken version is shown failing that assertion.

**Done 2026-08-02.** 43 tests green. Both halves of the criterion, same 1,000-join workload,
same single token:

```
NAIVE join:   1000 joins of ONE token -> seq counter=50, 50 distinct sequences handed out,
                                         49 gaps burned, surviving score=50
SCRIPTED:     1000 joins of ONE token -> seq counter=1,   1 sequence, 0 gaps, joined=True once
```

The naive line is `design.md` §5's table happening for real: one user handed **50 different
sequence numbers**, and their surviving score is **50 instead of 1** — 49 places worse purely for
double-tapping. The 49 burned numbers are the more important half, because gaps silently inflate
every later waiter's `position = my_seq − admitted` (§6) and `ZADD NX` would not have fixed them.

Also proved: 1,000 *distinct* tokens joining concurrently consume exactly 1..1000 — the density
precondition that makes the O(1) position design legal.

Live, against Uvicorn on :8001 with a real cookie jar:

```
waiter A join 1 -> 200 {... 'sequence': 1, 'position': 1, 'joined': True}
                   Set-Cookie: qf_coldplay-mumbai-2026=e3a1…; HttpOnly; Max-Age=21600; Path=/; SameSite=Lax
waiter A join 2 -> 200 {... 'sequence': 1, 'position': 1, 'joined': False}   <-- refresh
waiter B join   -> 200 {... 'sequence': 2, 'position': 2, 'joined': True}
unknown event   -> 404 {"detail": "unknown_event"}
GET             -> 405 {"detail": "method_not_allowed"}
```

Journey B holds: a refresh changes exactly one field, `joined`.

**Also built, because join could not be exercised without it:** `manage.py create_event` — an
event exists precisely when `qf:{event}:config` exists, which is join.lua's `EXISTS` check.
A management command, not an endpoint, for the reason in `decisions.md` 2026-07-28.

**Three wire-contract defects this phase found and fixed** — see §1, §2 and §3.1 above: the
cookie `Path` contradiction, `eta_seconds` having no defined value for a paused drop, and §8
disagreeing with §3.1 about a malformed token at the one endpoint where it matters.

### ✅ Phase 7 — `GET /position`

Authoritative `ZRANK`. v0 polls this every 5 seconds.
**Done when:** ten clients see ten correct, distinct positions.

**Done 2026-08-02.** 59 tests green. Ten clients with ten independent cookie jars see positions
1–10, all distinct, `total_waiting` 10 for every one of them.

**Why it is a Lua script and not two commands.** Not the round trip — the **atomic snapshot**.
Read `ZRANK` and the admitted counter separately and an admission batch can land between them,
so one response mixes numbers from two different instants. The error is not random: it can make
a waiter's position go **up**, which is the one thing `product-spec.md` F4 promises never happens.

**Two tests that pin why this endpoint survives SSE** rather than being deleted at Phase 10:

- `test_position_is_rank_not_sequence` — pop the front waiter and the survivor's *sequence* is
  still 2 while their *position* is now 1. Sequence is fixed at arrival; position is not.
- `test_zrank_does_not_drift_when_a_waiter_abandons` — remove a waiter from the middle without
  admitting them, and `my_seq − admitted` says 3 where `ZRANK` says 2. **The cheap arithmetic
  drifts by exactly one per abandonment, forever.** That is the drift `design.md` §6 reconciles
  against, and this endpoint is the reference it reconciles *to*.

Plus `test_position_never_increases_while_the_queue_only_drains`, which checks fairness promise
F4 as a monotonicity assertion over ten successive readings instead of leaving it as prose.

### ✅ Phase 8 — `admit_batch.lua`, the admission loop, pass issuance

Token bucket + `ZPOPMIN` + counter, atomic. The loop runs in every process — no leader
(`design.md` §7). Issues the HS256 pass Phase 2 already verifies.
**Done when:** three processes admitting concurrently for 60 seconds at 100/min issue **exactly**
the configured number of passes, no duplicates. Then: an admitted user books end to end.

**This is the point v0 is complete** — a real person can queue, be admitted, and book.

**Done 2026-08-02. 🎉 v0 complete.** 86 queue tests + 19 booking = **105 green**.

Both halves of the criterion, with a fake clock so "60 seconds" is exact rather than flaky:

```
NAIVE admit: 3 concurrent processes, budget=20 -> 60 passes for 20 distinct people
3 processes x 60s @ 100/min (burst 20): 120 passes, 120 distinct, expected 120
```

The naive line is `design.md` §7's table happening for real, and it shows **all three** failures
at once: 3× over-admission, every one of the 20 people holding three passes, and an `admitted`
counter of 60 that would have corrupted every remaining waiter's position arithmetic.

**The full v0 loop, both services live** (rate 60/min, burst 3, 8 waiters):

```
waiters 1-8 join            -> positions 1..8
one admitter tick           -> admitted 3
waiters 1-3                 -> state "admitted", pass attached
waiters 4-8                 -> waiting, positions 1..5   (shifted up by exactly 3)
waiter 1 books              -> HTTP 201  booking_id 5
waiter 1 retries            -> HTTP 200  same booking_id
waiter 8 forges a pass      -> HTTP 401  invalid admission token
```

That last line is the product: **you cannot book without having waited.**

**Two additions the phase forced, both documented above in §3.1:**

- **`qf:{event}:pass:{token}`** — the first per-*waiter* key in a design that was all per-event.
  Admission removes you from the sorted set, so a polling client has nowhere to learn it was
  admitted; `position.lua` now falls through to this key before answering `unknown_token`.
  Self-limiting: each key carries the pass's own 60s TTL.
- **`GET /position` gained an `admission` object** when `state` is `admitted`.

**Known gap, accepted for v0 and logged in `decisions.md`:** the pop commits inside the script,
but signing happens in Python, so a crash between the two strands up to `batch_max` waiters. It
cannot be closed by making it atomic — HMAC is not available in Redis Lua.

**A live bug this phase found in its own config defaults:** with `burst=20` and `batch_max=50`,
`batch_max` **can never bind** — `n = min(floor(tokens), batch_max, queue_length)` and `tokens`
is itself capped at `burst`. The shipped default had a knob that silently did nothing.

### Phase 9 — The waiting room page

`frontend/index.html`, vanilla, polling. ~200 lines. Shows position, total, hedged estimate, the
fairness line and the honesty line (`product-spec.md` §6).
**Done when:** two browsers queue, see different positions, and are admitted in arrival order —
and refreshing visibly changes nothing (Journey B).

**Built 2026-08-02.** `frontend/index.html` (~150 lines, vanilla, 5s polling): the
join → poll → admitted → book state machine with the §7 client rules (position clamped, ETA
hedged, dimmed-not-blanked on disconnect, pass in memory, sold-out terminal). Served by the queue
service at `GET /` so join/position are same-origin (its `MIDDLEWARE=[]` cannot carry CORS); the
one cross-origin call, `POST /book`, is allowed by `django-cors-headers` on the booking service
(see `decisions.md`, 2026-08-02, Phase 9). Verified live: `GET / → 200 html`, `POST /join → 200`,
`OPTIONS /book → 200` with `Access-Control-Allow-Origin`. **Confirmed in-browser 2026-08-02:** the
full journey join → poll → admitted → book ran end to end at `http://127.0.0.1:8001/` and produced
a real `Booking #6`. The two-browser arrival-order view (fairness, F4) is a nice thing to eyeball,
but the mechanism it would show is already proven by the Phase 6–8 tests; the page's job — drive
the journey correctly — is done.

### Phase 10 — SSE

`StreamingHttpResponse` with an async generator, hand-rolled frames (CLAUDE.md Rule 5 —
this is learning surface, not plumbing). One subscriber task per process, bounded per-connection
`asyncio.Queue`, 15s heartbeat.
**Done when:** the page updates with no polling, and `INFO clients` shows **one** Redis
subscriber per process with 500 browsers connected.

**Built 2026-08-02.** `adapters/broadcaster.py` (one PSUBSCRIBE per process → per-connection
bounded `asyncio.Queue`s), `record_admissions` PUBLISHes on admission, an async-generator SSE view
(`retry`/`position`/`admitted` frames, 15s `: ping`, `X-Accel-Buffering: no`), and the frontend now
uses `EventSource`. Verified live (`text/event-stream` + `retry` + a `position` frame) and by
`tests/test_broadcaster.py` (one publish → five inboxes through one subscriber; admission
publishes). A real bug was found and fixed en route: the subscriber needs a DEDICATED pub/sub
client with no read timeout, or the shared client's 2s `socket_timeout` churns the subscription
(see `decisions.md`). **Pending:** the "one subscriber with 500 browsers" scale check is a Phase 14
load-test concern, not yet run.

### Phase 11 — Position arithmetic and reconciliation

Switch from per-client `ZRANK` to `my_seq − admitted`, with periodic `ZRANK` reconciliation and
the upward-correction clamp (FR-7).
**Done when:** `INFO commandstats` shows Redis ops flat from 1K to 10K connections — the
measurement that makes the whole design worth having.

**Built 2026-08-02.** The arithmetic (`seq - admitted`) was already in Phase 10's stream; this
phase added the missing half: every `SSE_RECONCILE_SECONDS` (30s, `settings.py`) the loop re-checks
the authoritative ZRANK via `_reconcile` and re-pins `sequence`, and `core.state.clamp_position`
guarantees a correction is never shown moving the position up (FR-7). `tests/test_reconcile.py`
pins both invariants; `check` + broadcaster tests green; stream re-smoked live. **Pending:** the
flat-Redis-ops-1K→10K measurement is the Phase 14 load test, not yet run.

### Phase 12 — Docker Compose

Both services, Redis 7, Postgres 16 (pinned to match production — the dev box runs 18, a parity
gap already logged), Caddy with `proxy_buffering off`.
**Done when:** `make up` gives a working system on a clean machine.

**Built 2026-08-02.** `docker-compose.yml` (Redis 7, Postgres 16, queue, booking, Caddy),
`Dockerfile`s for both services (`gunicorn` via a `sys_platform != "win32"` marker), a `Caddyfile`
serving the frontend static and proxying both APIs on one origin (`:8080`, `flush_interval -1` for
SSE), a `Makefile`, and `.dockerignore`/`.env.example`. Verified live: all five containers boot
healthy, and the full **join → admit → book** loop runs through Caddy on one origin and persists a
booking in the containerised Postgres. Because it is one origin, the Phase 9 CORS/serve-the-page
hacks are not needed here (kept for the non-Docker local flow). **Pending:** clean-machine test —
verified on the dev box, not yet on a fresh clone.

### Phase 13 — Prometheus and Grafana

prometheus-client in multiprocess mode; the series in §3.1; one dashboard: queue depth,
admission throughput, SSE connections, end-to-end wait.
**Done when:** the dashboard shows a full drop from first join to last booking.

**Built 2026-08-02.** `prometheus-client` in multiprocess mode (`adapters/metrics.py`,
`gunicorn.conf.py`, `PROMETHEUS_MULTIPROC_DIR` in the queue Dockerfile), an aggregating `/metrics`
endpoint, and the series `qf_queue_depth` / `qf_sse_connections` / `qf_admissions_total` /
`qf_admission_batch_seconds` / `qf_position_drift_total` instrumented at the adapter + view layer
(core/ stays clean). Compose adds Prometheus (scrapes `queue:8001`) and Grafana (:3000, anonymous,
datasource + dashboard provisioned). Verified: the admitter's counter reaches a web worker's
/metrics, Prometheus target `up`, metric queryable, Grafana healthy. **Pending:** watching a full
drop render is a manual eyeball once Phase 14 drives load; `qf_redis_command_seconds` deferred.

### Phase 14 — k6 and the load test report

The numbers. Raise `ulimit -n` and `somaxconn` **first** (CLAUDE.md §8). Every run recorded in
[`loadtest-report.md`](loadtest-report.md) with its command and its raw output.
**Done when:** `design.md` §13's table has real numbers instead of ⏳ — and
[`resume-claims.md`](resume-claims.md) can be updated with numbers that actually happened.

### Phase 15+ — v2, pick three not all

Horizontal scaling behind Nginx (sticky vs stateless — `design.md` §9 already argues it) ·
Redis Sentinel failover during a live load test · dynamic backpressure keyed to booking p99 ·
abuse mitigation where reconnecting cannot improve position.

---

## 7. Frontend integration

### Screen → call map

| Moment | Call |
|---|---|
| Page load | `POST /api/queue/{event}/join` |
| Waiting (v0) | `GET /api/queue/{event}/position` every 5s |
| Waiting (v1) | `new EventSource('/api/queue/{event}/stream?t=…')` |
| Admitted | `POST /events/{event}/book` with `Authorization: Bearer <pass>` |

### Rules the client must follow

1. **Send the queue token on every call.** Losing it means losing the place — there are no
   accounts to recover it from.
2. **Never re-join to "refresh".** `POST /join` is idempotent, but the position endpoint is the
   right call; re-joining as a refresh mechanism is how a future bug becomes a fairness bug.
3. **Never render the estimate as a countdown.** It is an estimate and the rate changes on
   purpose (rule 21).
4. **Never blank the position on disconnect.** Dim it and show "Reconnecting…". A blank number
   reads as a lost place (Journey C).
5. **Never show position increasing.** The server clamps this, but the client keeps the last
   value too — belt and braces on the promise users care most about.
6. **Let `EventSource` reconnect itself.** Do not write reconnection logic; the browser's is
   correct and the `retry:` directive tunes it.
7. **Store the pass in memory only.** It lives 60 seconds; persisting it to `localStorage` gains
   nothing and leaves a credential lying around.
8. **On a 401 at booking, go back to the queue.** The pass expired; the honest UI is "your turn
   passed, you are back in line", not an error page (Journey D).

### The waiting-room state machine

```
JOINING  --200-->            WAITING     store queue_token + sequence
WAITING  --position frame--> WAITING     update the number (never upward)
WAITING  --admitted frame--> ADMITTED    stream closes; hold pass in memory
WAITING  --stream error-->   RECONNECTING  keep last number on screen, dimmed
RECONNECTING --open-->       WAITING     first frame is absolute truth
ADMITTED --book 201/200-->   BOOKED
ADMITTED --book 401-->       EXPIRED     window missed; offer to rejoin at the back
ADMITTED --book 409-->       SOLD_OUT    terminal; do NOT send them back to a queue
```

`SOLD_OUT` is terminal on purpose. Returning someone to a queue for a thing that no longer exists
is the cruellest possible interaction, and it is the default if you are not thinking.

---

## 8. Validation rules

| Field | Rule | On failure |
|---|---|---|
| `event_id` (path) | Slug, ≤64 chars, must exist in Redis config | 404 `unknown_event` |
| `t` / cookie token | 32 lowercase hex chars | 404 `unknown_token` — deliberately *not* 400: a malformed and an unknown token are the same thing to a client, and distinguishing them leaks whether a token exists |
| `rate_per_min` | Integer 0–100000 | 400 `validation_error` |
| `burst` | Integer 0–10000 | 400 `validation_error` |
| `batch_max` | Integer 1–1000 | 400 `validation_error` |
| `X-Operator-Key` | Compared with `hmac.compare_digest` — never `==`, which is timing-variable | 401 `bad_operator_key` |
| `Authorization` | `Bearer <jwt>`, HS256, unexpired, `event_id` matching the URL | 401 / 403 |

**Deliberately not validated:** queue length (unbounded by design — `product-spec.md` rule 1),
number of events, how long a waiter stays connected.

---

## Change log

| Date | Change |
|---|---|
| 2026-08-02 | Created. Eight endpoints with exact shapes; SSE frame format including why there is no `id:`/`Last-Event-ID`; per-operation Redis round-trip budget; 15 phases with Phase 4 added because Phase 3's tests do not prove concurrency-safety despite looking like they do. |
| 2026-08-02 | **Phase 6 corrections, all found by implementing §3.1 rather than by re-reading it.** §1: the queue-token cookie is isolated by **name** (`qf_{event_id}`) at `Path=/`; the previous "scoped to the event path" contradicted §3.1's own `Set-Cookie` line. §2: `eta_seconds` is **nullable** and rounds **up** — a paused drop (`rate_per_min: 0`) has no honest estimate and must not report `0`. §3.1 `/join`: documented `405` and `503`, that a malformed *event id* returns `404 unknown_event`, and that a malformed *queue token* is replaced rather than rejected — the one deliberate exception to §8's table, because join is the front door. |
