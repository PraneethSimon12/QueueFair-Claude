# QueueFair — How it works, end to end

This traces **one person's journey** through both services — page load → wait → admitted → booked —
naming the file, the function, and the Redis/Postgres operation at each step. It is the *narrative*
that ties the other docs together:

- **Why** each piece is shaped this way → [`decisions.md`](decisions.md)
- The **hard problems** (fairness, position, admission) in depth → [`design.md`](design.md)
- The **exact** request/response for every endpoint → [`build-plan.md`](build-plan.md) §3

Nothing here is restated from those; when it matters, it links.

---

## The shape

Two services, one origin (behind Caddy in Docker):

```
browser ──> Caddy (:8080) ──> /              -> static waiting-room page (frontend/index.html)
                          ├──> /api/queue/*   -> QUEUE service   (async Django/ASGI, Redis only)
                          └──> /events/*       -> BOOKING service (Django/DRF, PostgreSQL)

QUEUE service state:  Redis         BOOKING service state:  PostgreSQL
admission loop:       run_admitter (its own process, no leader)
observability:        /metrics -> Prometheus -> Grafana
```

**Two tokens, never confused** (build-plan §1):

| | Queue token | Admission pass |
|---|---|---|
| What | 32 hex chars, opaque | HS256 JWT |
| Issued by | queue service, on **join** | queue service, on **admission** |
| Means | "my place in line" | "I waited, it's my turn" |
| Lifetime | the whole drop | **60 seconds** |
| Verified by | queue service (Redis lookup) | booking service (local HMAC, no lookup) |

---

## Step 1 — Join the queue

`POST /api/queue/{event}/join`

- **Browser** (`frontend/index.html` → `join()`): fires once on page load. Never re-fired to
  "refresh" — that is what the position stream is for.
- **Queue** (`api/views.py::join`): validates the event id, reads the queue token from the cookie
  `qf_{event}` or `?t=` (or mints a fresh one), then calls the repository.
- **Redis** (`adapters/queue_repository.py::join` → `lua/join.lua`): **one atomic script** does
  `ZADD NX` (place in the sorted set, score = arrival sequence) **and** `INCR` the sequence counter
  — but only when the token is new, so a double-tapped join gets its *existing* place back and
  burns no sequence number. This is the fairness core (`decisions.md`, "Phase 6: join is one Lua
  script"; the race it prevents is `design.md` §5).
- **Response**: `queue_token`, `sequence`, `position`, `total_waiting`, `joined`. Sets an
  `HttpOnly` cookie so a refresh keeps the place. `joined` is the only field a refresh changes.

## Step 2 — Watch your position (SSE)

`GET /api/queue/{event}/stream?t=<token>`

- **Browser**: opens an `EventSource`; the browser reconnects on its own.
- **Queue** (`api/views.py::stream` → `_event_stream`, an async generator): sends `retry: 3000`, a
  first `position` frame, then updates. Position is **arithmetic**: `position = my_sequence −
  admitted_total`, recomputed in memory from each broadcast — **zero Redis per tick per client**
  (`design.md` §6; `decisions.md`, "position and eta are computed in core/").
- **Fan-out** (`adapters/broadcaster.py`): **one** Redis `PSUBSCRIBE` *per process*, not per
  connection, feeding each connection's own bounded `asyncio.Queue`. This is the #1 mistake §8
  warns about, avoided (`decisions.md`, "Phase 10: SSE via one per-process subscriber").
- **Keeping it honest**: a `: ping` every 15s stops proxies dropping the connection; every 30s the
  stream re-checks the authoritative `ZRANK` and re-pins the sequence to correct abandonment drift,
  and a clamp guarantees the *shown* position never moves up (`decisions.md`, "Phase 11:
  reconciliation… the displayed position is clamped").

## Step 3 — Get admitted

`run_admitter <event>` — a background process (as many as you like, on any box)

- **Queue** (`core/admission.py` → `adapters/queue_repository.py::admit_batch` → `lua/admit_batch.lua`):
  **one atomic script** refills a token bucket, `ZPOPMIN`s that many waiters off the front, and
  increments the admitted counter. Because the rate limit *and* the pop are one atomic step, every
  process can run this loop safely — **no leader, no lock** (`design.md` §7; `decisions.md`, "Phase
  8: the clock is injected").
- **Issue + park + announce** (`record_admissions`): signs an HS256 pass per admitted waiter, parks
  it at `qf:{event}:pass:{token}` with a 60s TTL, and `PUBLISH`es the new `admitted_total` on the
  event channel.
- **Delivery**: the per-process subscriber receives that publish and fans it out; the waiter's SSE
  loop sees `sequence ≤ admitted_total`, fetches the parked pass, and sends a final `admitted`
  frame — then closes. (A polling client finds the same pass via `GET /position`.)

## Step 4 — Book

`POST /events/{event}/book` — header `Authorization: Bearer <pass>`

- **Browser**: on the `admitted` frame, POSTs to the booking service (same origin behind Caddy).
- **Booking** (`bookings/authentication.py`): a DRF auth class verifies the HS256 pass **locally**
  — recompute the signature with the shared secret, check `exp`, pin `algorithms=["HS256"]`. No
  call back to the queue service; the shared secret is the whole trust boundary (`decisions.md`,
  "Admission-token verification: PyJWT, algorithm pinning").
- **Claim + record** (`bookings/booking.py`): in one transaction, an **atomic conditional UPDATE**
  claims a ticket (`... SET tickets_booked = tickets_booked + 1 WHERE tickets_booked < capacity` —
  0 rows means sold out, no race) and inserts the `Booking`. Two DB unique constraints make it
  idempotent: one token → one booking, one user → one booking per event (`decisions.md`, "Booking
  integrity enforced by the database").
- **Response**: `201` new, `200` idempotent replay, `401` bad/expired pass, `403` wrong event,
  `409` sold out.

---

## Where the state lives

**Redis** (per event; `core/keys.py`):

| key | type | holds |
|---|---|---|
| `qf:{e}:queue` | sorted set | member = queue token, score = arrival sequence |
| `qf:{e}:seq` | counter | next arrival sequence |
| `qf:{e}:admitted` | counter | total ever admitted — what makes O(1) position possible |
| `qf:{e}:bucket` | hash | token-bucket state (written only inside Lua) |
| `qf:{e}:config` | hash | rate_per_min, burst, batch_max — *its existence = "event exists"* |
| `qf:{e}:events` | channel | pub/sub admission announcements |
| `qf:{e}:pass:{token}` | string (TTL) | the one per-*waiter* key — a parked admission pass |

**PostgreSQL** (booking service): `bookings_event` (capacity + `tickets_booked` counter), and
`bookings_booking` (the ticket, with the two unique constraints).

---

## The three races this whole project is about

Each is demonstrated *failing* against a deliberately broken implementation, then holding:

1. **Queue-jumping on join** — a double-tapped `GET→modify→SET` hands one person several sequence
   numbers and a worse place. Fixed by `join.lua`. (`design.md` §5; `tests/test_join.py`)
2. **Over-admission** — check-then-act across processes releases too many. Fixed by
   `admit_batch.lua`. (`design.md` §7; `tests/test_admit_batch.py`)
3. **Oversell on book** — two requests both read "99 of 100" and both insert. Fixed by the atomic
   conditional UPDATE. (`decisions.md`, "Inventory as a denormalized counter";
   `bookings/test_concurrency.py`)

The common lesson: correctness that must survive concurrency lives in **one atomic operation** —
a Lua script in Redis, or a single conditional UPDATE in Postgres — never in read-modify-write
application code.
