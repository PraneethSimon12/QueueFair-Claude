# Decision log

Running log of every non-obvious choice. ~5 lines each: what we chose, what we rejected, why,
and what would make us revisit. This file is the raw material for the design doc and for
interview prep — the "Alternatives Considered" section is worth more than the code.

For the runtime *narrative* these choices add up to, see [`flow.md`](flow.md).

---

## Index

A navigation aid, grouped by area (entries stay chronological below). Ctrl-F the title to jump.

**Booking service — data, integrity, auth (Phases 0–4, D1)**
- SQLite for v0 *(superseded)* · PostgreSQL now
- Django-native settings (not Pydantic) · Dedicated project venv
- Booking scope: real + inventory/oversell (deviation from §1)
- Event identified by a slug natural key
- Inventory as a denormalized counter (not COUNT(*))
- Booking integrity enforced by the database (dual idempotency + PROTECT)
- Admission-token verification: PyJWT, algorithm pinning, required secret
- Booking endpoint (D1): DRF auth class, atomic-UPDATE claim, idempotent replay
- Tokens issued only by the queue service (dev minting is a CLI command)
- Phase 4: concurrency proved with threads at the function level

**Queue service — framework & foundations (Phase 5)**
- Queue service on async Django (ASGI), not FastAPI
- Phase 5: the queue service's constraints are asserted, not just commented
- Redis client: one lazy singleton per process
- Three things Django does that the Phase 5 comments got wrong

**Fairness, position & admission (Phases 6–8)**
- Phase 6: join is one Lua script (naive version kept as a test)
- Phase 6: no QueueRepository Protocol yet, deliberately
- Phase 6: position and eta are computed in core/
- Phase 7: /position is one Lua script, for atomicity not speed
- Phase 7: two position types ("computed" vs "measured")
- Phase 8: core/ports.py exists now (the Phase 6 prediction settled)
- Phase 8: the clock is injected, and it is wall-clock, not monotonic
- Phase 8: qf:{event}:pass:{token} — the first per-waiter key
- Phase 8: known gap — the pop/sign window is not atomic, and cannot be
- Phase 8 found a dead config knob: batch_max below burst

**Frontend, SSE & reconciliation (Phases 9–11)**
- Phase 9: the queue service serves the one static page; CORS lives on booking
- Phase 10: SSE via one per-process subscriber fanning out to in-memory queues
- Phase 11: reconciliation re-pins the sequence; the displayed position is clamped

**Infrastructure & observability (Phases 12–13)**
- Phase 12: Docker Compose, one origin via Caddy (the CORS hacks fall away)
- Phase 13: metrics in multiprocess mode, Prometheus + Grafana

**v2 — dynamic backpressure, abuse mitigation & horizontal scaling**
- Dynamic backpressure: an AIMD controller that tunes the admission rate to booking p99
- Abuse mitigation: a config-gated per-client join rate limit, and the XFF trap
- Horizontal scaling: load-balance across replicas with Caddy dynamic upstreams

**Cross-cutting & findings**
- A five-document spec set, with a claim-to-evidence ledger
- Finding: localhost→Redis connect takes ~2s here, at the connect-timeout edge
- Phase 14 first run: a hand-rolled asyncio load client, and a local floor

---

## 2026-07-18 — SQLite for the v0 booking service (not PostgreSQL)

> **Superseded same day** by "PostgreSQL now" below — we committed to a persisted `Booking`
> model with inventory, which is exactly the "revisit when" trigger this entry named. Kept for
> the record, because the *reasoning* (defer infra until something needs it) is still sound.

- **Chose:** SQLite (Django default) for the booking service in v0.
- **Rejected:** PostgreSQL + Docker from day one (what the target stack in §2 lists).
- **Why:** In v0 the booking service is a mock — verify an admission token, sleep ~100ms,
  return a fake ticket. It persists nothing, so a DB server adds setup plus Docker (which v0
  explicitly excludes) for zero benefit right now.
- **Revisit when:** we add a persisted `Booking` model. That is where the interesting material
  lives — idempotency (one token → at most one booking), unique constraints, and behaviour
  under concurrent bookings — and where PostgreSQL earns its place over SQLite.

## 2026-07-18 — Django-native settings for the booking service (not Pydantic Settings)

- **Chose:** Django's own `settings.py` convention (env vars via `os.environ`, `django-environ`
  later if needed) for the booking service.
- **Rejected:** forcing the queue service's "one Pydantic Settings object" pattern (§4) onto
  Django.
- **Why:** §4's Pydantic Settings rule is the FastAPI/queue-service idiom. Django already has a
  mature settings mechanism; bolting Pydantic on top is more code to explain, not less. Keep
  each service idiomatic to its framework.
- **Revisit when:** the two services ever need to share a config schema — unlikely at our scale.

## 2026-07-18 — Dedicated project venv (not the conda base env)

- **Chose:** a project-local `.venv` created from system Python 3.12 (`py -3.12 -m venv .venv`).
- **Rejected:** installing into the existing miniconda `base` env, which already had Django 5.1.
- **Why:** dependencies installed in `base` leak across every project on the machine and make
  `requirements.txt` meaningless. Isolation is what makes the environment reproducible.
- **Revisit when:** never, for this project — standard practice.

## 2026-07-18 — Booking service scope: real + inventory/oversell (conscious deviation from §1)

- **Chose:** the booking service persists real `Booking` rows, validates real HS256 admission
  tokens, enforces idempotency (one token → at most one booking) and a fixed per-event ticket
  capacity with concurrency-safe oversell protection.
- **Rejected:** (a) keeping it a pure sleep+return mock, as §1 literally specifies; (b) the
  opposite extreme — a full consumer product with seat selection, payments, accounts, a real UI,
  always-on hosting.
- **Why:** the pure mock teaches nothing on the booking side; the real-but-bounded version makes
  the booking service a genuine lesson in idempotency and oversell-under-concurrency (the classic
  DB race) without stealing time from the queue, which stays the star of the project.
- **The line we will NOT cross:** no seats, no payments, no user accounts, no real UI, not
  always-on. §1's warning still binds: "if we ever spend a week on the booking service, something
  has gone wrong."
- **Revisit when:** booking work starts crowding out queue-service work — then we freeze it.

## 2026-07-18 — PostgreSQL now (supersedes the SQLite-for-v0 decision above)

- **Chose:** connect the booking service to PostgreSQL immediately, using the native PostgreSQL
  18 server already running on the dev machine (no Docker locally for v0).
- **Rejected:** SQLite until later (previous decision); standing up Postgres in a Docker
  container now.
- **Why:** we committed to a persisted `Booking` model with a unique constraint and
  concurrency-safe inventory — exactly the trigger the SQLite entry named. And since Postgres is
  already running natively, connecting is cheap and keeps v0 Docker-free.
- **Note:** dev machine runs PG 18; §2 targets PG 16 — a dev/prod parity gap we accept for now
  and will close by pinning the Docker image to match later.
- **Revisit when:** n/a — this is the intended backend.

## 2026-07-18 — Event identified by a slug natural key (not a surrogate int)

- **Chose:** `Event.event_id` is a `SlugField` primary key (e.g. `coldplay-mumbai-2026`).
- **Rejected:** an auto-increment integer PK plus a separate unique slug.
- **Why:** both the queue and booking services refer to an event as a string; a shared readable
  key means one vocabulary, no int↔slug translation step, and legible URLs and logs. The id is
  one we mint and never change, so the usual "natural keys mutate" objection does not bite.
- **Revisit when:** we ever need to rename an event id (painful with a natural PK) — unlikely.

## 2026-07-18 — Inventory as a denormalized counter (not COUNT(*))

- **Chose:** a `tickets_booked` counter column on `Event`. Step D books via one atomic statement:
  `UPDATE ... SET tickets_booked = tickets_booked + 1 WHERE tickets_booked < capacity`.
- **Rejected:** no counter — compute inventory with `COUNT(*)` of bookings, guarded by
  `SELECT ... FOR UPDATE` on the event row.
- **Why:** the single conditional UPDATE does check-and-increment atomically (row-level lock
  serialises concurrent writers), so it is race-proof with no explicit locking and stays fast
  under a stampede. `COUNT(*)` + `FOR UPDATE` is correct but needs ~4 statements per booking and
  funnels every booking through one lock.
- **Tradeoff accepted:** the counter is denormalized (could drift from `COUNT(*)` if code is
  buggy). Mitigations: the counter increment and the booking insert happen in one transaction
  (Step D), and a `CheckConstraint (tickets_booked <= capacity)` is a hard DB-level backstop.
- **Revisit when:** we need per-seat or multi-ticket-per-booking semantics — the model changes.

## 2026-07-18 — Booking integrity enforced by the database (dual idempotency + PROTECT)

- **Chose:** two UNIQUE constraints — `token_jti` (request-level: one admission token → at most
  one booking) and `(event, user_id)` (business-level: one booking per user per event) — plus
  `on_delete=models.PROTECT` on the Booking→Event FK. All enforced by Postgres, not app-code.
- **Rejected:** enforcing "no double booking" only with Python `if` checks; a single idempotency
  key; Django's default `on_delete=CASCADE`.
- **Why:** the two unique keys stop genuinely different failures (a replayed/retried token vs a
  user re-admitted with a fresh token), and a constraint is the only place the rule cannot be
  raced past. `PROTECT` stops an event deletion from silently cascading away its bookings —
  transaction records should never vanish as a side effect.
- **Consequence accepted:** a user can never hold two bookings for one event — intended for a
  ticket drop.
- **Revisit when:** a legitimate flow needs multiple tickets per user per event.

## 2026-07-18 — Admission-token verification: PyJWT, algorithm pinning, required secret

- **Chose:** verify HS256 admission tokens with **PyJWT**, pinned to `algorithms=["HS256"]`; the
  verifier (`bookings/tokens.py`) is a pure function that takes the secret as an argument;
  `ADMISSION_TOKEN_SECRET` is a **required** setting with no fallback default.
- **Rejected:** hand-rolling JWT verification (~60 lines of stdlib hmac/base64); reading the
  secret from `settings` inside the verifier; giving the secret a dev fallback.
- **Why:** JWT verification is security-critical — a naive verifier is open to `alg: none` and
  algorithm-confusion attacks that give a total auth bypass; PyJWT + a pinned algorithm is
  battle-tested against them. Passing the secret in keeps the verifier pure and unit-testable
  with no Django or DB (Dependency Inversion — the test run literally skips database setup). A
  guessable secret default would let anyone forge tokens, so a missing value must crash loudly.
- **Contrast with §5:** the SSE response and token bucket are deliberately hand-rolled (learning
  surface, low blast radius). JWT verification is the opposite — a bug is a security hole — so we
  take the dependency.
- **Revisit when:** we need multiple issuers (add `iss`/`aud` claims) or signing-key rotation.

## 2026-07-18 — Booking endpoint (D1): DRF auth class, atomic-UPDATE claim, idempotent replay

- **Chose:** a **DRF authentication class** (`AdmissionTokenAuthentication`) attached to the view;
  the **atomic conditional UPDATE** to claim a ticket (`rows`-affected = got-it / sold-out); an
  **idempotent replay** that returns the existing booking with `200`; status map
  `201/200/401/403/404/409`.
- **Rejected:** inline token verification in the view (mixes concerns); **global Django
  middleware** (runs on every URL, needs path hacks, doesn't integrate with DRF's request);
  `select_for_update` (a held lock is overkill for a simple bounded increment); erroring on replay.
- **Why:** the auth class scopes verification to the endpoint and keeps the view HTTP-only (SRP);
  the atomic UPDATE is race-proof with no held lock; `200`-on-replay makes the endpoint safe to
  retry. `authenticate_header()` is implemented deliberately so DRF returns **401** (not its
  default **403**) on auth failure.
- **Note (honesty):** D1's tests exercise the endpoint *sequentially*; they prove correctness and
  idempotency but NOT oversell-safety under true concurrency — that is D2's job (a real parallel
  load test), and will likely need a live server + `TransactionTestCase`, since `APITestCase`
  wraps each test in a rolled-back transaction.
- **Revisit when:** many endpoints need auth (promote to a shared default) or we need richer
  authorization (permission classes).

## 2026-07-28 — Tokens are issued only by the queue service (gated); dev minting is a CLI command

- **Considered:** an HTTP endpoint that returns an admission token (convenient for Postman/tests).
- **Rejected:** any ungated token-issuing endpoint, and issuing tokens from the booking service.
- **Why:** the admission token is *proof of controlled admission* — QueueFair's whole value is
  that tokens are scarce and issued ONLY by the queue service, ONLY after a user has waited in the
  FIFO queue and been released by the admission controller at a controlled rate. A free "give me a
  token" endpoint lets anyone skip the queue and book directly: a total bypass of the system's
  reason to exist, and a security hole. The booking service VERIFIES tokens (trust boundary,
  Step C); it must never ISSUE them.
- **For dev/testing** we use a `mint_token` management command: it needs shell access (only the
  operator can run it), has zero network attack surface, and can't be accidentally left exposed
  the way an endpoint could.
- **Revisit when:** the queue service is built — it exposes token issuance, but as the final,
  gated step of admission, never a free endpoint.

## 2026-08-02 — Queue service on async Django (ASGI), not FastAPI

- **Chose:** the queue service is **async Django 5 on ASGI** — bare `async def` views, an empty
  `MIDDLEWARE` list, no DRF on the hot path, no `DATABASES`. Uvicorn/uvloop under Gunicorn.
- **Rejected:** FastAPI + Starlette (what CLAUDE.md §2 originally specified); Django Channels
  (a WebSocket/consumer framework — we need SSE, which is plain HTTP streaming and needs none
  of its machinery); Go (already settled, see §2).
- **Why:** one framework across both services means one settings idiom, one test runner, one
  mental model, and one deployment story — real value on a solo project where the scarce
  resource is my attention, not CPU. Django 4.2+ supports async iterators in
  `StreamingHttpResponse`, which is all SSE actually requires. And the framework is not the
  bottleneck: per connection we hold one `asyncio.Queue` and one socket, and per position update
  we do arithmetic in memory — the costs are Redis round-trips and per-connection memory, and
  both are framework-independent.
- **What we are giving up — say this out loud, do not hide it:** Django carries more
  per-request overhead than Starlette (middleware chain, `HttpRequest` construction, settings
  resolution). On the position endpoint that is a measurable tax we have not yet measured.
- **The footgun this buys, and the mitigation:** Django adapts sync↔async at the middleware
  boundary. **One non-`async_capable` middleware forces every request through
  `sync_to_async` and therefore through the ASGI thread pool** — which, with thousands of open
  SSE connections, means thousands of parked threads and a service that dies well before its
  connection target. Mitigation: ship `MIDDLEWARE = []`, forbid the ORM by having no
  `DATABASES` at all, and make this the first thing any load test would expose. Logged as a
  trap in CLAUDE.md §8.
- **Honesty note:** this decision was made partly to match a resume line that already said
  "async Django (ASGI)". That is a legitimate reason to *pick between two defensible options* —
  it is not a reason to claim anything unbuilt. See `docs/resume-claims.md`, which tracks every
  claim against its evidence.
- **Revisit when:** a load test shows Django's per-request overhead — not Redis, not memory — is
  the binding constraint on connection count or p99. Then we port the SSE endpoint alone to
  Starlette and keep Django for the rest, and this entry gets superseded with the number that
  forced it.

## 2026-08-02 — A five-document spec set, with a claim-to-evidence ledger

- **Chose:** split the docs by the question they answer — `product-spec.md` (what), `design.md`
  (why), `build-plan.md` (how + wire contract), plus `resume-claims.md` (claim → evidence) and
  `loadtest-report.md` (the numbers). `decisions.md` and `interview-prep.md` continue unchanged.
  Precedence when they disagree: **behaviour > design > wire format.**
- **Rejected:** (a) one large `design.md` holding everything — it becomes unreadable and nobody
  can tell which part is settled behaviour and which is an implementation sketch; (b) keeping
  ADRs inside `design.md` as well as in `decisions.md` — two copies of an ADR is exactly how a
  spec goes stale, so `design.md` **links** to decision-log entries and never restates them.
- **Why `resume-claims.md` exists at all:** CLAUDE.md Rule 7 says never put a number on the
  resume we have not reproduced. That rule had no artifact enforcing it, so the resume drifted
  ahead of the repo by roughly a whole version. The ledger makes the gap visible and gives every
  claim a status (SHIPPED / MEASURED / PLANNED / UNSUPPORTED) and a named piece of evidence.
- **Revisit when:** never for the split itself; the ledger gets revisited every time a phase
  lands or a load test runs — that is its whole purpose.

## 2026-08-02 — Phase 4: concurrency proved with threads at the function level, not over HTTP

- **Chose:** a `TransactionTestCase` (`bookings/test_concurrency.py`) that releases 60 threads
  from a `threading.Barrier` straight into `book_ticket()`, each on its own DB connection.
- **Rejected:** (a) `LiveServerTestCase` + real HTTP — a real server and 60 sockets to exercise
  the same DB race, seconds slower, testing nothing extra about the race itself; (b) staying on
  `APITestCase`, which is *structurally incapable* of this: it never commits, so a second
  connection cannot see `setUp`'s data at all.
- **Why the harness is shaped this way:** `_race()` takes the implementation as a parameter
  (`BookAttempt`), so the same harness drives the real code and the deliberately broken one —
  Dependency Inversion, and the only reason the mutation proof is cheap. Workers *record*
  outcomes instead of asserting, because an assertion raised in a worker thread does not fail
  the test, it prints to stderr and the run reports green. Connections open **before** the
  barrier (connecting costs milliseconds, the `UPDATE` costs microseconds — connect after the
  barrier and there is no collision) and close in a `finally` (a leaked thread-local connection
  blocks the teardown `TRUNCATE` on its `ACCESS EXCLUSIVE` lock, forever).
- **The finding that shaped the assertions:** the broken read-modify-write produced
  `tickets_booked=1` with **60 booking rows**, and `CheckConstraint (tickets_booked <= capacity)`
  was satisfied the whole time. A lost update writes a *stale* value, not an *illegal* one, so no
  database constraint can catch it. The test therefore asserts `COUNT(bookings) == capacity`
  **and** `tickets_booked == capacity` — asserting only the counter passes against the broken
  code. This is the denormalization risk accepted in the 2026-07-18 counter entry, observed.
- **Scope added deliberately:** a third test replays one token from 10 threads. The
  `except IntegrityError` branch in `booking.py` is by its own comment only reachable
  concurrently, so before this it had never once executed.
- **Revisit when:** `ATOMIC_REQUESTS` is turned on, or booking grows multi-statement logic — both
  change what the request-level transaction boundary is, and then the HTTP-level test we rejected
  starts earning its cost.

## 2026-08-02 — Phase 5: the queue service's constraints are asserted, not just commented

- **Chose:** `queue_service/tests/test_invariants.py` — executable assertions for `MIDDLEWARE == []`,
  the absence of a usable database, the absence of a WSGI entry point, and `core/` importing
  nothing from `adapters/`, `redis`, `django.db` or `django.http` (checked by parsing the AST of
  every file under `core/`, so it reports the exact line and works on code that would not import).
- **Rejected:** relying on the comments in `settings.py` and on CLAUDE.md §8. A comment saying
  "do not add middleware" is a suggestion; the thing it guards is a silent, load-only failure
  that no ordinary test would catch. The boundary test in particular was shown to fail on demand.
- **Why it matters more here than usual:** every one of these is invisible when wrong. Adding a
  sync middleware does not break a single functional test — it breaks the service at 2,000
  connections, in production, months later.
- **Revisit when:** never as a category; each assertion is revisited only by the change that
  breaks it, and the correct response is to justify the change, not to update the assertion.

## 2026-08-02 — Redis client: one lazy singleton per process (and what the event loop does to it)

- **Chose:** a module-level singleton in `adapters/redis_client.py`, built on first call to
  `get_redis()`, never at import; `decode_responses=True`; bounded `socket_timeout` and
  `socket_connect_timeout` (2s); an explicit `close_redis()`.
- **Rejected:** a client per request (a second connection pool per request — the exact
  per-client cost this service exists to avoid); constructing eagerly at module import;
  unbounded socket timeouts.
- **Why lazy, specifically:** a redis-py pooled connection is bound to the event loop that
  created it and cannot be used or even closed from another one. Constructing eagerly at import
  — before `UvicornWorker` forks and makes its loop — would bind the pool to the wrong loop.
  Because construction opens no socket and the first command always runs on the worker's own
  loop, each process ends up with a pool bound to the only loop it will ever have.
- **How we learned it:** the first version of the integration tests failed with
  `RuntimeError: Event loop is closed`. Django runs each `async def test_` on `SimpleTestCase`
  through `async_to_sync`, which creates a **fresh loop per test**, so a `tearDown` doing
  `asyncio.run(close_redis())` tries to close transports belonging to a dead loop. Fix: those
  tests use `unittest.IsolatedAsyncioTestCase`, whose `asyncTearDown` runs in the test's own loop.
- **Why bounded timeouts:** an unbounded wait on a dead Redis is not an error, it is a request
  that never returns while holding its connection. A health check that hangs is strictly worse
  than one that fails, because it is indistinguishable from one that is passing.
- **Revisit when:** we need a separate client for pub/sub (Phase 10 — a subscribed connection
  cannot serve normal commands, so the fan-out subscriber will need its own).

## 2026-08-02 — Three things Django does that the Phase 5 comments originally got wrong

Logged because each was written down confidently, then disproved by running it — and a spec known
to be wrong is worse than none (`docs/index.md`).

- **`DATABASES = {}` does not mean "no connection."** `ConnectionHandler.configure_settings`
  injects a `default` alias backed by `django.db.backends.dummy`. The lookup succeeds; the first
  *query* raises `ImproperlyConfigured`. The loud-failure guarantee holds, but via the dummy
  backend, not via an absent connection.
- **`settings.DATABASES` is mutated in place** the first time anything touches `connections`, so
  `assertEqual(settings.DATABASES, {})` passes or fails depending on test ordering. The invariant
  test asserts the resolved `ENGINE` instead, which is order-independent.
- **`SimpleTestCase` installs its own query blocker.** `connections["default"].cursor()` inside
  one raises `DatabaseOperationForbidden` — Django's test harness, not our settings — so that
  version of the test passes even with a real PostgreSQL configured. The test uses
  `connections.create_connection("default")`, which is unpatched, to assert the real behaviour.
- **The general lesson:** an assertion that cannot fail for the reason you think it can is worse
  than no assertion. All three of these were green tests that proved nothing.

## 2026-08-02 — Phase 6: join is one Lua script, and the naive version is kept as a test

- **Chose:** `lua/join.lua`, invoked via `register_script` (EVALSHA, with redis-py handling
  NOSCRIPT reloads), returning everything the response needs — sequence, joined, `ZCARD`,
  `admitted`, `rate_per_min` — in **one** round trip, which is build-plan §5's budget.
- **Rejected:** check-then-act across three round trips (the race); `ZADD NX` alone.
- **Why `ZADD NX` is not enough, which is the non-obvious half:** it fixes fairness — the second
  `ZADD` becomes a no-op and the user keeps their score. It does **not** stop the second `INCR`,
  so every wasted call leaves a hole in the sequence. `design.md` §6 computes
  `position = my_seq − admitted`, which is only correct if sequences are dense, so gaps inflate
  every later waiter's displayed position permanently and invisibly. **The round trips are a
  performance argument; the gaps are a correctness argument, and the gaps decide it.**
- **Measured, not asserted** (1,000 joins of one token, ≤50 concurrent): naive → seq counter 50,
  50 distinct sequences issued to one person, surviving score 50. Scripted → seq counter 1, one
  sequence, no gaps. The naive implementation lives on in `tests/test_join.py::_naive_join` for
  the same reason Phase 4 keeps its read-modify-write mutant: a race test that has never failed
  has not been shown to test anything.
- **Revisit when:** never for join itself. The pattern — one script per mutation, invariant in a
  header comment, broken version retained as a test — is the template for `admit_batch.lua`.

## 2026-08-02 — Phase 6: no QueueRepository Protocol yet, deliberately

- **Chose:** a concrete `RedisQueueRepository` with no interface above it.
- **Rejected:** defining the `QueueRepository` Protocol that CLAUDE.md Rule 4 uses as its worked
  example of Dependency Inversion.
- **Why:** nothing in `core/` depends on the repository today — the view calls it directly and
  passes plain integers into `core.state`. An interface with one implementation and one caller
  is speculative generality (Rule 11), and it would make the DIP example a fiction rather than a
  demonstration. The boundary that actually earns its keep right now is the *import* boundary,
  and that one is enforced executably by `test_invariants.py`.
- **Revisit when:** Phase 8. The admission controller is real logic that must be unit-testable
  against an in-memory double, and that is the moment the Protocol stops being decoration.

## 2026-08-02 — Phase 6: `position` and `eta` are computed in core/, not in Lua or the view

- **Chose:** `join.lua` returns raw facts (sequence, admitted, ZCARD, rate); `core/state.py`
  turns them into `position`, `eta_seconds` and `state`; the view only serialises.
- **Rejected:** computing position inside the Lua script; computing it inline in the view.
- **Why:** Phase 11 moves position updates off Redis entirely — the SSE fan-out will recompute
  every waiter's position in memory from two integers it already holds. That is only possible if
  the arithmetic lives somewhere with no Redis in it. Putting it in Lua would weld the cheapest
  operation in the system to the one component we are trying to stop calling.
- **Two decisions inside it worth their own line:** `position` is clamped to ≥1 (a 0 would mean
  more people were admitted than were ever sequenced — a bug, not a number to show a user), and
  `eta_seconds` is `None` rather than `0` when the rate is 0, because a paused drop rendering as
  "any moment now" is worse than rendering as nothing.
- **Revisit when:** Phase 11, which will reuse these exact functions from the SSE loop — the test
  that this decomposition was right is whether that phase needs to change them at all.

## 2026-08-02 — Phase 7: `/position` is one Lua script, for atomicity rather than for speed

- **Chose:** `lua/position.lua` — `EXISTS` config, `ZRANK`, `ZCARD`, admitted, rate — in one
  script, returning a leading status code so the caller can tell `unknown_event` from
  `unknown_token` without parsing an error string.
- **Rejected:** the `ZSCORE` + `ZRANK` pair that build-plan §5 originally budgeted; a pipeline.
- **Why:** `ZRANK` alone answers both "am I queued" (nil if not) and "where", so `ZSCORE` was
  redundant. But the reason for a *script* is not the round-trip count. Read the rank and the
  admitted counter as separate commands and an admission batch can commit between them, so one
  response describes two different instants — and the resulting error can make a waiter's
  position go **up**, breaking the one promise (F4) that users notice immediately. A pipeline
  saves the round trips but not the interleaving; only a script does both.
- **Budget updated, not quietly beaten:** build-plan §5 now says 1 round trip for `/position`,
  with the reason. A budget that silently disagrees with the code is worse than no budget.
- **Revisit when:** Phase 11 adds reconciliation, which calls this same script on a timer.

## 2026-08-02 — Phase 7: two position types, because "computed" and "measured" must not blur

- **Chose:** `PositionOutcome` (authoritative, from `ZRANK`) as a separate type from
  `JoinOutcome` + `position_from()` (the cheap `my_seq − admitted` arithmetic), with two builder
  functions rather than one taking a flag.
- **Rejected:** one outcome type with a boolean, or reusing `position_from()` for both.
- **Why:** the difference between these two numbers is the entire subject of `design.md` §6, and
  Phase 11's reconciliation exists precisely because they can disagree. A boolean parameter would
  hide the distinction at exactly the call sites where it has to be legible.
- **The disagreement is now a test, not a claim:**
  `test_zrank_does_not_drift_when_a_waiter_abandons` removes a waiter from the middle of the
  queue without admitting them; `ZRANK` says 2, the arithmetic says 3. One abandonment, one
  permanent unit of drift, per affected waiter. That is the cost `design.md` §6 accepts in
  exchange for zero Redis calls per connected client, and it is now measured rather than asserted.
- **Revisit when:** Phase 11 — if reconciliation turns out to correct drift often, the
  abandonment rate is higher than §6 assumes and the tradeoff needs re-arguing with the number.

## 2026-08-02 — Phase 8: `core/ports.py` exists now, and the Phase 6 prediction is settled

- **Chose:** Protocols (`QueueRepository`, `PassIssuer`, `Clock`) in `core/ports.py`, with
  `AdmissionController` in `core/admission.py` depending only on them.
- **Why now and not in Phase 6:** the Phase 6 entry above said an interface with one
  implementation and one caller is decoration, and that the Protocol would arrive when something
  in `core/` genuinely needed it. This is that: `tests/test_admission.py` runs the entire
  admission controller — issuance order, TTLs, clock discipline, failure recovery — against
  in-memory fakes, with **no Redis, no Django and no wall clock**. That test file is the return
  on the abstraction, and it did not exist two phases ago.
- **Protocols, not ABCs:** the adapters never import `core.ports`, so there is nothing to
  subclass. Structural typing is what keeps the dependency arrow pointing inward.
- **Revisit when:** a second repository implementation appears (Phase 15's horizontal scaling
  work might want one) — the Protocol is already the seam it would slot into.

## 2026-08-02 — Phase 8: the clock is injected, and it is wall-clock, not monotonic

- **Chose:** `now_ms` passed into `admit_batch.lua` as ARGV and into `AdmissionController` as a
  `Clock` Protocol; the production implementation is `int(time.time() * 1000)`.
- **Rejected:** `redis.call('TIME')` inside the script; `time.monotonic()`.
- **Why not `TIME`:** it makes the script impure and non-deterministic under replication, and it
  makes "60 seconds at 100/min" untestable without actually waiting 60 seconds. With an injected
  clock that acceptance criterion is an **exact** assertion that runs in milliseconds.
- **Why wall-clock and not monotonic, which is the counter-intuitive half:** `last_refill_ms` is
  written to Redis and compared against timestamps written by *other processes*. Monotonic clocks
  are per-process with an arbitrary epoch, so a second admitter would compute a nonsense elapsed
  interval the moment it started. The price is that clocks can disagree or step backwards, which
  is why the script clamps a negative elapsed interval to zero — under-admitting for one tick is
  recoverable, a bucket driven negative is a stalled queue.
- **Revisit when:** clock skew between admitter hosts is ever measured to be large enough to
  matter. At one box (CLAUDE.md §7) it is zero by construction.

## 2026-08-02 — Phase 8: `qf:{event}:pass:{token}` — the first per-waiter key

- **Chose:** park each issued pass at a per-waiter key carrying the pass's own TTL, and have
  `position.lua` fall through to it before answering `unknown_token`.
- **Rejected:** (a) returning passes only from the admission loop and waiting for Phase 10's SSE
  to deliver them — that leaves v0 with no way for a polling client to ever learn it was
  admitted, i.e. no v0; (b) a per-event hash of all outstanding passes, which needs its own
  reaping logic where individual keys expire themselves.
- **Why it does not violate design.md §3's "five keys per event, nothing else exists":** it is
  not per-event, and it is self-limiting. At 100 admissions/min with a 60s TTL, roughly 100 exist
  at any instant regardless of queue size, and they disappear whether or not anyone collects.
- **The behaviour it buys, which is the actual point:** admission removes you from the sorted
  set, so `ZRANK` cannot tell "just reached the front" from "never queued". Without this key the
  system tells a person who has *just been admitted* that they are not in the queue.
- **Revisit when:** Phase 10. SSE pushes the pass at admission time, but this key stays — it is
  the only way a waiter who was mid-reconnect during their admission finds their pass.

## 2026-08-02 — Phase 8: known gap — the pop/sign window is not atomic, and cannot be

- **The gap:** `admit_batch.lua` commits the `ZPOPMIN` before Python signs the passes. A crash in
  between strands up to `batch_max` waiters: removed from the queue, no pass issued, and
  `/position` will call them `unknown_token`.
- **Why it is not simply fixed:** HMAC-SHA256 is not available inside Redis Lua, so signing
  cannot join the atomic step. Nothing about the script's design causes this; the trust boundary
  does.
- **Accepted for v0**, with the size bounded (≤ `batch_max`, currently 50) and the blast radius
  understood: those waiters must rejoin at the back, which is unfair to them specifically.
- **The fix when it earns its complexity:** have the script itself write the popped tokens as
  "admitted, pass pending" in the same atomic step, so a restarted process can re-sign for them.
  That turns an unrecoverable loss into a recoverable one.
- **Revisit when:** the admitter is ever observed to crash, or before the first real demo.

## 2026-08-02 — Phase 8 found a dead config knob: `batch_max` below `burst` or nothing

- **The finding:** `n = min(floor(tokens), batch_max, queue_length)`, and `tokens` is itself
  capped at `burst`. With the shipped defaults `burst=20, batch_max=50`, the middle term can
  never be the smallest — **`batch_max` had no effect whatsoever.**
- **Why it matters beyond the trivia:** a configuration knob that silently does nothing is worse
  than an absent one, because an operator turning it during an incident believes they have acted.
- **Kept both knobs rather than removing one:** they answer genuinely different questions —
  `burst` is "how much budget may accrue while nobody is being admitted", `batch_max` is "how
  many may arrive at the booking service in a single instant". They only *look* redundant at the
  default values. `test_batch_max_binds_only_when_it_is_below_burst` pins the relationship so the
  next person does not have to rediscover it.
- **Revisit when:** Phase 17's dynamic backpressure tunes `rate_per_min` — whatever it does must
  keep `batch_max < burst` or it is tuning nothing.

## 2026-08-02 — Phase 9: the queue service serves the one static page; CORS lives on booking

- **Chose:** the queue service serves `frontend/index.html` at `GET /` (read once at import — no
  template engine, no per-request IO, no middleware), and the **booking** service gets
  `django-cors-headers` allowing the queue origin for the one cross-origin call, `POST /book`.
- **Rejected:** (a) CORS on the queue service — it needs middleware, and `MIDDLEWARE = []` is a
  pinned invariant (Phase 5); (b) serving the page from the booking service, which only flips the
  problem — then join/position become the cross-origin calls and the *queue* would need CORS it
  cannot have; (c) a reverse proxy now — Caddy is Phase 12, a third process for a demo wanted fast.
- **Why the split is forced, not chosen:** join/position are the frequent calls and live on the
  queue service, so the page must share that origin to avoid CORS there — which it cannot provide.
  That leaves exactly one cross-origin call, `book`, and it lands on the booking service, which has
  a normal middleware stack and can carry `django-cors-headers` safely.
- **The deviation, said out loud:** `queue_service/settings.py` says "every response this service
  produces is JSON or an SSE frame." It now also serves one static HTML page. The spirit holds — no
  template engine, no per-request IO, no middleware, not on the hot path — and the `index` route is
  deleted at Phase 12 when Caddy fronts both services under one origin.
- **Revisit when:** Phase 12 — Caddy serves the frontend and proxies both services under one
  origin; the `index` route and the booking CORS allow-list both go away.

## 2026-08-02 — Phase 10: SSE via one per-process subscriber fanning out to in-memory queues

- **Chose:** one `Broadcaster` per process (`adapters/broadcaster.py`) PSUBSCRIBEs `qf:*:events`
  ONCE and copies each admission announcement into every connected client's bounded
  `asyncio.Queue`. Admission PUBLISHes `{admitted_total, total_waiting, rate}` from
  `record_admissions`. The SSE view pins the waiter's sequence from the authoritative ZRANK at
  connect, then recomputes position by arithmetic from each broadcast — zero Redis per tick.
- **Rejected:** one Redis subscription per connection (CLAUDE.md §8's named #1 mistake —
  thousands of Redis connections); per-client ZRANK every tick (the per-client cost SSE exists to
  remove); publishing from the core admission controller (kept pure — the publish is an
  adapter-side effect of recording an admission).
- **The bug this phase found in itself:** the subscriber first reused the shared command client,
  which carries a 2s `socket_timeout`. A subscriber IDLES waiting for messages, so that read
  timeout fires on every quiet interval and churns the subscription. Fix: a DEDICATED pub/sub
  client with no read timeout — exactly the "separate client for pub/sub" the Phase-5
  `redis_client` entry predicted Phase 10 would need; a subscribed connection cannot serve normal
  commands anyway.
- **Bounded per-connection queue (16), drop-oldest:** every frame carries absolute state, so a slow
  client that overflows loses only intermediate positions, never the current truth.
- **Verified:** SSE live (`text/event-stream`, `X-Accel-Buffering: no`, `retry: 3000`, a `position`
  frame); `tests/test_broadcaster.py` — one publish reaches five inboxes through one subscriber, and
  `record_admissions` publishes end to end.
- **Revisit when:** Phase 11 adds ZRANK reconciliation for the arithmetic's abandonment drift;
  Phase 13 exposes `qf_sse_connections` from `Broadcaster.connection_count()`.

## 2026-08-02 — Finding: localhost→Redis connect takes ~2s here, at the connect-timeout edge

- **Observed:** the first PING costs ~2045 ms; every subsequent op is 0 ms. It is connection
  ESTABLISHMENT that is slow (a known Windows/WSL2 localhost cost), not Redis.
- **Symptom:** `REDIS_CONNECT_TIMEOUT_SECONDS = 2.0` sits right on that ~2s, so the concurrent join
  stress tests intermittently fail to open a fresh pool connection (3 of 16 error with
  `TimeoutError`). Proven environmental: with `REDIS_CONNECT_TIMEOUT_SECONDS=10` the same tests pass
  unchanged, and Phase 10's code never touches the join path.
- **Not fixed in code:** the honest fix is a faster connect (native Redis / Docker networking, or
  the WSL2 IP instead of `127.0.0.1`), not a bigger timeout that only masks it.
- **Revisit when:** Phase 12 (Docker) moves Redis into a container — re-measure the connect cost then.

## 2026-08-02 — Phase 11: reconciliation re-pins the sequence; the displayed position is clamped

- **Chose:** the SSE loop reconciles against the authoritative ZRANK every `SSE_RECONCILE_SECONDS`
  (30s), re-pinning `sequence = zrank_position + admitted_total`, and clamps the displayed position
  with `min(computed, last_shown)` so a correction is never SHOWN moving up (FR-7).
- **Rejected:** (a) per-tick ZRANK (the per-client Redis cost the whole design removes); (b) no
  reconciliation (abandonment drift accumulates forever — `test_position`'s drift test measures the
  one-per-abandonment cost); (c) letting a correction raise the number (breaks fairness promise F4).
- **Why the arithmetic was right but incomplete:** Phase 10's `seq - admitted` is monotonic and
  cheap but drifts up by one per abandonment ahead of you. Reconciliation is the bounded, periodic
  re-truth that stops that drift being permanent; the clamp makes the correction invisible when it
  would otherwise show a position going up.
- **On the event-loop clock, not the injected Clock:** the reconcile interval is a per-connection
  local timer, not the cross-process admission rate, so `loop.time()` is correct and needs no
  injection. The drift-correction *logic* is what's tested (`test_reconcile.py`), not the 30s cadence.
- **Revisit when:** Phase 14 measures whether reconciliation corrects anything at load — a high
  `qf_position_drift_total` means the abandonment rate is higher than design.md §6 assumes.

## 2026-08-02 — Phase 12: Docker Compose, one origin via Caddy (the CORS hacks fall away)

- **Chose:** `docker compose` runs Redis 7, Postgres 16, both services, and Caddy on one origin
  (`http://localhost:8080`). Caddy serves the static frontend and reverse-proxies `/api/queue/*` →
  queue and `/events|/admin|/static/*` → booking, with `flush_interval -1` on the queue routes so
  SSE is not buffered (Caddy's form of `proxy_buffering off`, §8). Gunicorn arrives via a
  `sys_platform != "win32"` marker, so the Windows dev box stays Uvicorn-only and the Linux image
  gets the process manager.
- **Rejected:** publishing each service's port to the host (the CORS problem returns); baking
  secrets into the image (compose env with dev defaults + a root `.env` override); a build context
  that copies the frontend into the queue image (Caddy serves it static instead).
- **What one origin buys:** the Phase 9 hacks — the queue serving the page, `django-cors-headers`
  on booking — are unnecessary here, because browser, queue API and booking API share the Caddy
  origin. They stay for the non-Docker local flow (page on :8001, booking on :8000); the frontend
  now picks its booking base URL from `location.port`.
- **Verified end to end:** `make up` boots all five healthy (queue on Gunicorn + Uvicorn workers,
  the first time it runs on Linux; booking migrates then serves), and the full join → admit → book
  loop runs through :8080 and persists `booking_id 1` in the containerised Postgres.
- **Two changes it forced:** booking's `DEBUG`/`ALLOWED_HOSTS` became env-driven (the container
  must accept the host Caddy proxies under); the Makefile's `create-event` args were wrong until
  the real command signature (`--rate-per-min/--burst/--batch-max`) was checked against `--help`.
- **Parity note:** Postgres pinned to 16 here vs the dev box's native 18 — the gap logged
  2026-07-18 is now closed for anything running via Docker.
- **Revisit when:** deploying to AWS (§7) — same compose, a real domain, Caddy automatic TLS, and
  `QUEUE_COOKIE_SECURE=1`.

## 2026-08-02 — Phase 13: metrics in multiprocess mode, Prometheus + Grafana

- **Chose:** prometheus-client in **multiprocess mode** — every Gunicorn worker and the admitter
  write to a shared `PROMETHEUS_MULTIPROC_DIR`; `/metrics` aggregates with a `MultiProcessCollector`.
  A `gunicorn.conf.py` clears stale files at startup and calls `mark_process_dead` on worker exit.
  Prometheus scrapes `queue:8001/metrics` on the internal network (not through Caddy — /metrics is
  not public); Grafana provisions the datasource + one dashboard, anonymous-admin for the demo.
- **Rejected:** the default single-process registry — under Gunicorn's N workers each request hits
  a random worker, so counts would be a fraction of the truth and gauges would flicker per scrape;
  exposing `/metrics` through Caddy (Prometheus is on the internal network and it should not be
  public).
- **The metric→process map that made it work:** counters/histograms (`qf_admissions_total`,
  `qf_admission_batch_seconds`) are written by the ADMITTER process and survive it ending — that is
  the whole point of file-backed multiprocess counters, and it is what lets a separate admitter's
  numbers appear in a web worker's `/metrics`. Gauges (`qf_sse_connections` livesum, `qf_queue_depth`
  livemax) are written by the web workers and cleaned up on worker exit.
- **Verified:** the admitter's `qf_admissions_total{event} = 1` appeared in a web worker's
  `/metrics`; Prometheus reports the `queue` target `up` and the metric is queryable; Grafana is
  healthy with the datasource + dashboard provisioned.
- **`/metrics` reads files off disk**, so the async view runs the collection in a thread
  (`sync_to_async`) — no blocking IO on the event loop (CLAUDE.md §4).
- **Revisit when:** Phase 14 drives real load and the dashboard shows a full drop end to end;
  `qf_redis_command_seconds` was deferred as the least essential of the §3.1 series.

## 2026-08-13 — Phase 14 first run: a hand-rolled asyncio load client, and a local floor

- **Chose:** measure the design.md §6 headline **now**, locally, with a ~150-line pure-asyncio
  client (`loadtest/load.py`) instead of waiting for k6 + AWS. It has two modes: `join` (throughput
  + latency percentiles) and `sse` (open M connections, hold, and diff Redis `INFO commandstats`
  around the open phase and the steady-hold phase separately). The point of the two-phase split is
  to price the two costs apart honestly: opening is O(connections), holding should be ~0/tick.
- **Rejected (for now):** k6 (not installed locally, and it would still be measuring one Windows
  box); a Linux/Gunicorn multi-worker run (that is the *next* run, not this one); trusting the
  §6 claim without a number (CLAUDE.md Rule 7 forbids it).
- **What it proved (L1 in `loadtest-report.md`):** steady-state Redis command volume is **flat (3)
  at 500, 1,500 and 3,000 held SSE connections** — position is arithmetic in memory, so holding a
  connection costs ~0 Redis calls per tick. Opening costs **~6 Redis commands per connection**
  (one `position.lua` EVALSHA + its 5 internal calls, which `commandstats` counts separately) —
  linear, as predicted. 3,000 connections held with 0 failures on **one uvicorn worker**; join
  ~526 req/s, p99 631 ms.
- **What it explicitly is NOT:** a ceiling. The load generator ran on the same box as the server
  (§2 says this caps absolute numbers), it was a single uvicorn process (not the Gunicorn 4–6), and
  none of the Linux fd/somaxconn tuning applied. So 3,000 is a **floor**; the flatness is the real,
  transferable result. This distinction is written into the report so the number cannot be quoted
  without its caveat.
- **One tooling bug it surfaced:** the client first labelled open cost "~1 each (the ZRANK)"; the
  measured 6.0/connection disproved that, and the label was corrected to name the EVALSHA + all
  five internal calls. A load client that lies about what it measured is worse than none.
- **Revisit when:** k6 is installed / the stack runs on Linux under Gunicorn / a second box drives
  load — then R8–R12 (multi-worker, multi-box, end-to-end p99, heartbeat cost, Django-vs-Starlette)
  become measurable, and the ⏳ rows in `design.md` §13 can start to fill.

## 2026-08-13 — Dynamic backpressure: an AIMD controller that tunes the admission rate to booking p99

- **Chose:** a control loop (`core/backpressure.py`, run via `run_backpressure`) that reads the
  booking service's p99 and adjusts each event's `rate_per_min` to hold it near an SLO. The control
  law is **AIMD** — additive increase when healthy, multiplicative decrease on overload — the same
  law TCP uses for congestion control. It writes nothing new on the hot path: `admit_batch.lua`
  already re-reads `rate_per_min` every tick (FR-13, built in Phase 8 for a *human* operator), so
  the controller simply becomes the automated operator. Zero change to the admission hot path.
- **Rejected — proportional control** (`rate * target/observed`): reacts symmetrically to a signal
  whose cost is asymmetric (overload is an outage, slowness is an annoyance) and oscillates on noisy
  p99. **Rejected — PID**: more capable but its gains cannot be honestly defended in a portfolio
  project ("why Kd = 0.3?"). AIMD has two intuitive knobs and converges to a stable sawtooth.
- **Where the p99 comes from:** the booking service now records request latency in a Prometheus
  `Histogram` (`booking_request_seconds`) and exposes `/metrics`; Prometheus scrapes it; the
  controller queries Prometheus's HTTP API (`histogram_quantile(0.99, …)`). **Rejected** an
  in-process rolling p99 gauge on booking — with 4 gunicorn workers each would have its own window;
  Prometheus already aggregates across workers and powers the dashboard. **Cost of the choice:** the
  controller depends on Prometheus being up — mitigated by the hold rule below.
- **Missing signal → HOLD, never probe up.** `next_rate(rate, None, cfg)` returns the current rate
  (clamped), never an increase. Ramping admissions with no view of the thing being protected is how
  you melt it. It does not *decrease* either — a monitoring blip is not a booking outage; starving
  the queue for a Prometheus hiccup would be its own failure. Fail-safe-decrease was considered and
  noted as the stricter alternative. **Verified live:** with Prometheus down, `run_backpressure
  --once` read rate 600, got None, and held at 600 with no write.
- **Single instance, unlike the leaderless admitter.** N admitters are safe because the rate check
  and the pop are one atomic Lua step (design.md §7). The backpressure controller has no such
  protection: two of them would read the same rate and issue conflicting AIMD decisions — a
  read-modify-write race on `rate_per_min`, and a control loop with two controllers oscillates. So
  it runs as one process. Running several would need a lock or leader — exactly the coordination the
  admitter was designed to avoid. A clean contrast worth being able to explain.
- **Booking metrics use prometheus multiprocess mode**, mirroring the queue service's Phase 13
  setup, because the booking image runs `gunicorn -w 4` and a per-worker registry would give the
  controller a p99 that jitters per scrape. `/metrics` branches on `PROMETHEUS_MULTIPROC_DIR`: the
  aggregating collector under gunicorn, the default registry on the single-process dev server.
- **rate_min is a trickle, not zero (default 10/min).** Even under sustained overload the queue must
  still drain; an operator who wants overload to fully pause the drop sets `rate_min = 0` on purpose.
- **No new dependency (Rule 5):** the Prometheus query uses stdlib `urllib` wrapped in
  `asyncio.to_thread` — one GET every ~10s does not justify httpx/aiohttp. The fragile parsing
  (`parse_p99_seconds`: NaN and empty-result handling) is split from the IO and unit-tested.
- **Verified:** 24 unit tests (control law + parser + orchestration, no IO); both services
  `manage.py check` clean; booking `/metrics` records real request latency; the no-signal hold path
  on the live command. **Closed loop MEASURED live (L2, loadtest-report.md):** on the full Docker
  stack the controller read a real Prometheus booking p99 (56–98 ms) and drove the real admission
  rate both ways — overload 600→10, healthy 10→150. Caveat: overload was induced by setting the
  target below the observed p99, not by real booking saturation (a mock-fast booking sits under the
  200 ms SLO on one laptop) — a valid demonstration of the control *law*, not of a real p99 spike.
- **Revisit when:** the loop runs live under load (does the sawtooth actually track the booking
  ceiling? tune `increase_step` / `interval` from what the Grafana graph shows); if multiple
  backpressure instances are ever wanted, add a leader/lock; a Grafana panel for `rate_per_min` over
  time would make the control loop visible next to the p99 it chases.

## 2026-08-13 — v2 abuse mitigation: a config-gated per-client join rate limit, and the XFF trap

- **Chose:** two guarantees under one feature. (1) The fairness property the v2 item is named for —
  reconnecting, refreshing, or manufacturing identities cannot IMPROVE your position — is already
  enforced by join.lua (`ZADD NX` + monotonic sequence + cookie-bound token), so it is now *proven*
  by tests (`ReconnectImmunityTests`) rather than asserted. (2) A new **per-(event, client) fixed-
  window join rate limit** (`lua/rate_limit.lua`) bounds a single-source flood — inflating the
  queue, exhausting Redis, or fishing for slots with many identities — returning HTTP 429 +
  `Retry-After` when exceeded.
- **Why an atomic Lua script (again):** the counter is `INCR` and, on the first hit, `EXPIRE`. Done
  as two round trips, a crash or race between them leaves a key with a count but NO TTL — it never
  resets and throttles that client forever. Redis has no "increment and set-expiry-if-new"
  primitive, so the atomicity comes from the script. Same lesson as admit_batch.lua, in miniature.
- **Rejected — sliding-window / token-bucket per client:** fixed-window is the simplest correct
  per-client limiter; its known cost is a 2× burst across a window boundary, accepted for a coarse
  anti-flood gate. A per-client token bucket (like the admission one) is heavier (a hash per client)
  for no benefit here.
- **The X-Forwarded-For trap (core/clientid.py):** behind a proxy, `REMOTE_ADDR` is the proxy — every
  client identical — so the client IP must come from XFF. But XFF is a client-settable header: trust
  the wrong end and an abuser spoofs a fresh value per request and never hits the limit. With one
  trusted proxy (Caddy) the real peer is the **rightmost** XFF hop (the one the proxy appended); the
  leftmost is the spoofable "claimed" client. `TRUST_PROXY` gates this — it MUST be off when the
  service is directly exposed, and is on only in the Caddy deployment.
- **Default OFF (JOIN_RATE_LIMIT=0), an operator kill-switch.** Rate limiting is an operational
  control whose right limit/window depend on traffic and NAT topology, so it is off until a
  deployment opts in (the compose enables 30/min). Off, the join path behaves exactly as before and
  writes no rate-limit state — which is also, honestly, why the existing integration suite (which
  creates 25 waiters from one test client) is unaffected without editing a single existing test.
- **Honest limitation:** a per-IP limit bounds a single source, NOT a botnet with many IPs, and NATs
  make many real users share one IP — so the limit is generous and this is defense in depth, not a
  silver bullet. Said out loud rather than implied.
- **A rejoin still counts** toward the request-rate cap (the limit is on requests; a flood of
  rejoins is still a flood). Position immunity is a separate guarantee (join.lua), so a legitimate
  reconnect within the limit keeps its exact place — the two properties are independent and both
  tested.
- **Verified:** 9 client-id unit tests (the XFF rule, incl. the spoof) + 7 integration tests
  (enforcement, 429+Retry-After, per-client isolation via distinct XFF, disabled-mode writes no
  state, reconnect immunity), and the 16 existing join tests still green — the default-off guard
  proven to be a true no-op.
- **Revisit when:** abuse is seen from many IPs (needs a different signal — proof-of-work, device
  fingerprint, or account binding); or if a per-IP SSE *connection* cap is wanted (one IP holding
  thousands of streams is a separate DoS this does not address).

## 2026-08-13 — v2 horizontal scaling: load-balance across replicas with Caddy dynamic upstreams

- **Chose:** demonstrate the stateless-scaling claim (design.md §9) directly — run 3 queue replicas
  (`docker compose up --scale queue=3`) and load-balance across them with Caddy's `dynamic a` upstream
  (`name queue; port 8001; refresh 5s` + `lb_policy round_robin`), which re-resolves the Docker
  service DNS and round-robins over every replica it returns. Verified live (L3): 18 requests
  distributed 6/6/6, and one admission from a single admitter delivered an `admitted` frame to SSE
  streams on all three replicas (2/2/2) — cross-replica pub/sub fan-out, the thing that makes it scale.
- **The finding that motivated it:** the original `reverse_proxy queue:8001` resolves the name ONCE
  and pins every request to a single replica — 18/18 landed on `queue-1`. That is effectively sticky
  routing, and it is exactly the failure the "sticky vs stateless" question in design.md §9 is about.
  The design is stateless; the *default proxy config* was quietly not using it.
- **Rejected — adding an Nginx service** (what CLAUDE.md §6 literally names): the stack already fronts
  everything with Caddy, and a second proxy layer purely to load-balance one upstream is redundant.
  Caddy's dynamic upstreams give the same round-robin-across-replicas behaviour in the proxy we
  already run. The design point CLAUDE.md cares about — *stateless, no sticky sessions, write about
  it* — is fully served; the specific choice of Nginx is not load-bearing. Noted as a conscious
  deviation from the §6 wording.
- **Rejected — sticky/consistent-hashing at the LB:** unnecessary by construction (a waiter's place
  is in Redis, keyed by token; every replica subscribes to the same channels), and it would cost
  affinity that survives a restart plus a rebalancing story. The whole point of the O(1)-position +
  one-subscriber-per-process design is that it buys statelessness, so the LB can be dumb.
- **SSE connections are not migrated between replicas** and do not need to be: they are long-lived
  and land on whichever replica accepted them, but nothing about a connection is unique to a replica
  (position is Redis + arithmetic; the fan-out subscriber runs in every replica). Round-robin at
  *connect* is the only balancing needed.
- **What is NOT proven:** a throughput number — 3 replicas share one laptop's CPU and one Redis, so
  this shows the stateless *property*, not scale. And Prometheus still scrapes a single replica
  (static `queue:8001`), so per-replica aggregate metrics would need DNS service discovery.
- **Revisit when:** running on real separate boxes (then a throughput number is meaningful, and
  Prometheus needs service discovery); at much larger N, shard pub/sub channels by event so each
  replica does not fan out every event's traffic (design.md §9).
