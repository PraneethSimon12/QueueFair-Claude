# QueueFair — Documentation Map

Nine files. Each answers exactly one question, so nothing has to be duplicated and nothing goes
stale in two places at once.

| I want to know… | Read |
|---|---|
| What the system does, and what it promises users | [`product-spec.md`](product-spec.md) |
| Why it is built this way | [`design.md`](design.md) |
| How it works at runtime, end to end (the journey) | [`flow.md`](flow.md) |
| How to build it, and the exact request/response for every endpoint | [`build-plan.md`](build-plan.md) |
| Why we chose X over Y | [`decisions.md`](decisions.md) |
| What a concept means, or how to answer a question about it | [`interview-prep.md`](interview-prep.md) |
| Whether a claim on the CV is actually true | [`resume-claims.md`](resume-claims.md) |
| What a performance number really was | [`loadtest-report.md`](loadtest-report.md) |
| How Claude and I are supposed to work on this | [`../CLAUDE.md`](../CLAUDE.md) |

---

## Precedence

When two documents disagree, **behaviour beats design beats wire format**:

```
product-spec.md   ─┐
                   ├─►  design.md  ─►  build-plan.md
   (behaviour)     │     (design)      (wire format)
                   │
              wins over ──────────────────────────►
```

The loser gets corrected in the same sitting. A spec that is known to be wrong is worse than no
spec, because people trust it exactly once.

---

## Which file gets which kind of change

| The change | Goes in |
|---|---|
| New or changed endpoint, payload, or response | `build-plan.md` §3 **and** its change log |
| A decision, or a deliberate exception to a principle | `decisions.md` (append-only) — **automatic, per CLAUDE.md Rule 8** |
| A new functional requirement | `design.md` §2, plus `product-spec.md` if users can see it |
| A concept explained, or an understanding-check Q&A | `interview-prep.md` — **automatic, per CLAUDE.md Rule 9** |
| A phase finished | tick it in `build-plan.md` §6 and update `resume-claims.md` |
| A load test run | `loadtest-report.md` §5 **first**, then `resume-claims.md` |
| A reworded CV bullet | `resume-claims.md` §1, same sitting |

**ADRs live in `decisions.md` only.** `design.md` links to them and never restates them — two
copies of a decision is the most reliable way to end up with two different decisions.

---

## The two rules that exist because they were being broken

**No number leaves `loadtest-report.md`.** Not to the CV, not to the README, not to an interview
answer. CLAUDE.md Rule 7 said this already; it had no artifact behind it, so the CV drifted about
a version ahead of the repo. [`resume-claims.md`](resume-claims.md) §2 has the current count.

**Verbs are load-bearing.** *Designed* means there is a design doc. *Built* means there is code
and tests. *Measured* means there is a run in the report. They are not synonyms and an
interviewer reads them as precisely as this file does.

---

## Current state, in one line

**Phases 0–13 of 15 built** — v0 (join → admit → book) plus v1's SSE + reconciliation, the whole
stack in `docker compose` behind Caddy on one origin, and Prometheus + Grafana. A real person can
queue, refresh without losing their place, watch their position fall over SSE, be admitted at a
controlled rate, and book — end to end, with a forged pass rejected. The three races the design is
about (oversell, queue-jumping on join, over-admission) are each demonstrated failing against a
deliberately broken implementation and then holding.

Still ahead: **Phase 14, the k6 load tests — so [`loadtest-report.md`](loadtest-report.md) is empty
and no performance number may leave it.** See [`build-plan.md`](build-plan.md) §6.
