# QueueFair — Load Test Report

> **Every performance number this project claims, and the run that produced it.**
>
> CLAUDE.md Rule 7: *no performance claim without a number; no number without a run.*
> [`resume-claims.md`](resume-claims.md) may only cite numbers that appear in this file.
>
> **If a number is not in this file, it does not exist.** Not on the CV, not in the README, not
> in an interview answer. "About 20 thousand" is not a number; it is a memory of a number.

**Status:** 📏 **three runs recorded (2026-08-13).** L1 — a local single-process *floor*: 3,000 SSE
connections held on one uvicorn worker, steady-state Redis command volume flat (3) across
500 → 3,000 connections. L2 — the dynamic-backpressure *closed loop*, live: the AIMD controller drives
the real admission rate from a real booking p99 both ways (overload → 600→10, healthy → 10→150).
L3 — *stateless horizontal scaling*: 3 replicas behind a round-robin LB, requests distributed 6/6/6,
and one admission broadcast reached SSE streams on all three replicas (2/2/2). The multi-box /
high-N / p99-latency runs (R8–R12) are still ⏳.
**Last updated:** 2026-08-13

---

## 1. The recording rule

Every run gets an entry in §5 containing, without exception:

1. **Date and commit SHA** — a number without a commit is not reproducible
2. **The exact command**, copy-pasteable
3. **The environment** — hardware, `ulimit -n`, `somaxconn`, worker count, Redis version
4. **Raw output**, pasted, not summarised
5. **What we expected, and whether we were wrong** — a run that confirmed a prediction and a run
   that demolished one are equally worth recording, and the second is worth more

A run whose result is "it fell over at 3,000 connections" is a **good entry**. It is the number,
it is honest, and the reason it fell over is the interesting part. Deleting a disappointing run
is how a report becomes marketing.

---

## 2. Preconditions — do these before the first run

Skipping any of these produces a number that measures the *test harness*, not the system.

| Precondition | Why | Command |
|---|---|---|
| `ulimit -n` raised | Default ~1024 file descriptors caps you at ~1000 connections, and the failure looks like the app breaking | `ulimit -n 65535` |
| `somaxconn` raised | Default listen backlog drops connections during a ramp; shows up as spurious errors | `sysctl -w net.core.somaxconn=65535` |
| Ephemeral port range widened **on the load generator** | One client box runs out of source ports around 28K connections to one destination | `sysctl -w net.ipv4.ip_local_port_range="1024 65535"` |
| `tcp_tw_reuse` | TIME_WAIT exhaustion on repeated short-connection runs | `sysctl -w net.ipv4.tcp_tw_reuse=1` |
| Load generator **not** on the box under test | Otherwise you are measuring two systems competing for one CPU | separate instance |
| `DEBUG = False` | Django's debug machinery is not free | settings |
| Redis `maxclients` checked | Defaults to 10,000 — silently the ceiling on any naive per-client-connection design | `CONFIG GET maxclients` |

Record which of these were applied **in each entry**. A run with default `ulimit` is not a
failure of the system, and mislabelling it as one wastes a day.

---

## 3. What each target measures

Vague targets produce numbers nobody can defend. These are the definitions.

| Target | Definition — precisely what is being timed or counted |
|---|---|
| **Concurrent SSE connections** | Simultaneously *open* `text/event-stream` responses that have received a frame in the last 30 s. Not connections attempted, not sockets in any state. |
| **Position-update p99** | Wall-clock from the admission script committing in Redis to the client's `onmessage` firing. Requires a timestamp inside the message and clock-synced boxes — **if the clocks are not synced, this number is fiction.** |
| **Join p99** | `POST /join` request to response, at the load generator |
| **Redis ops/sec** | `INFO commandstats` delta over a fixed window, divided by the window. Broken out per command. |
| **Memory per connection** | (RSS at N connections − RSS at 0) ÷ N, after a settle period. Report the settle period. |
| **Admission exactness** | Passes issued in a window ÷ configured rate. Must be exactly 1.00, not "about right". |
| **Oversell safety** | Bookings recorded vs capacity, under parallel load. Must be exact. |

---

## 4. Standing results

Populated as runs happen. Every ⏳ here corresponds to a row in
[`design.md`](design.md) §13 and a claim in [`resume-claims.md`](resume-claims.md).

| # | Target | Result | Run | Phase |
|---|---|---|---|---|
| R1 | Oversell safety under true parallel load | ⏳ | — | 4 |
| R2 | Join idempotency under concurrent duplicate joins | ⏳ | — | 6 |
| R3 | Admission exactness, 3 processes admitting concurrently | ⏳ | — | 8 |
| R4 | Concurrent SSE connections, single process | 📏 **3,000 held, 0 failed** (local floor, not a ceiling) | [L1](#l1--sse-connection-flatness--single-process-local-floor--2026-08-13) | 10 |
| R5 | Redis subscriptions per process at 500 connections | 📏 **1** (unit-tested; implied by L1's flat hold) | [L1](#l1--sse-connection-flatness--single-process-local-floor--2026-08-13) | 10 |
| R6 | Redis ops/sec flat from 1K → 10K connections | 📏 **flat across 500→3,000** (steady hold = 3 cmds at every level); 1K→10K still ⏳ | [L1](#l1--sse-connection-flatness--single-process-local-floor--2026-08-13) | 11 |
| R7 | Memory per SSE connection | ⏳ | — | 11 |
| R8 | Position-update p99, end to end | ⏳ | — | 14 |
| R9 | Heartbeat cost at 10K connections (with vs without) | ⏳ | — | 14 |
| R10 | Concurrent SSE connections, 3 processes behind Nginx | ⏳ | — | 15 |
| R11 | Behaviour during Redis Sentinel failover under load | ⏳ | — | 16 |
| R12 | Django ASGI per-request overhead vs Starlette, same endpoint | ⏳ | — | 14 |

**R12 exists because the framework choice must be falsifiable.** `decisions.md` (2026-08-02)
picks Django over FastAPI partly on the argument that the framework is not the bottleneck. R12 is
the run that either supports that or forces the entry to be superseded. A decision with no test
that could refute it is an opinion.

---

## 5. Run log

Newest first.

### L3 — Horizontal scaling: stateless, cross-replica SSE fan-out — 2026-08-13

**Commit:** working tree on `3f1ae86`

**Hypothesis (before the run):** the queue service is stateless (design.md §9), so N replicas behind
a round-robin load balancer need no sticky sessions: any replica serves any client from shared Redis,
and an admission published once reaches SSE connections on EVERY replica (design.md §8 — one pub/sub
subscriber per process, fanning out to that process's own connections).

**Environment:** full Docker stack, queue scaled to 3 replicas (`docker compose up -d --scale
queue=3`), Caddy load-balancing the queue upstream via `dynamic a` (re-resolve `queue` every 5s,
`lb_policy round_robin`) on one Windows laptop.

**The finding that forced the LB change (the "sticky vs stateless" crux):** the original
`reverse_proxy queue:8001` resolves the name once and pins to a single replica — 18 requests all
landed on `queue-1`, i.e. effectively **sticky**. Switching to `dynamic a` (re-resolve + round-robin)
made it genuinely stateless.

**Raw output**
```
Load balancing — 18 position GETs through Caddy:
  before (reverse_proxy queue:8001):  queue-1=18  queue-2=0  queue-3=0   (sticky, one upstream)
  after  (dynamic a + round_robin):   queue-1=6   queue-2=6  queue-3=6   (distributed)

Cross-replica SSE fan-out — 6 streams through Caddy, then ONE admitter admits the batch:
  streams accepted per replica:  queue-1=2  queue-2=2  queue-3=2
  admitter: "admitted 6"  (one Redis PUBLISH)
  ...0001: admitted_frame=True   ...0002: admitted_frame=True   ...0003: admitted_frame=True
  ...0004: admitted_frame=True   ...0005: admitted_frame=True   ...0006: admitted_frame=True
  => 6/6 streams received an 'admitted' frame after one admission
```

**Result:** stateless horizontal scaling confirmed. The LB distributes clients with no affinity, and
a single admission broadcast reaches SSE connections on all three replicas — because every replica
runs its own subscriber (design.md §8), including the two the admitter was NOT running on. No sticky
sessions are needed, exactly as design.md §9 argues.

**Caveat:** 3 replicas on one laptop proves the stateless *property*, not a throughput number (all
replicas share one host's CPU and one Redis). Also, Prometheus still scrapes a single replica (static
target `queue:8001`), so per-replica aggregate metrics would need DNS service discovery — noted, not
built. Reproduce with `loadtest/demo_sse_scale.py` after seeding 6 hex tokens into the queue.

**Claims this unlocks:** "horizontal scaling behind a load balancer, stateless / no sticky sessions"
moves from 📐 DESIGNED to 📏 **MEASURED** — the *property* is demonstrated (distribution + cross-replica
fan-out), not a throughput figure.

### L2 — Dynamic backpressure closed loop, live on the Docker stack — 2026-08-13

**Commit:** working tree on `3f1ae86` (backpressure + abuse mitigation uncommitted)

**Hypothesis (before the run):** the AIMD controller, reading the booking p99 from Prometheus, will
multiplicatively DECREASE the admission rate when p99 exceeds the target and additively INCREASE it
when under — driven by a *real* p99 from a live booking service, not a fake. Both directions should
move the actual `rate_per_min` that admit_batch.lua enforces.

**Environment**
| | |
|---|---|
| Stack | full `docker compose` on one Windows laptop: redis, postgres, queue (gunicorn 4× UvicornWorker), booking (gunicorn 4×), caddy, prometheus, grafana |
| Booking under load | sustained ~61 req/s at concurrency 40 (idempotent replays of one admission pass through Caddy), booking = 4 gunicorn workers |
| p99 source | Prometheus scraping `booking_request_seconds`, queried as `histogram_quantile(0.99, sum(rate(..._bucket[1m])) by (le))` |
| Observed booking p99 | **0.056 s**, rising to **0.098 s** under sustained load |
| Controller | `run_backpressure --once` per tick; defaults rate_min 10, rate_max 600, step 20, factor 0.5 |

**Commands**
```
docker compose exec queue   python manage.py create_event coldplay-mumbai-2026 --rate-per-min 600 --burst 50 --batch-max 20
docker compose exec booking python manage.py mint_token --event coldplay-mumbai-2026 --user loadtest-user
# sustained booking load (loadtest/booking_load.py) through Caddy, then per tick:
docker compose exec -e BACKPRESSURE_TARGET_P99_SECONDS=<t> queue python manage.py run_backpressure coldplay-mumbai-2026 --once
```

**Raw output**
```
booking p99 (Prometheus) = 0.0565 s at 61 req/s

Phase A — target 0.02s (< observed 0.056s) => OVERLOAD => multiplicative decrease
  start rate: 600/min
  tick 1: rate now 300/min
  tick 2: rate now 150/min
  tick 3: rate now 75/min
  tick 4: rate now 37/min
  tick 5: rate now 18/min
  tick 6: rate now 10/min
  tick 7: rate now 10/min      (floored at rate_min)

Phase B — target 0.2s (> observed 0.056–0.098s) => HEALTHY => additive increase (+20/tick)
  start rate: 10/min
  tick 1: rate now 30/min
  tick 2: rate now 50/min
  tick 3: rate now 70/min
  tick 4: rate now 90/min
  tick 5: rate now 110/min
  tick 6: rate now 130/min
  tick 7: rate now 150/min
  booking p99 now = 0.0984 s
```

**Result:** the closed loop is real and works in both directions. Overload (target below the live
p99) drove the rate `600 → 10` by halving; health (target above it) drove `10 → 150` by +20/tick.
The path exercised end to end: Prometheus scrape → `histogram_quantile` → controller decision →
`HSET rate_per_min` in the config hash → the rate admit_batch.lua reads on its next tick.

**Was the hypothesis right?** Yes, both directions, against a real Prometheus p99.

**Bottleneck / the honest caveat:** the *target* was used as the knob to cross the observed p99 in
each direction, because a mock-fast booking service (56–98 ms) sits comfortably under the 200 ms SLO
on this hardware. That is a legitimate demonstration of the control **law** — the controller cannot
tell *why* p99 exceeds the target, only that it does — but it is NOT a demonstration that booking's
p99 actually climbs to 200 ms under a real stampede. Showing that would need a genuinely slower
booking path, or far more concurrency than one laptop sustains. Also: controller run as discrete
`--once` ticks, not the continuous loop (identical logic, one call per tick).

**Claims this unlocks:** dynamic backpressure moves from ✅ SHIPPED to 📏 **MEASURED** — the closed
loop (booking p99 → admission rate) is demonstrated live end to end. The caveat travels with the
claim: overload was induced via the target, not by real booking saturation.

### L1 — SSE connection flatness — single process, local floor — 2026-08-13

**Commit:** `3f1ae86` (working tree; this run added `loadtest/load.py` and these docs on top of it)

**Hypothesis (written before the run):** design.md §6 claims ongoing Redis command volume is
independent of the number of connected SSE clients, because position is arithmetic in memory
(`position = my_seq − admitted_total`) and the fan-out is one `PSUBSCRIBE` per process, not per
connection. So: *opening* N connections should cost O(N) Redis calls (each connect does one
authoritative `position.lua` lookup), but *holding* them should cost ~0 per client per tick — a
steady-state count that stays flat whether 500 or 3,000 connections are open. On a single uvicorn
worker with the load generator sharing the CPU, I expected the connection count to be a floor
(reached cleanly, no failures) rather than a real ceiling, and join latency to be dominated by
one event loop's queuing.

**Environment**
| | |
|---|---|
| Target host | local dev box (Windows 11 laptop), **single `uvicorn` worker** — NOT Gunicorn, NOT the 4–6 workers of the real config |
| Load generator | **same box** as the target — this invalidates any *absolute* ceiling (client and server compete for the same CPU); it does not invalidate the *flatness* result, which is a ratio |
| Workers | 1 uvicorn process (`uvicorn queue_service.asgi:application --port 8001`) |
| Redis | 7.x, native on `localhost:6379` (not Docker) |
| `ulimit -n` / `somaxconn` | N/A — Windows; none of the §2 Linux preconditions apply |
| `DEBUG` | `False` (queue-service default) |
| Preconditions §2 applied | **none** of the Linux tuning; **generator on-box** (a known, recorded cap per §2) |
| Load client | `loadtest/load.py` (pure asyncio, no k6 — k6 not yet installed locally) |

**Command**
```
python loadtest/load.py join 2000 200
python loadtest/load.py sse 500 12
python loadtest/load.py sse 1500 12
python loadtest/load.py sse 3000 12
```

**Raw output**
```
JOIN  n=2000 concurrency=200: 2000 ok in 3.80s = 526 req/s
      latency ms: p50=356.9  p95=487.5  p99=631.1  max=667.7

--- SSE load: 500 connections, 12.0s steady hold ---
JOIN  n=500 concurrency=250: 500 ok in 0.83s = 604 req/s
      latency ms: p50=345.4  p95=520.4  p99=552.7  max=559.7
SSE   opened 500 / attempted 500 (failed 0)
      Redis commands to OPEN 500 connections: 3005  (~6 each: one position.lua EVALSHA + its 5 internal calls)
      Redis commands DURING 12.0s steady hold of 500 connections: 3

--- SSE load: 1500 connections, 12.0s steady hold ---
JOIN  n=1500 concurrency=250: 1500 ok in 2.62s = 572 req/s
      latency ms: p50=425.3  p95=474.5  p99=509.5  max=510.4
SSE   opened 1500 / attempted 1500 (failed 0)
      Redis commands to OPEN 1500 connections: 9005  (~6 each: one position.lua EVALSHA + its 5 internal calls)
      Redis commands DURING 12.0s steady hold of 1500 connections: 3

--- SSE load: 3000 connections, 12.0s steady hold ---
JOIN  n=3000 concurrency=250: 3000 ok in 5.76s = 521 req/s
      latency ms: p50=457.6  p95=591.1  p99=597.1  max=599.9
SSE   opened 3000 / attempted 3000 (failed 0)
      Redis commands to OPEN 3000 connections: 18005  (~6 each: one position.lua EVALSHA + its 5 internal calls)
      Redis commands DURING 12.0s steady hold of 3000 connections: 3
```

**Result**

- **R4 — concurrent SSE connections, single process:** 3,000 opened, 3,000 held, **0 failed**. This
  is a *floor*, not a ceiling — the run never provoked a failure, so the real single-process limit
  is higher and unmeasured. (Definition §3: simultaneously open `text/event-stream` responses.)
- **R6 — Redis-op flatness (the headline):** Redis commands during a 12 s steady hold were **3 at
  500, 3 at 1,500, and 3 at 3,000 connections** — flat, independent of connection count. Holding a
  connection costs ~0 Redis calls per tick; the "3" is measurement-harness noise (the client's own
  `INFO commandstats` probes), not per-connection cost. This is the arithmetic-position claim of
  design.md §6, measured. **Caveat:** the 12 s window is shorter than the 30 s reconciliation
  interval, so it measures the *between-reconcile floor*; over a longer window each connection adds
  one `ZRANK` per 30 s reconcile — bounded and deliberate (design.md §6), still flat per *tick*.
- **Open cost is O(connections), ~6 Redis commands each:** 3005 / 9005 / 18005 for 500 / 1,500 /
  3,000 (= 6.0 per connection). Each connect performs one authoritative `position.lua` lookup;
  `INFO commandstats` counts that as 6 — the `EVALSHA` wrapper plus its 5 internal `redis.call()`s
  (`EXISTS`, `ZRANK`, `GET admitted`, `HGET rate_per_min`, `ZCARD`). Logically one query; six
  counted commands. This is the honest cost the "flat steady state" is traded against.
- **Join throughput (single worker, warm):** 2,000 joins in 3.80 s = **526 req/s**, p50 = 357 ms,
  p99 = 631 ms, max = 668 ms. The per-SSE-run join phases agree (521–604 req/s). Definition §3:
  `POST /join` request→response at the generator.

**Was the hypothesis right?** Yes on both counts. Steady-state Redis ops are flat (3) at every
connection level; open cost is linear at ~6 commands/connection. The one correction to my own
tooling: the load client had labelled open cost "~1 each (the ZRANK)" — the measured 6.0/connection
proved that wrong and I fixed the label to name all five internal calls plus the `EVALSHA`.

**Bottleneck:** the test harness, not the system. (1) The load generator ran on the same box as
the server — §2 says this caps absolute numbers, so 3,000 is a floor. (2) A single uvicorn worker
serialises the join path through one event loop; the real config is Gunicorn with 4–6 workers,
which this run did not exercise. Nothing here measured a *system* limit — it measured how far one
laptop process gets, and proved the shape of the Redis cost curve (flat hold, linear open).

**Claims this unlocks (→ 📏 MEASURED in `resume-claims.md`, with the "single process, local,
generator on-box" caveat attached):**
- "Held 3,000 concurrent SSE connections on a single process, 0 dropped" — floor.
- "Steady-state Redis command volume flat across a 6× increase in concurrent connections
  (500 → 3,000), because position is served from in-memory arithmetic, not per-client queries."

**Claims this does NOT unlock (still ⏳ — do not put these anywhere):** 20K connections; any
multi-process or multi-box number; p99 < 200 ms end-to-end (R8 needs synced clocks); k6 at any
scale; Sentinel failover; dynamic backpressure. Those are unbuilt or unmeasured.

<!--
Copy this template for each run. Do not summarise the output — paste it.

### R{n} — {what was measured} — {YYYY-MM-DD}

**Commit:** `{sha}`
**Hypothesis:** {what we expected, and why — written BEFORE the run}

**Environment**
| | |
|---|---|
| Target host | t4g.small (2 vCPU ARM, 2 GB) / local dev box — say which |
| Load generator | separate instance / same box (say so — it invalidates the number) |
| Workers | {n} Gunicorn UvicornWorker |
| Redis | {version}, {where} |
| `ulimit -n` | {value} |
| `somaxconn` | {value} |
| Preconditions §2 applied | {list, or "all"} |

**Command**
```bash
{exact command}
```

**Raw output**
```
{paste it}
```

**Result:** {the number, with its unit and its definition from §3}

**Was the hypothesis right?** {yes / no / partly — and what the surprise was}

**Bottleneck:** {what actually limited it — CPU, Redis RTT, memory, fds, the load generator
itself. "The load generator was the bottleneck" is a legitimate and common answer, and a run
that hit it measured nothing about the system.}

**Claims this unlocks:** {rows in resume-claims.md that may now move to 📏 MEASURED}
-->

---

## 6. Measurement traps

Recorded before they cost a day each.

- **The load generator is the bottleneck more often than the system is.** Before believing a
  ceiling, check the generator's CPU, its fd limit, and its ephemeral port range. A "20K
  connection limit" that is really one k6 box running out of source ports is the single most
  common false result in this kind of test.
- **k6 does not do 100K VUs on one machine.** Roughly 10–30K on a well-provisioned box. Anything
  larger needs distributed execution, which the budget in CLAUDE.md §7 does not cover. Plan the
  number around the hardware, not the other way around.
- **p99 latency across two machines requires synchronised clocks.** Without NTP discipline the
  number is noise shaped like data. Either sync, or measure round-trip from one box only.
- **SSE connections idle at almost zero CPU**, so a connection-count test can pass beautifully
  and tell you nothing about behaviour under admission churn. Test both: connections held, and
  connections held *while admissions are firing*.
- **Measure Redis with `INFO commandstats`, not with a stopwatch on the client.** Client-side
  timing includes Python, the event loop, and the network; you need to know which one moved.
- **Warm up before measuring.** First-request costs (script loading into Redis, connection pool
  fill, JIT-less Python import) distort a short run.
- **Run each configuration more than once.** A single run on shared cloud hardware has noisy
  neighbours in it.
- **Record the failure mode, not just the ceiling.** "Stopped at 8K" is half a result;
  "stopped at 8K because RSS hit the 2 GB instance limit at ~240 KB/connection" is a finding, and
  it is the sentence that goes in an interview.

---

## 7. Cost note

Load-testing means running infrastructure, and CLAUDE.md §7 caps spend at ₹500/month with a ₹250
target. Before any run on AWS:

- Use a **spot** instance for the load generator; terminate it in the same session
- `terraform destroy` immediately after the run — the AMI and S3 bucket survive, nothing else
- Check the Budgets alarm (₹300) is live *before* the first `apply`, not after

A load test that runs overnight because nobody tore it down is the most likely way this project
exceeds its budget.
