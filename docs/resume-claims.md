# QueueFair — Resume Claims Ledger

> **Every claim made about this project in public, and the evidence behind it.**
>
> CLAUDE.md Rule 7 says: *"Never let me put a number on my resume that we have not reproduced on
> a real run."* That rule had nothing enforcing it, and the resume drifted about one whole
> version ahead of the repository. This file is the enforcement.
>
> - A claim moves to **SHIPPED** when code exists and tests pass.
> - A claim moves to **MEASURED** when [`loadtest-report.md`](loadtest-report.md) records the run.
> - **Nothing goes on the CV from any other row.**

**Last audited:** 2026-08-13, full re-audit after **Phases 0–13 + all three v2 picks** (dynamic
backpressure, abuse mitigation, horizontal scaling), with three measured runs recorded (L1/L2/L3 in
[`loadtest-report.md`](loadtest-report.md)). The gap between CV and repo has largely closed: most of
the CV is now SHIPPED or MEASURED. **What is still false and must not be written:** `p99 < 200 ms`
(no end-to-end position-latency run), Redis Sentinel failover, and k6 at 100K VUs — plus one wording
bug (`middleware` → DRF authentication class). Everything else below survives `git log`.

---

## Status vocabulary

| Status | Means | May appear on a CV? |
|---|---|---|
| ✅ **SHIPPED** | Code exists in this repo and its tests pass | **Yes** |
| 📏 **MEASURED** | A number reproduced on a real run, recorded in `loadtest-report.md` | **Yes, with the number** |
| 📐 **DESIGNED** | Written down in `design.md` with alternatives considered; no code | Only with the verb *"designed"*, never *"built"* |
| ⏳ **PLANNED** | On the phase list in `build-plan.md`; not designed in detail | **No** |
| ❌ **UNSUPPORTED** | Claimed, with nothing behind it | **No — remove or rewrite** |
| ⚠️ **MISSTATED** | Something real exists, but the claim describes it wrongly | **No — fix the wording** |

The distinction between DESIGNED and SHIPPED is the one that matters most. "Designed a race-free
FIFO queue" is a defensible sentence with a design doc behind it. "Built" is not, and the
difference is one word an interviewer will find in ninety seconds by opening the repo.

---

## 1. The ledger

Claims are quoted verbatim from the CV as of 2026-08-02 and split where one sentence makes
several claims.

### Bullet 1 — "Built a virtual waiting room on async Django (ASGI) holding 20K+ concurrent SSE connections across a 3-node cluster at p99 < 200 ms, shielding a Django booking backend from thundering-herd traffic"

| Sub-claim | Status | Evidence / what is missing | Unblocked by |
|---|---|---|---|
| "virtual waiting room" | ✅ SHIPPED (v1) | The whole loop works end to end **over SSE**: join → watch position fall live → admitted at a controlled rate → book, both services behind Caddy on one origin, forged pass rejected. Vanilla EventSource UI (`frontend/index.html`); 30s ZRANK reconciliation with an upward-clamp so the shown position never rises. | — |
| "on async Django (ASGI)" | ✅ SHIPPED | `queue_service/` — async Django on ASGI, `MIDDLEWARE = []`, no database, live SSE streaming under Gunicorn+UvicornWorker; executable invariant guards pin the empty-MIDDLEWARE / no-DB constraints. Whole queue suite green (join/position/admission/broadcaster/reconcile/backpressure/rate-limit). | — |
| "holding 20K+ concurrent SSE connections" | 📏 **PARTIAL — 3,000 MEASURED, 20K still ❌** | SSE endpoint shipped (Phase 10). L1 (2026-08-13) held **3,000** connections on one uvicorn worker, 0 failed — a local *floor*, not the ceiling, and not 20K. The honest number today is 3,000, single process, generator on-box. | Phase 14 multi-worker/multi-box for a higher, real N |
| "across a 3-node cluster" → **"3 stateless replicas behind a round-robin LB"** | 📏 MEASURED as *replicas*, ❌ still false as *"cluster"* | L3 (2026-08-13): ran **3 queue replicas** behind Caddy load-balancing (`dynamic a` round-robin); requests distributed 6/6/6 and one admission reached SSE streams on all three (2/2/2 — cross-replica fan-out, no sticky sessions). This supports *"3 stateless replicas / worker processes behind a round-robin load balancer, demonstrated"*. It does **not** support *"3-node cluster"* — that implies three machines, and this is 3 processes on one laptop (CLAUDE.md §7 budgets one box). Rewrite the noun: **replicas/processes**, not nodes, and say "demonstrated the property", not a throughput number. | Real separate boxes for a genuine multi-node + throughput claim |
| "at p99 < 200 ms" | ❌ UNSUPPORTED | Nothing measured — and the claim does not say p99 *of what*. Define it as position-update propagation, admission→client. | Phase 14 |
| "shielding a Django booking backend" | ✅ SHIPPED | `booking_service/` — Django 5 + DRF + PostgreSQL, `POST /events/{id}/book`, 19 tests green | — |

### Bullet 2 — "Kept per-connection cost flat by multiplexing every waiter over a shared Redis pub/sub fan-out in the event loop instead of one Redis connection per client"

| Sub-claim | Status | Evidence / what is missing | Unblocked by |
|---|---|---|---|
| The fan-out architecture | ✅ SHIPPED (Phase 10) | `adapters/broadcaster.py` — ONE Redis `PSUBSCRIBE` per process fanning out to per-connection bounded `asyncio.Queue`s; absolute-state messages so a dropped message is safe; a full queue drops the slow client, not everyone. `test_broadcaster.py`. This is CLAUDE.md §8's "#1 architectural mistake" (one subscription per connection) explicitly avoided. | — |
| "kept per-connection cost flat" | 📏 **MEASURED** | L1 (2026-08-13): Redis commands during a steady hold were **3 at 500, 3 at 1,500, and 3 at 3,000 connections** — flat, independent of connection count, via `INFO commandstats`. Holding a connection costs ~0 Redis calls/tick; opening costs ~6 (one `position.lua` EVALSHA + 5 internal calls). Measured across 500→3,000 (a 6× range), not yet the 1K→10K of the original target. | 1K→10K needs the multi-worker run |

**Note the verb.** "Kept … flat" asserts an outcome — and as of L1 (2026-08-13) the outcome is
**measured**: the steady-state Redis count held at 3 across a 6× jump in connections (500→3,000).
The verb "kept flat" is now defensible, *with the caveat* that the run was one uvicorn worker on
one box; the O(1) position arithmetic (`design.md` §6) is what makes the flatness possible, and it
is still the interesting half to talk about.

### Bullet 3 — "Designed a race-free FIFO queue using Redis sorted sets with atomic Lua scripts for admission; issued short-TTL JWTs validated at the booking layer via stateless middleware to block queue-jumping"

| Sub-claim | Status | Evidence / what is missing | Unblocked by |
|---|---|---|---|
| "**Designed** a race-free FIFO queue … sorted sets … atomic Lua" | ✅ **SHIPPED** — and the verb can now be *built* | `lua/join.lua`, `lua/position.lua`, `lua/admit_batch.lua`. Both races are **demonstrated, not asserted**: 1,000 concurrent joins of one token give the naive version 50 sequences and 49 burned gaps vs 1 and 0; three concurrent admitters give the naive version 60 passes for 20 people vs exactly 120 over 60s at 100/min. | — |
| "short-TTL JWTs … validated at the booking layer" | ✅ SHIPPED | `bookings/tokens.py`, HS256, `algorithms=["HS256"]` pinned, required claims enforced; `bookings/authentication.py`; tests incl. `test_alg_none_is_rejected` | — |
| "**issued**" | ✅ SHIPPED | `adapters/pass_issuer.py`, reachable only from the admission controller — i.e. only after the token bucket has popped you off the front. Verified end to end: an admitted waiter books (201), retries (200, same booking), and a still-queued waiter forging a pass gets 401. | — |
| "**stateless**" | ✅ SHIPPED | Accurate and it is the good part: verification is a local HMAC with no DB lookup and no call to the queue service | — |
| "via stateless **middleware**" | ⚠️ **MISSTATED** | It is a **DRF authentication class**, not middleware — and `decisions.md` (2026-07-18, D1) records middleware being *considered and rejected*, because it runs on every URL, needs path hacks, and does not integrate with DRF's request. **An interviewer who reads the decision log finds you claiming the thing you rejected.** Fix the word. | Wording fix, today |

### Bullet 4 — "Implemented dynamic backpressure (token-bucket admission auto-tuned to booking p99), validated graceful degradation under Redis Sentinel failover with k6 chaos tests (100K virtual users), and instrumented Prometheus + Grafana dashboards for queue depth, throughput, and end-to-end wait time"

| Sub-claim | Status | Evidence / what is missing | Unblocked by |
|---|---|---|---|
| "Implemented dynamic backpressure … auto-tuned to booking p99" | ✅ SHIPPED **+ 📏 MEASURED** (closed loop, L2) | Built 2026-08-13: an AIMD controller (`core/backpressure.py`) tunes `rate_per_min` to hold the booking p99, read from a booking Prometheus histogram via `histogram_quantile`. 24 unit tests. **L2 (loadtest-report.md) demonstrated the closed loop LIVE** on the full Docker stack: reading a real booking p99 (56–98 ms), the controller drove the real admission rate down on overload (600→10, target below p99) and up on health (10→150, +20/tick). Caveat that MUST travel with the claim: overload was induced by tightening the target below the observed p99, not by real booking saturation — so say "demonstrated the control loop end to end", not "handled a real p99 spike to 200 ms". | Real booking saturation (slower path / higher concurrency) to drop the caveat |
| "validated graceful degradation under Redis Sentinel failover" | ❌ UNSUPPORTED | No Sentinel, no failover test. `design.md` §11 currently names Redis as the **v1 single point of failure** — the opposite of this claim. | Phase 16 |
| "k6 chaos tests (100K virtual users)" | ❌ UNSUPPORTED | No k6 scripts. **And 100K VUs is not reachable on the planned infrastructure** — k6 manages roughly 10–30K VUs on one well-provisioned machine, and the budget is one spot instance (CLAUDE.md §7). Claiming a number the setup cannot produce is worse than claiming no number. | Phase 14, and a realistic number |
| "instrumented Prometheus + Grafana dashboards" | ✅ SHIPPED (Phase 13) | prometheus-client in **multiprocess mode** (both services — aggregated across gunicorn workers); Prometheus scrapes queue + booking; Grafana provisions a datasource + a QueueFair dashboard. Series: queue depth, SSE connections, admissions, admission-batch latency, position drift, booking request latency. | A dashboard screenshot in the README |

**Two of this bullet's four sub-claims are now real** — dynamic backpressure (📏 MEASURED, L2) and
Prometheus + Grafana (✅ SHIPPED). The other two — Sentinel failover and k6 at 100K VUs — remain
unbuilt and (for 100K) unreachable on the budgeted hardware. So the verb "Implemented" is now
half-earned: **split the bullet, keep backpressure + observability, delete the Sentinel and k6
clauses** until they exist. This was the highest-risk line on the CV; it is now half-defused.

### Technology line — "Python, Django (ASGI), Redis, PostgreSQL, Docker, Prometheus"

| Item | Status |
|---|---|
| Python | ✅ SHIPPED |
| Django (ASGI) | ✅ SHIPPED — async Django on ASGI runs the live SSE queue service (`MIDDLEWARE=[]`, no DB), Django+DRF the booking service |
| PostgreSQL | ✅ SHIPPED |
| Redis | ✅ SHIPPED — the whole queue lives here: sorted sets, Lua scripts, pub/sub, token bucket, the rate-limit counter |
| Docker | ✅ SHIPPED — Phase 12, `docker compose` stack behind Caddy on one origin; scale-ready (L3) |
| Prometheus | ✅ SHIPPED — Phase 13, multiprocess mode + Grafana dashboard |

---

## 2. Scoreboard

| Status | Sub-claims |
|---|---|
| ✅ SHIPPED | 15 |
| 📏 MEASURED | 4 (two partial — the real number is below the CV's, i.e. 3,000 not 20K, replicas not nodes) |
| 📐 DESIGNED | 0 |
| ⏳ PLANNED | 0 |
| ⚠️ MISSTATED | 1 (middleware → DRF auth class) |
| ❌ UNSUPPORTED | 3 (`p99 < 200 ms`, Sentinel failover, k6 100K VUs) |

**The picture has inverted since the 2026-08-02 audit** (which read 4 shipped / 7 unsupported). The
repository is now at **Phases 0–13 plus all three v2 picks**, with three measured runs on the board.
What remains un-writable is small and specific: three unsupported claims and one wording bug — versus
fifteen shipped and four measured. **The only items that must not appear on the CV until built:**
end-to-end `p99 < 200 ms`, Sentinel failover, and k6 at 100K; and `middleware` must be reworded to
`DRF authentication class`. Everything else is now defensible line by line against the GitHub link.

---

## 3. The honest version — usable today

Everything below is ✅ SHIPPED or 📐 DESIGNED and survives being interrogated line by line.

> **QueueFair: Distributed Virtual Waiting Room** | *Python, Django, DRF, PostgreSQL, Redis, JWT*
>
> - **Designing** a virtual waiting room that absorbs ticket-drop stampedes (BookMyShow-style)
>   and admits users to a protected booking service in fair FIFO order at a controlled rate —
>   shipped with an RFC-style design doc, a decision log recording every rejected alternative,
>   and a product spec stating the fairness guarantees the system actually promises.
> - **Built the protected booking service** on Django + DRF + PostgreSQL: stateless HS256
>   admission-token verification with the algorithm pinned, so forged `alg:none` and
>   algorithm-confusion tokens are rejected; verification is a local HMAC with no cross-service
>   call and no shared session store on the hot path.
> - **Made overselling impossible under concurrency** with a single atomic conditional `UPDATE`
>   (capacity check and increment in one statement, no held lock), backed by a database
>   `CheckConstraint` as a hard floor — and made booking **idempotent** with a unique token id, so
>   a replayed request returns the original booking instead of creating a second.
> - **Designed the queue core**: a Redis sorted-set FIFO ordered by an atomic counter rather than
>   timestamps, with Lua scripts making join idempotent and admission rate-limited atomically —
>   the latter removing the need for leader election entirely — plus O(1) position updates
>   computed in-process and fanned out over one shared Redis pub/sub subscription per worker.
> - **Made the queue abuse-resistant**: proved (with tests) that reconnecting or opening many tabs
>   can never improve a place in line — position is bound to an arrival sequence, not a connection —
>   and added a config-gated per-client join rate limit (an atomic-Lua fixed-window counter, 429 +
>   `Retry-After`) that keys on the real client IP behind a trusted proxy, rejecting spoofed
>   `X-Forwarded-For`. Honest about its limit: it bounds a single-source flood, not a botnet.

**Why this is stronger than it looks.** The third bullet contains a distinction most candidates
get wrong under questioning — overselling is a *concurrency* problem solved by atomicity;
double-booking is a *repetition* problem solved by idempotency. The fourth contains a
non-obvious result (an atomic rate-limiter removes leader election) that invites exactly the
follow-up you can answer. Neither needs a number.

**Added 2026-08-02, Phase 4 having landed** (this was the honest weak spot):

> - Proved oversell-safety under **true** concurrency — 60 barrier-released threads, one DB
>   connection each, against a 20-ticket event — and kept the test falsifiable by running it
>   against a deliberately broken read-modify-write, which oversells 3× while still satisfying
>   the database `CheckConstraint`.

**Why that second clause matters more than the first.** Any candidate can claim a concurrency
test. This one states what the test would look like when it *fails*, which is the difference
between having a test and having evidence.

**Added 2026-08-13, v1 + all three v2 picks having landed** (each with a run in `loadtest-report.md`,
each caveat kept):

> - **Streamed live queue position over SSE** to long-lived connections, computing each waiter's
>   position as in-memory arithmetic (`position = my_seq − admitted`) fanned out over **one** Redis
>   pub/sub subscription per process — and **measured** that steady-state Redis load stays flat (3
>   commands) whether 500 or 3,000 connections are open, because holding a connection costs no
>   Redis call, only opening one does (L1).
> - **Auto-tuned admission rate to the downstream's health** with an AIMD controller (the TCP
>   congestion-control law) that reads the booking service's p99 from Prometheus and shifts the
>   token-bucket rate to hold it near an SLO — **demonstrated the closed loop live** on the Docker
>   stack, backing off hard on overload and reclaiming capacity gently when healthy (L2).
> - **Scaled the queue statelessly**: 3 replicas behind a round-robin load balancer with no sticky
>   sessions — **demonstrated** that requests distribute evenly and a single admission broadcast
>   reaches SSE connections on every replica, because per-connection state is one integer re-readable
>   from Redis (L3).
> - **Shipped observability**: Prometheus (multiprocess-mode across gunicorn workers) + a Grafana
>   dashboard for queue depth, admissions, and booking latency — the same p99 the backpressure loop
>   consumes.

**Verbs, deliberately:** *streamed*, *auto-tuned*, *scaled*, *shipped* are earned; but each carries
its caveat in the ledger above (3,000 not 20K; overload induced via the target; replicas not nodes;
one laptop, not a throughput number). Say the capability, not a number the run did not produce.

---

## 4. The target version, and what unlocks it

Most of this is now unlocked. What remains gated is marked ⏳ — do not write those until the run
exists in [`loadtest-report.md`](loadtest-report.md).

| Target claim | Status | Note |
|---|---|---|
| "held N concurrent SSE connections" | 📏 **DONE at N=3,000** (L1) | write 3,000, single process; the 20K version needs multi-worker/multi-box |
| "per-connection Redis cost flat" | 📏 **DONE, 500→3,000** (L1) | 1K→10K still ⏳ (needs the multi-worker run) |
| "token-bucket admission auto-tuned to booking p99" | 📏 **DONE** (L2) | closed loop demonstrated live; overload was target-induced, say so |
| "N stateless replicas behind a round-robin LB" | 📏 **DONE at N=3** (L3) | **replicas**, not nodes; property shown, not throughput |
| "Prometheus + Grafana dashboards" | ✅ **DONE** (Phase 13) | add a screenshot to the README |
| "p99 position-update latency of N ms" | ⏳ | needs an end-to-end run with synced clocks (R8); say *what* the p99 measures |
| "graceful degradation under Redis Sentinel failover" | ⏳ | Sentinel not built; a failover during a live load test, with the graph |
| "k6 load test at N virtual users" | ⏳ | k6 not installed; N is what the generator actually sustains (not 100K on one box) |

---

## 5. The follow-up questions each claim invites

An interviewer picks the most specific thing on the line and pulls. This is what they will pull,
and whether it holds today.

| They ask | Today |
|---|---|
| "Walk me through how you got to 20K connections — what was the bottleneck?" | ⚠️ **Partial.** 20K is still unmeasured, but there is now an honest, specific answer: *"I've measured 3,000 on a single uvicorn worker with the generator on the same box — a floor, not a ceiling. The real finding is that the steady-state Redis load is flat (3 commands) whether 500 or 3,000 are connected, because position is in-memory arithmetic; the per-connection cost is at *open* (~6 Redis commands), not while holding. The next bottleneck to find is the single-process CPU — Gunicorn's 4–6 workers and a Linux box are the next run."* Do not say 20K. |
| "Why is a Redis pub/sub fan-out cheaper than one connection per client?" | ✅ Answerable now — `design.md` §8, and Redis `maxclients` defaults to 10,000 so the naive version does not even start |
| "What breaks if the Lua script isn't atomic?" | ✅ Answerable now — `design.md` §7 has the interleaving: over-admission, double admission, and a corrupted counter that silently breaks everyone's position |
| "Why a counter instead of a timestamp for ordering?" | ✅ Answerable now — collisions at 60K/min, clock skew across processes, and the density that position arithmetic requires |
| "How do you compute position for 20K people without hammering Redis?" | ✅ Answerable now — `position = my_seq − admitted`, with the three preconditions and what each breaks |
| "Horizontal scaling — sticky or stateless?" | ✅ Answerable now (L3) — **stateless**: 3 replicas behind a round-robin LB, requests distribute 6/6/6, and one admission broadcast reaches SSE streams on *every* replica because each runs its own subscriber. Bonus: I found and fixed a sticky default (single-upstream proxy pinning to one replica). |
| "How does the backpressure controller pick the rate?" | ✅ Answerable now (L2) — **AIMD**, the TCP congestion-control law: read booking p99 from Prometheus, multiplicative-decrease on overload, additive-increase when healthy, hold on no signal. Demonstrated live, 600→10 and 10→150. |
| "Can someone game the queue by reconnecting or opening tabs?" | ✅ Answerable now — **no**: position is bound to arrival sequence, not the connection (proven by test); plus a per-IP join rate limit that keys on the rightmost X-Forwarded-For hop so a spoofed header can't dodge it. Honest limit: bounds a single-source flood, not a botnet. |
| "You said middleware — show me." | ⚠️ **The code says authentication class and the decision log says you rejected middleware.** Fix the CV. |
| "How did you validate Sentinel failover?" | ❌ No answer exists |
| "Where did 100K virtual users run?" | ❌ No answer exists, and the honest answer is that the budgeted hardware could not do it |
| "How do you prevent overselling?" | ✅ Answerable and strong — atomic conditional UPDATE, contrasted with `SELECT FOR UPDATE`, plus the `CheckConstraint` backstop |
| "Is it idempotent? How?" | ✅ Answerable — unique `token_jti`, replay returns the original with 200 |
| "Did you test the oversell path concurrently?" | ✅ **Yes** — `test_concurrency.py`: 60 barrier-released threads at a capacity-20 event, exactly 20 succeed. And the mutation proof: the broken read-modify-write records `tickets_booked=1` against 60 booking rows, which the `CheckConstraint` happily permits |

---

## 6. Update protocol

1. A phase lands → move its rows from ⏳/📐 to ✅ here, in the same commit.
2. A load test runs → record it in [`loadtest-report.md`](loadtest-report.md) **first**, then move
   the row to 📏 here with the number.
3. A claim is reworded on the CV → update the verbatim quote in §1 in the same sitting, or this
   file starts lying too.
4. **Re-audit before sending the CV anywhere.** Update the "Last audited" line with the commit.

Anything that is ❌ or ⚠️ at the moment of sending does not go in the document being sent.
