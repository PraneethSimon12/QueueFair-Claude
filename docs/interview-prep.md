# Interview prep — questions & model answers

Auto-maintained per CLAUDE.md Rule 9. Every understanding-check question and every substantial
concept explanation lands here as we build, so this file doubles as a pre-interview study guide.

Format per entry: the **question**, a **one-liner** for fast recall, the **full explanation**,
and — where useful — the **interviewer framing** (how a senior reviewer would phrase it).

Decisions (what we chose vs rejected, and why) live in [`decisions.md`](decisions.md). This file
is about *concepts and questions*.

---

## Environment config & secrets

### Q: Why keep both `.env` and `.env.example`? Why not one file?

**One-liner:** `.env` holds the secret *values* (gitignored); `.env.example` documents the
required *keys* (committed). A single file can't do both without either leaking secrets or
leaving anyone who clones the repo with no idea what config the app needs.

**Full:**
- `.env` = real secret values (DB password, secret key). Gitignored → never leaves the machine.
- `.env.example` = same keys, placeholder values. Committed → documentation of what's required.
- One file, **committed** → secrets leaked (the exact thing gitignore prevents; a pushed key is
  scraped from GitHub within minutes).
- One file, **gitignored** → secrets safe, but anyone cloning the repo has zero idea what env
  vars the app needs and must reverse-engineer them from the source.
- The split separates concerns: the example shares the *shape* of the config; `.env` holds the
  *values*. Onboarding becomes `cp .env.example .env`, then fill in real values.

**Interviewer framing:** *"How does a new engineer know what environment variables to set to run
this service?"* → *"There's a committed `.env.example` listing every key."* A gitignored `.env`
with no example is a red flag.

### Q: Django doesn't read `.env` files on its own. How does a `.env` value reach `os.environ`, and if the same variable is also set in the shell, which one wins?

**One-liner:** Our hand-rolled `_load_dotenv()` copies each `KEY=VALUE` into `os.environ` using
`setdefault`, so a value already present (e.g. exported in the shell) is **not** overwritten —
**the shell/real environment wins; the file is only a fallback.**

**Full — the chain:**
1. Django imports `settings.py`; at the top, *before any setting reads config*, it calls
   `_load_dotenv(BASE_DIR / ".env")`.
2. `os.environ` is Python's live dictionary of the process's environment variables.
3. `_load_dotenv` reads the `.env` text file and, for each `KEY=VALUE` line, runs
   `os.environ.setdefault(key, value)`.
4. `settings.py` then reads values with `os.environ.get(...)`.

So `.env` is **not magic** — it's a plain text file, and our ~10-line loader is the thing that
puts its contents into `os.environ`. Django only ever looks at `os.environ`; it never knows the
file exists.

**Precedence — the operative word is `setdefault`:** it sets a key *only if it is absent*. A var
already exported in the shell is already in `os.environ`, so `setdefault` leaves it untouched.
- `.env` has `DB_PASSWORD=queuefair`. Shell empty → app uses `queuefair`. Shell has
  `DB_PASSWORD=override` → app uses `override`.

**Why this precedence is correct:** in production/CI there is often no `.env` file at all — the
platform injects real environment variables (from a secrets manager), and those must win. If the
loader used plain assignment (`os.environ[key] = value`) instead of `setdefault`, the file would
clobber the platform's values — backwards, and a nasty production surprise.

---

## Database / PostgreSQL

### Q: Why does the app connect as the `queuefair` role instead of the `postgres` superuser?

**One-liner:** **Principle of Least Privilege** — a limited role contains the blast radius. If
the app is compromised (e.g. SQL injection) or buggy, damage is confined to the `queuefair`
database instead of the entire server.

**Full:**
- `postgres` is a **superuser** — the root account of the DB server: it can `DROP` any database,
  read/modify any table in any database, create/drop roles, and change server config.
- `queuefair` is a **plain login role** that owns exactly one database (`queuefair`) and can only
  touch that one.
- **Blast radius on compromise:** an injection running as `postgres` could drop any database, dump
  every other app's data on the server, or create a hidden superuser → whole-instance compromise.
  The same injection running as `queuefair` is confined to booking data — it cannot reach other
  databases or escalate.
- **Bugs too, not just attacks:** a stray `DROP` in code running as superuser could wipe unrelated
  databases; as `queuefair` it physically cannot.
- Same instinct as never running a web server process as `root`.

**Honest caveat:** locally there is only one database on the box, so the *practical* difference
today is small — but the reasoning and the habit are exactly what a senior reviewer checks.

**Interviewer framing:** *"What's the blast radius if this service's DB credentials leak?"* →
dedicated role: *"one database."* Superuser: *"the entire Postgres instance and everything else
on it."*

---

## Data modeling (Django)

### Q: What is a `SlugField`?

**One-liner:** A `SlugField` is just a text column (`VARCHAR` under the hood) that Django
validates to contain only URL-safe characters — letters, numbers, hyphens, underscores. A "slug"
is a short, human-readable, URL-safe identifier like `coldplay-mumbai-2026`.

**Full:**
- "Slug" comes from publishing — a short label for a story. On the web it means a lowercase,
  hyphenated, URL-safe id: `my-first-post`, `coldplay-mumbai-2026`.
- `SlugField` = `CharField` + a validator that rejects spaces and special characters. In the
  database it is a plain `VARCHAR(max_length)`; the only difference from `CharField` is the
  validation.
- We use it for `Event.event_id` because an event id needs to be (a) shared by both services as
  a string, (b) safe and readable in URLs (`/events/coldplay-mumbai-2026/book`), and (c)
  eyeball-able in logs. A slug gives all three.
- Contrast: an auto-increment int (`42`) is meaningless in a URL/log; a UUID is unique but
  unreadable; a slug is unique *and* readable.
- Making it the **primary key** means the human-readable id *is* the key — no separate integer
  id to translate between the queue service and the booking service.

**Interviewer framing:** *"Why a slug instead of an auto-increment id or a UUID for the event
key?"* → "The id is one we mint and never change; it's shared across services as a string and is
readable in URLs and logs. A surrogate int would force a translation step and add nothing."

### Q: Why `on_delete=models.PROTECT` on the Booking→Event FK instead of the default `CASCADE`?

**One-liner:** With `CASCADE`, deleting an `Event` silently deletes every `Booking` row for it —
wiping the transaction records. `PROTECT` instead makes the event-deletion *fail* while bookings
exist, forcing a deliberate decision.

**Full:** Bookings are financial-ish records; they must never vanish as a side effect of an
unrelated admin action. Our model has no separate users table — `user_id` is a field *on*
`Booking` — so it is the booking rows themselves (user_id, token, timestamp) that `CASCADE` would
destroy. `PROTECT` raises `ProtectedError`; to delete an event you must first deal with its
bookings on purpose.

---

## Concurrency & inventory (the oversell race)

### Q: How do you stop a ticket drop from overselling — recording 101 bookings for 100 seats — when thousands book at the same instant?

**One-liner:** Make the capacity check and the increment a **single atomic SQL statement**:
`UPDATE event SET tickets_booked = tickets_booked + 1 WHERE event_id = %s AND tickets_booked <
capacity`. If it changes 0 rows, you're sold out. One statement means there is no gap for a race,
and Postgres serialises concurrent updates to the same row automatically.

**The race it prevents (read-modify-write):** capacity 100, 99 booked. Two requests both `SELECT`
the count (both see 99), both decide "room for one", both `INSERT` → 101 booked. The *check* and
the *write* were two separate steps and a second request slipped into the gap.

**Why the single UPDATE is safe:** the `WHERE tickets_booked < capacity` (check) and the
`SET ... + 1` (write) run as one indivisible operation, and Postgres row-level locking makes two
such updates to the same row take turns. The second request sees `100 < 100` = false → 0 rows
changed → rejected. No explicit locking code needed.

**The alternative (`COUNT(*)`):** don't store a counter; count booking rows instead. Always
consistent (the rows *are* the count) but the `COUNT` and the `INSERT` are two statements, so you
must `SELECT ... FOR UPDATE` the event row to serialise them — ~4 statements per booking and
every booking queues on one lock. Slower under a stampede.

**Tradeoff:** the counter is fast and race-proof by construction but *denormalized* (could drift
from reality if buggy — guarded by a `CheckConstraint tickets_booked <= capacity`). `COUNT(*)`
never drifts but is lock-heavy and slow at scale. **We chose the counter** (see `decisions.md`).

**Interviewer framing:** *"How do you prevent overselling under high concurrency?"* → a single
atomic conditional UPDATE (check-and-decrement in one statement); contrast with `SELECT ... FOR
UPDATE` pessimistic locking; mention the `CheckConstraint` as a database-level backstop.

### Q: Optimistic (atomic conditional UPDATE) vs pessimistic (`select_for_update`) locking?

**One-liner:** Both prevent the oversell race; they differ in *how*. Pessimistic
(`select_for_update`) locks the row, then you read-decide-write while holding the lock — others
wait. Optimistic (the `F()` conditional UPDATE) holds no lock across the logic; it does
check-and-increment in one atomic statement and you inspect the **rows-affected** (1 = got it,
0 = sold out).

- **In Django, the race trap:** `event.tickets_booked += 1; event.save()` increments in *Python*
  on a stale read → oversell. `F("tickets_booked") + 1` moves the arithmetic into the DB statement.
- **We use the atomic UPDATE** because our op is a simple bounded increment — one statement, no
  held lock, fastest under a stampede. `select_for_update` is for when you must read a row, run
  non-trivial multi-step logic, and write back while guaranteeing nobody changes it in between.

**Analogy:** `select_for_update` = hold the item off the shelf while you decide (blocks others);
atomic UPDATE = "cashier, ring up one if any are left" — check-and-decrement in one motion.

### Q: How do you make `POST /book` idempotent, and what status codes?

**One-liner:** A retried/replayed token must return the *same* booking, not a second one or an
error. The `token_jti` UNIQUE constraint is the enforcer: insert → success = `201 Created`; insert
hits the duplicate-`jti` error = replay → roll back the transaction (so the counter isn't
double-incremented), fetch the existing booking, return it as `200 OK`.

**Status codes:** `201` new booking · `200` idempotent replay · `401` invalid/expired token ·
`403` valid token but wrong event · `409` sold out (`rows == 0`) · `404` no such event.

### Q: What *property* of the single `UPDATE` prevents the oversell race — is it "idempotency"?

**One-liner:** It's **atomicity**, not idempotency. The check (`WHERE tickets_booked < capacity`)
and the write (`SET +1`) run as one indivisible operation, and Postgres serialises concurrent
updates to the same row — so no second request can act on the in-between state.

**Don't confuse the two (a common interview trap):**
- **Atomicity** solves *concurrency* — different requests running at the same time cannot
  interleave between the check and the write. This is what stops overselling (two *different*
  users racing).
- **Idempotency** solves *repetition* — the same request run twice has the same effect as once.
  That is what the UNIQUE constraints (`token_jti`, `(event, user_id)`) give us, so a replayed
  booking does not create a second row (the *same* user/token twice).

Overselling = concurrency → atomicity. Double-booking = repetition → idempotency. Different
problems, different tools.

---

## Admission tokens (JWT / HS256)

Analogy used throughout: a nightclub wristband. The bouncer (queue service) checks ID once and
snaps on a hard-to-fake wristband; the bartender (booking service) trusts the wristband without
re-checking ID. The JWT is the wristband; the signature makes it hard to fake.

### Q: What is a JWT, and is the payload secret?

**One-liner:** A JWT is a string of three base64url parts — `header.payload.signature`. The
payload is **encoded, not encrypted** — anyone can read it. A JWT guarantees
**authenticity + integrity** (it wasn't forged or tampered with), *not* secrecy. Never put
secrets in it.

- **header**: `{"alg":"HS256","typ":"JWT"}`
- **payload**: the claims (data)
- **signature**: a cryptographic stamp over header+payload

### Q: What is HS256, and why it over RS256 here?

**One-liner:** HS256 = HMAC-SHA256: `signature = HMAC_SHA256(shared_secret, header + "." +
payload)`. It's **symmetric** — the *same* secret both signs (queue service) and verifies
(booking service). We chose it over RS256 (asymmetric private/public keypair) because both
services are ours so a shared secret is fine, and RS256's signing cost shows up in p99
(CLAUDE.md §8). Change any byte of the payload, or not know the secret → the signature no longer
matches.

### Q: Why can the booking service verify a token WITHOUT calling the queue service? (the key idea)

**One-liner:** **Stateless verification.** If the signature verifies with the shared secret, only
a holder of that secret could have minted it — and only the queue service holds it — so the token
itself proves admission. No DB lookup, no cross-service call on the hot path, just a fast local
HMAC. That is what lets the system verify thousands of tokens/sec without a shared session store
becoming the bottleneck.

### Q: What does "validate a token" mean, step by step?

1. **Signature** — recompute the HMAC with the secret; mismatch → forged/tampered → reject.
2. **Expiry** — `exp` in the past → expired (past the 60s TTL) → reject.
3. **Required claims** — `sub`, `event_id`, `jti` present and sane; `event_id` matches the event
   being booked → else reject.

Claims map straight onto Step B's constraints: `jti` → `Booking.token_jti` (stops replay),
`sub` → `Booking.user_id`, `event_id` → `Booking.event`.

### Standard claims cheat-sheet

`sub` = subject (the user), `exp` = expiry (Unix seconds), `iat` = issued-at, `jti` = unique
token id. Custom claim we add: `event_id`.

### Q: Encoded vs encrypted — what's the difference?

**One-liner:** Encoding (e.g. base64) reformats data and is reversible **by anyone, no key** —
its purpose is transport, not secrecy. Encryption scrambles data so **only a key-holder** can read
it — its purpose is secrecy. A JWT payload is base64-**encoded, not encrypted**, so anyone holding
the token can read the claims.

| | Encoding | Encryption |
|---|---|---|
| key needed? | no | yes |
| reversible by | anyone | key-holder only |
| purpose | format / transport | secrecy |

Three tools not to confuse: **base64 = encoding**, **AES/RSA = encryption**, **HMAC/HS256 =
signing**.

### Q: What are SHA-256, HMAC, and therefore HS256 — in plain terms?

- **SHA-256** — a *hash function*: any input → a fixed 64-hex-char "fingerprint". Deterministic,
  one-way (can't reverse), avalanche (one byte change → totally different output). Proves
  integrity, but **anyone** can compute it.
- **HMAC** — a hash with a *secret folded in*: `HMAC(secret, message)`. The fingerprint now depends
  on the message **and** the secret, so **only a secret-holder can produce it** → this adds
  *authenticity* (proves *who* made it), which a bare hash cannot.
- **HS256 = HMAC + SHA-256.** That is all the acronym means.

### Q: Why is one shared secret enough for HS256?

**One-liner:** HS256 is *symmetric* — the same operation with the same secret both makes and checks
the signature. The verifier does not decrypt; it re-runs `HMAC(secret, data)` and compares to the
signature on the token. So one secret shared by both services suffices. (RS256 instead splits into
a sign-only private key + a verify-only public key — for when verifiers must not be able to mint
tokens.) Note: **HS256 = one secret; RS256 = two keys.** Don't say "two secrets" for HS256.

### Q: What does pinning `algorithms=["HS256"]` prevent? (a top security question)

**One-liner:** It stops the verifier from trusting the token's *own* `alg` header. Without it, an
attacker sends `{"alg":"none"}` with no signature; a naive verifier accepts it and forges any
claims → **total authentication bypass**. Pinning says "only accept HS256-signed tokens."

**Two attacks it blocks:**
1. **`alg: none`** — an unsigned token whose header claims no algorithm; a header-trusting verifier
   treats it as needing no signature check and accepts arbitrary claims.
2. **Algorithm confusion** — an RS256 system tricked into verifying an attacker's HS256 token using
   the *public* key (which is public) as the HMAC secret.

Modern PyJWT *requires* the `algorithms` argument for exactly this reason. This is why
`test_alg_none_is_rejected` exists, and a large part of why we took the PyJWT dependency instead
of hand-rolling verification.

### Q: Why does `verify_admission_token` take the secret as an argument instead of reading `settings`?

**One-liner:** It keeps the function **pure** (output depends only on its arguments), so it's
testable with zero infrastructure — no Django settings, no DB. The test run proved it by printing
*"Skipping setup of unused database(s)"*. It's also Dependency Inversion: the logic doesn't depend
on *where* the secret comes from; the caller owns that.

**Payoffs:** unit-testable in isolation; key rotation / a second issuer / test secrets = just pass
a different value; the logic module never imports Django settings.

### Q: Why not just add an API endpoint that hands out admission tokens?

**One-liner:** Because the token is *proof you waited in the queue and were admitted*. If any client
can fetch a token on demand, the queue is pointless and admission control is bypassed entirely —
the stampede hits the protected service directly. Token issuance belongs to the **queue service**,
gated behind the FIFO queue + rate-controlled release; it must never be a free endpoint, and never
on the booking service (which only *verifies*).

**Why a CLI `mint_token` for testing, not an endpoint?** A CLI command needs shell access (so only
the operator can run it), has no network attack surface, and can't be accidentally shipped/exposed
like a `/dev/mint-token` endpoint could. It fakes the issuer for tests without opening a bypass.

**The boundary:** booking service = VERIFY only; queue service = ISSUE (gated). Keeping issuer and
verifier separate is what makes the admission token trustworthy.

---

## Tooling & environment

### Q: A Django command throws a weird TypeError (e.g. `CheckConstraint got an unexpected keyword 'condition'`). First thing to check?

**One-liner:** Read the traceback's **import paths** — they reveal *which environment* the code
loaded from. Here Django loaded from a *different project's* venv (`...\Riggle X\r-one\env\...`)
that had an older Django, so a Django-5.1-only argument (`condition=`, which replaced `check=`)
raised TypeError. The code was correct; the wrong venv was active.

**Lesson:** the fix is the *environment*, not the code — use the project's own venv (activate it,
or call `.venv\Scripts\python.exe` by full path). `python` runs whatever env is active, and layered
or wrong activations (a giveaway: `(env) (base)` in the prompt) silently win. This is the concrete
payoff of the dedicated-venv decision (see `decisions.md`, Step A).

---

## Framework & architecture

### Q: Why async Django (ASGI) for the queue service instead of FastAPI? (the one they'll push on)

**The move:** reasoning → *own the tradeoff* → measurement → revisit condition. Never "Django is
faster" — it isn't, for this workload, and they'll know.

**30-second answer:**
- Both services are Python; the booking service is already Django + DRF. Using async Django for the
  queue too = **one framework** (one mental model, settings idiom, test runner, deploy).
- **SSE is just HTTP streaming** — Django's `async` view + `StreamingHttpResponse` covers it; no
  need for FastAPI's Pydantic/OpenAPI on a service whose job is to hold connections open and push
  bytes.
- **The framework is not the bottleneck** — Redis round-trips + per-connection memory dominate, not
  request parsing; Starlette's lower overhead wouldn't move the number that matters.
- It's a **measured bet** — load-testing to 10K connections makes it falsifiable.

**Volunteer the tradeoff (this is what scores):** Django has more per-request overhead than
Starlette, and its **sync/async middleware adaptation is a footgun** — one sync middleware makes
Django route the async view through `sync_to_async` + a thread pool, which can serialize at
thousands of connections. Mitigation: keep the whole middleware chain async.

**Pushback handling:**
- *"Isn't FastAPI/Starlette the obvious pick for high-concurrency SSE?"* → "Defensible — if the
  queue stood alone I might pick it. Deciding factors: one framework across two services, and the
  framework isn't the bottleneck. And I measure it rather than assert it."
- *"Why not Go?"* → bottleneck is Redis round-trips + the booking service, not the queue process;
  asyncio handles the IO at our scale; multi-process workers cover the GIL; measurements will show
  where it falls over.

Full decision (chose/rejected/why/tradeoff): `decisions.md`, 2026-08-02 "Queue service on async
Django (ASGI), not FastAPI."

---

## The queue: FIFO ordering, fairness, and Redis

> Added 2026-08-02 alongside `design.md`. **None of this is built yet** — these are the concepts
> the design rests on, so they are answerable in an interview as *design*, never as *shipped*.
> `resume-claims.md` tracks which is which.

### Q: Why is arrival order a counter (`INCR`) and not a timestamp?

**One-liner:** Timestamps collide, skew, and leave gaps. A counter does none of the three, and
all three would break something different.

**Full — three separate failures:**

1. **Collisions.** At 60,000 arrivals in 60 seconds, millisecond timestamps repeat constantly.
   Two members of a Redis sorted set with the *same score* have no defined order beyond
   lexicographic comparison of the member strings — so ordering silently degrades into comparing
   random hex tokens. That is not FIFO; it looks like FIFO and behaves like a coin flip.
2. **Clock skew.** With several queue-service processes, their clocks differ by milliseconds at
   best. Someone who genuinely arrived later, on a fast-clocked box, sorts ahead of someone who
   arrived earlier elsewhere. Fairness breaks with no error and no log line anywhere.
3. **Density.** Position arithmetic (next question) needs sequences with no gaps. A counter is
   dense by construction; a clock cannot be.

`INCR` is atomic on a single Redis instance, so one counter per event is a **total order** over
arrivals — which is precisely what "fair" means here.

**Interviewer framing:** *"How do you establish ordering across multiple app servers?"* → "A
single atomic counter in Redis, not wall-clock time — because clocks disagree across processes
and timestamps collide under burst load."

### Q: Walk me through the race that breaks queue fairness. (**the project's central lesson**)

**One-liner:** `ZSCORE` to check, then `INCR` + `ZADD` to join, is a `GET → modify → SET` across a
network. Two concurrent joins with the *same* token both see "not queued", both take a number,
and the second `ZADD` overwrites the first — so **double-tapping makes your position worse.**

**The interleaving:**

| | Request A | Request B | State |
|---|---|---|---|
| t₁ | `ZSCORE` → nil | | not queued |
| t₂ | | `ZSCORE` → nil | not queued |
| t₃ | `INCR` → 100 | | |
| t₄ | `ZADD {tok: 100}` | | tok at 100 |
| t₅ | | `INCR` → 101 | |
| t₆ | | `ZADD {tok: 101}` | tok at **101** ← lost update |

Both read before either wrote. Plain `ZADD` overwrites an existing score, so the later, worse
number wins.

**Why it is nasty:** no exception, no log, no alert. The only symptom is one user's position
increasing — which they will complain about and nobody will be able to reproduce.

**Why `ZADD NX` is not the whole answer (be honest about this):** `NX` only adds if absent, so
t₆ becomes a no-op and the user keeps 100. **That is genuinely correct for fairness** — do not
invent a race that is not there. Two real costs remain: three round trips per join instead of
one (~180,000 instead of 60,000 on the hottest path), and request B's `INCR` to 101 is *burned*,
leaving a gap in the sequence. The round trips are a performance argument. **The gaps are a
correctness argument**, because position arithmetic requires density — and that is what actually
decides it.

**The fix:** one Lua script doing `ZSCORE` → (if absent) `INCR` + `ZADD`. Redis is single-threaded
and runs a script to completion, so no other command interleaves. One round trip, no gaps.

**Interviewer framing:** *"Where's the race condition in your queue?"* → describe the
interleaving, then say why `ZADD NX` fixes correctness but not density, then the script. Claiming
"Lua because Redis isn't atomic" is wrong and gets caught.

### Q: How do you show 20,000 people their position without 20,000 Redis calls?

**One-liner:** `position = my_sequence − total_admitted`. Because the queue is strict FIFO and
only pops from the front, rank is derivable arithmetic — one subtraction in memory, zero Redis
round trips per waiter.

**The derivation:** sequences are dense from 1..N, and everyone admitted has a lower sequence
than anyone still waiting. So people ahead of me = `(my_seq − 1) − admitted`, and
`position = ahead + 1 = my_seq − admitted`. Check: first arrival, `seq=1`, `admitted=0` →
position 1 ✓.

`my_seq` is fixed for the life of the connection. `admitted` arrives in one broadcast pub/sub
message per admission batch. So:

| | Redis ops/sec at 20K waiters |
|---|---|
| `ZRANK` polled every 5s | ~4,000 |
| Arithmetic + 1 broadcast/sec | **~1** |

**The three preconditions, and what each breaks:**
1. *Sequences dense* → violated, positions inflate by the number of gaps ahead
2. *Removal only from the front* → violated, every position behind the removal is permanently wrong
3. *Counters never reset* → violated, positions go negative or leap

**The cost of #2:** people who abandon the queue cannot be removed from the middle, so they are
"admitted" into the void and the effective rate of real people is below the configured rate. A
known, accepted inefficiency — not an oversight.

**Honesty:** `ZRANK` remains authoritative and reconciles periodically (and on every reconnect).
The arithmetic is the fast path, not the source of truth.

**Interviewer framing:** *"What's your per-connection cost?"* → "Constant. Position is a
subtraction against a broadcast counter, not a query — here's the derivation and here are the
three invariants that make it valid."

### Q: Why does the admission script need to be atomic? What breaks if it isn't?

**One-liner:** Three different things break, in three different components, from one race.

Two processes each run an admission loop. Both read the token bucket, both see a full quota, both
`ZRANGE` the same front-of-queue members, both `ZREM`, both issue passes:

1. **Over-admission** — 200 admitted in a window sized for 100. The backpressure that is the
   system's entire reason to exist has been bypassed and the booking service takes 2× its
   designed load.
2. **Double admission** — the same person gets two passes with two different `jti`s. The booking
   service's `jti` uniqueness will *not* catch it (two `jti`s are two requests), so it falls
   through to the `(event, user_id)` constraint — a queue-service bug surfacing two services
   away from its cause.
3. **A corrupted counter** — `admitted` is incremented twice for one pop, so **every** waiter's
   computed position is now wrong.

**The fix:** refill, consume and `ZPOPMIN` in one Lua script.

**The honest nuance:** `ZPOPMIN key count` is *itself* atomic, so the pop alone would not need
Lua. The script exists because **the rate check and the pop must be atomic together.** Get this
distinction right — it is the difference between understanding atomicity and reciting it.

**Design detail:** `now_ms` is passed in as an argument rather than read with
`redis.call('TIME')`, so the script is a pure function of its inputs — unit-testable with a fake
clock, and trivially deterministic.

### Q: What does making the *operation* atomic buy you over making the *actor* exclusive?

**One-liner:** It removes leader election entirely. Because the bucket is atomic, every process
can run an admission loop safely — no elected admitter, no lock lease, no renewal, no failover gap.

The alternative is a single designated admitter holding a Redis lock. Easier to reason about,
and it costs: lease renewal, failover detection, a stall while a dead leader's lease expires, and
a genuine single point of failure during that window. Serialising the *operation* instead of the
*actor* removes all of it — the processes contend on a script Redis already runs one at a time,
and a crashed process simply admits nobody.

**Interviewer framing:** *"How do you coordinate admission across instances — leader election?"*
→ "No leader. The rate check and the pop are one atomic Redis script, so every instance can admit
concurrently and the global limit still holds."

---

## SSE at scale

### Q: What's the #1 architectural mistake in a Python SSE service?

**One-liner:** One Redis pub/sub subscription per connected client. 20,000 waiters → 20,000 Redis
connections. Redis's default `maxclients` is **10,000**, so it does not merely perform badly — it
fails outright.

**The fix:** one subscriber task **per worker process**, fanning out to in-memory
`asyncio.Queue`s. Each SSE connection registers a bounded queue and iterates it. Redis
connections per process: one, regardless of connection count.

### Q: Why do the pushed messages carry absolute state instead of deltas?

**One-liner:** Because it makes two otherwise-hard problems disappear.

- **A dropped message becomes harmless.** The next message is complete on its own — no resync
  protocol, no gap detection, no replay buffer.
- **A slow client can be dropped rather than waited for.** Its bounded queue discards the oldest
  message; nobody else is blocked. Safe *only* because the newest message is self-sufficient.

Deltas would be marginally smaller on the wire and would require every consumer to be perfectly
reliable forever. The point is being able to say *why* the trade goes this way, not just picking
correctly.

**Corollary:** the SSE stream sends no `id:` field and ignores `Last-Event-ID`. There is nothing
to replay — a reconnecting client's first frame is current truth, which is strictly better than
replayed history.

### Q: Why does an SSE service need heartbeats, and what do they cost?

**One-liner:** Proxies drop idle connections, so a comment frame (`: ping\n\n`) every ~15s keeps
them open. At 20,000 connections that is ~1,300 writes/sec carrying no information — cheap per
write, not obviously cheap in aggregate, so it goes on the measurement list rather than being
assumed fine.

---

## Django on ASGI

### Q: What is the single most dangerous configuration mistake in an async Django service holding thousands of connections?

**One-liner:** Adding **one** non-`async_capable` middleware. Django adapts between sync and
async at the middleware boundary, so a single sync middleware forces the entire request through
`sync_to_async` — which runs it **in a thread from the ASGI thread pool**. With thousands of open
SSE connections that is thousands of parked threads, and the pool is exhausted long before.

**Why it is so dangerous:** it is a one-line settings change, made by someone adding an unrelated
feature, with a symptom (the service dies under load) that nobody connects back to the cause.
Hence `MIDDLEWARE = []` in the queue service, as a deliberate, commented constraint.

**Related traps in the same family:**
- `GZipMiddleware` / `ConditionalGetMiddleware` must **consume** the stream to compress or hash
  it — so an SSE client receives nothing until the generator ends, which for SSE is never.
- `StreamingHttpResponse` needs an **async** iterator (Django 4.2+). A sync generator gets run in
  a thread — the same trap wearing a different hat.
- Any ORM call from an async view raises `SynchronousOnlyOperation`; "fixing" it with
  `sync_to_async` just moves the request onto a thread. The queue service therefore has **no
  `DATABASES` at all**, so the mistake fails at import rather than under load.

**Interviewer framing:** *"You chose Django for a high-concurrency SSE service — what's the
risk?"* → the sync-middleware thread-pool trap, named precisely, plus the mitigation. This is the
answer that shows you chose Django with your eyes open rather than by habit.

### Q: Do you need sticky sessions for SSE behind a load balancer?

**One-liner:** Not in this design. Per-connection state is exactly one integer (the arrival
sequence) which can be re-read from Redis by any process, and every process subscribes to the same
channels — so a reconnect can land anywhere and a process can be restarted mid-drop.

What stickiness would have cost: consistent hashing at the proxy, affinity that survives a
restart, and a rebalancing story. All avoided by keeping per-connection state to something
re-derivable.

What statelessness costs: at N processes, pub/sub delivery work is N× the single-process amount.
Fine at N=3; a reason to shard channels by event at much larger N.

---

## Working practice

### Q: Why does the project need a `resume-claims.md` at all?

**One-liner:** Because CLAUDE.md Rule 7 ("never put a number on the resume we haven't
reproduced") had no artifact enforcing it, and the CV drifted roughly a whole version ahead of the
repository — seven unsupported claims, plus one that contradicted the project's own decision log.

**The mechanism:** every public claim gets a status — SHIPPED (code + passing tests), MEASURED (a
recorded run), DESIGNED (a design doc, no code), PLANNED, UNSUPPORTED — and only the first two may
appear on a CV. DESIGNED may appear, but only with the verb *"designed"*.

**Why verbs are load-bearing:** *designed*, *built* and *measured* are not synonyms, and a GitHub
link sitting next to the bullet makes the difference checkable in ninety seconds. "Designed a
race-free FIFO queue" with a design doc behind it is a strong, defensible claim. "Built" it,
without the code, is one question away from ending an interview.

**The specific trap this caught:** the CV said admission tokens were validated "via stateless
middleware". They are validated by a **DRF authentication class** — and `decisions.md` records
middleware being considered and *rejected*. An interviewer reading the decision log would have
found the candidate claiming the thing the candidate had rejected.

---

## Load testing & measurement

### Q: L1 shows opening an SSE connection costs ~6 Redis commands but *holding* it costs ~0 per tick, flat from 500 to 3,000 connections. During a real ticket drop, admissions fire every second. Does the steady state stay flat then — and if the box falls over at some N, what actually ran out?

**Model answer — two parts, and the interviewer wants both.**

**(1) Does it stay flat when admissions are firing?** *Redis* stays flat; *CPU* does not. When an
admission batch commits, the admitter does **one** `PUBLISH` on `qf:{event}:events`. The one
per-process subscriber receives that single message and fans it out to every connected client's
in-memory `asyncio.Queue` — **zero extra Redis commands per client.** So Redis command volume is
driven by *admission rate*, not by *connection count*: 600 admissions/min is ~10 `PUBLISH`/sec
whether 500 or 50,000 people are watching. What *does* scale with connection count is the
in-process fan-out: N `asyncio.Queue.put` calls and N position recomputations per message, on the
event loop's single core. That is the honest answer to "what's the bottleneck" — it is CPU in the
Python process fanning out, not Redis.

**One caveat I must state, or the "flat" claim is a small lie:** the 12 s L1 hold is shorter than
the 30 s reconciliation interval, so it measured the *between-reconcile* floor. Every 30 s each
connection does one authoritative `ZRANK` to correct abandonment drift — so Redis steady-state is
not literally zero, it is ~`N/30` ZRANK/sec (bounded, deliberate, design.md §6). Still flat *per
tick*; not flat *forever*.

**(2) What runs out first at the ceiling?** In rough order as N climbs: (a) **event-loop CPU** —
one core doing all the fan-out and heartbeat writes; this is why the real config is Gunicorn 4–6
workers, to get 4–6 cores. (b) **File descriptors / memory per connection** — each open SSE
response is a socket plus a bounded queue plus Python object overhead; RSS ÷ N is the number L1
did *not* measure and R7 still needs. (c) **`somaxconn` / ephemeral ports** during the *ramp*, not
the hold — a harness limit, not a system one. Redis `maxclients` (default 10,000) is *not* in this
list, and that is the whole point of one subscription per process: the naive one-connection-per-
client design dies there at 10K; this design never opens more than a handful of Redis connections.

**Why this is the strong version of the answer:** it separates the two cost axes (admission rate
vs connection count), names which resource each pressures (Redis vs CPU/memory), states the
reconcile caveat instead of hiding it, and explains why the design sidesteps the one Redis limit
(`maxclients`) that kills the naive approach.

---

## Dynamic backpressure (v2)

### Q: You run as many admitters as you like with no leader and no lock, and you say that's safe. But the backpressure controller must be a *single* instance. Why is one leaderless and the other not — isn't that a contradiction?

**No — and the difference is the whole lesson of the project applied twice.**

The admitter is safe to run N-wide because its critical operation — "check the rate budget AND pop
that many waiters" — is **one atomic Lua script** (`admit_batch.lua`). Two admitters that wake at
the same instant do not both read "100 tokens" and both pop: Redis runs each EVALSHA to completion
before the next starts, so the second sees the bucket the first already drained. The atomicity is
*inside* the operation, so no coordination is needed *around* it. That is design.md §7: making the
operation atomic removes the need to make the actor exclusive.

The backpressure controller has no such atomic step. Its logic is a **read-modify-write**: read
`rate_per_min`, apply AIMD, write the new value back — three separate Redis operations with the
control decision in between, in Python, not in a script. Two controllers would interleave exactly
the way the naive queue-jumping join did: controller A reads 400, controller B reads 400, A sees
overload and writes 200, B sees health and writes 420 — A's backoff is silently erased, and now
they fight. Worse, it is a *control loop*: two controllers driving the same knob don't just race,
they oscillate, because each reacts to a state the other is also changing.

So the honest rule is: **atomicity where you can get it, exclusivity where you can't.** The admitter
gets atomicity for free from Lua, so it needs no leader. The controller can't express its
read-decide-write as one atomic step (the decision is application logic), so the cheap correct
answer is to run one of them. If I ever needed several — I don't, one control loop is plenty — I
would have to add back exactly the coordination the admitter was designed to avoid: a lock, a lease,
or a leader election. That contrast, the same system needing opposite answers for two loops, is the
point.

**Two follow-ups they'll pull, and the short answers:**
- *"Why AIMD and not a proportional controller?"* The cost of the two error directions isn't
  symmetric — over-admitting is an outage of the thing I'm protecting, under-admitting is a slightly
  longer wait — so I want asymmetric response: cut hard on overload (multiplicative), reclaim gently
  (additive). Proportional control is symmetric and oscillates on noisy p99; PID's gains can't be
  honestly tuned in a project this size.
- *"Prometheus goes down mid-spike — does the rate keep climbing?"* No. A missing p99 is treated as
  "hold", never "healthy". `next_rate(rate, None, cfg)` returns the current rate; it never increases
  without a live signal, because raising load on a service you can no longer see is how you melt it.
  It doesn't back off on a blip either — a monitoring gap isn't a booking outage. Verified live:
  with Prometheus down, one control step read 600 and held at 600 with no write.

---

## Abuse mitigation (v2)

### Q: Your join rate limit keys on the client IP, taken from X-Forwarded-For. Which end of that header do you trust, and what exactly breaks if you pick the other one?

**The rightmost hop — the one the proxy appended — and picking the leftmost hands an attacker a
free bypass.**

X-Forwarded-For is a *request header*, so the client can send whatever it likes in it. When the
request passes through our one trusted proxy (Caddy), Caddy **appends** the address it actually
received the connection from. So with a single proxy the header reads
`"<whatever the client typed>, <the client's real IP>"` and the **last** element is the only one a
trusted machine vouched for. The leftmost element is the "original client" the header *claims* to
be — and it is fully attacker-controlled.

If I keyed the limit on the leftmost value, an abuser would send a different fake
`X-Forwarded-For` on every request — `1.1.1.1`, `2.2.2.2`, … — and each would look like a brand-new
client with a fresh budget. The limit would count to one and reset forever; it would be decorative.
Keying on the rightmost hop counts every one of those requests against the abuser's real IP, which
is the whole point.

**The other half of the answer — why there's a `TRUST_PROXY` flag:** the rightmost rule is only
safe *because* a trusted proxy is in front adding that last hop. If the service were exposed
directly, there is no trusted appender — the entire header, rightmost included, is the client's to
forge — so `TRUST_PROXY=False` makes the limiter ignore XFF entirely and key on the real transport
peer (`REMOTE_ADDR`). Reading XFF on a directly-exposed service is the same spoof bug wearing a
different hat. (For N chained trusted proxies you'd take the (N+1)th from the right; we run one.)

**What it deliberately does NOT solve** (say this before they ask): a botnet with thousands of real
IPs sails through — each IP has its own budget — and a corporate NAT makes many innocent users
share one. So the limit is generous and this is defense in depth against the cheap single-source
flood, not a defense against a distributed attacker. Stopping that needs a different signal:
proof-of-work, a device fingerprint, or binding a place in line to an authenticated account.

**Bonus — why the counter is one Lua script:** the limiter is `INCR` plus, on the first hit of a
window, `EXPIRE`. Split across two round trips, a crash between them leaves a counter with no TTL —
it never resets and throttles that client forever. One script makes INCR-and-set-expiry-if-new
atomic. It's admit_batch.lua's lesson in miniature.
