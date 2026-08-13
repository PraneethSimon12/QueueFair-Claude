"""Async views for the queue service.

Every view here is `async def`. A sync view in this module would be run by Django on a thread
from the ASGI thread pool, which is the same resource exhaustion described on MIDDLEWARE in
settings.py — just arriving from the other direction.

These views are thin on purpose: parse the request, call one repository method, shape the
response. The atomicity lives in Lua, the arithmetic lives in core/, and neither is HTTP's
business.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

from asgiref.sync import sync_to_async
from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse, StreamingHttpResponse
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    generate_latest,
    multiprocess,
)
from redis.exceptions import RedisError

from adapters.broadcaster import QUEUE_MAXSIZE, get_broadcaster
from adapters.metrics import POSITION_DRIFT, SSE_CONNECTIONS
from adapters.queue_repository import UnknownEvent, UnknownToken, get_queue_repository
from adapters.redis_client import redis_is_healthy
from core.state import (
    AdmittedOutcome,
    PositionOutcome,
    QueueState,
    WaiterState,
    clamp_position,
    eta_seconds_for,
    position_from,
    state_from,
    state_from_admission,
    state_from_position,
)
from core.validation import is_valid_event_id, is_valid_queue_token, new_queue_token

# The v0 waiting-room page, served by THIS service so the browser's join/position calls are
# same-origin. The service is otherwise JSON/SSE only; it carries this one static page because its
# MIDDLEWARE=[] invariant rules out putting CORS on it, so the page must share its origin. Read
# once at import — never blocking file IO inside an async view (settings.py MIDDLEWARE note). In
# production (Phase 12) Caddy serves the frontend and the `index` route is removed. Logged in
# decisions.md, 2026-08-02.
_INDEX_PATH = Path(settings.BASE_DIR).parent / "frontend" / "index.html"
_INDEX_HTML = (
    _INDEX_PATH.read_bytes()
    if _INDEX_PATH.exists()
    else b"<!doctype html><title>QueueFair</title><h1>frontend/index.html not found</h1>"
)


async def index(request: HttpRequest) -> HttpResponse:
    """GET / — the vanilla waiting-room page (v0 dev only). See the note above for why it is here."""
    if request.method != "GET":
        return HttpResponse(status=405)
    return HttpResponse(_INDEX_HTML, content_type="text/html; charset=utf-8")


async def healthz(request: HttpRequest) -> JsonResponse:
    """GET /healthz — can this process actually do its job?

    Checks Redis, not just liveness. This service holds no state of its own; a process that
    cannot reach Redis knows nobody's position and can admit nobody, so it is unhealthy no
    matter how well the process itself is running (build-plan §3.1).

    Returns: 200 {"status":"ok","redis":"ok"} · 503 {"status":"degraded","redis":"unavailable"}
    """
    if await redis_is_healthy():
        return JsonResponse({"status": "ok", "redis": "ok"})
    return JsonResponse({"status": "degraded", "redis": "unavailable"}, status=503)


async def metrics(request: HttpRequest) -> HttpResponse:
    """GET /metrics — Prometheus text format, aggregated across every worker process.

    Multiprocess collection reads files from PROMETHEUS_MULTIPROC_DIR, so it runs in a thread
    (sync_to_async) to keep blocking IO off the event loop (settings.py / CLAUDE.md §4).
    """
    if request.method != "GET":
        return HttpResponse(status=405)
    payload = await sync_to_async(_render_metrics)()
    return HttpResponse(payload, content_type=CONTENT_TYPE_LATEST)


def _render_metrics() -> bytes:
    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)
    return generate_latest(registry)


async def join(request: HttpRequest, event_id: str) -> JsonResponse:
    """POST /api/queue/{event_id}/join — take a place in line, or resume the one you hold.

    Idempotent (FR-2, FR-3, fairness promise F2): rejoining with a token that is already queued
    returns its existing sequence and changes nothing. `joined` is the only field that differs,
    and the status is 200 either way — a resume creates nothing, and a client must not be able
    to tell the two apart from the status code (build-plan §3.1).

    Returns: 200 with the join body · 404 unknown_event · 405 · 503 redis_unavailable
    """
    if request.method != "POST":
        # Checked inline rather than with @require_POST. With MIDDLEWARE = [] the view is the
        # only place dispatch can happen, and an explicit branch has no decorator that might
        # one day wrap this coroutine in something synchronous.
        return JsonResponse({"detail": "method_not_allowed"}, status=405)

    if not is_valid_event_id(event_id):
        # Same 404 as an event that does not exist. A malformed id and an unknown one are the
        # same thing to a client, and separating them would leak which events exist.
        return JsonResponse({"detail": "unknown_event"}, status=404)

    # A malformed token is treated as no token at all, and the waiter is minted a fresh one.
    # Join is the entry point to the system: answering the front door with a 404 because the
    # client sent us garbage it has no way to fix would strand them permanently.
    queue_token = _queue_token_from(request, event_id) or new_queue_token()

    try:
        outcome = await get_queue_repository().join(event_id, queue_token)
    except RedisError:
        # The v1 single point of failure, stated as such in design.md §11. 503, not 500: this is
        # correctly-functioning software reporting an accurate fact about its dependency.
        return JsonResponse({"detail": "redis_unavailable"}, status=503)

    if outcome is None:
        return JsonResponse({"detail": "unknown_event"}, status=404)

    state = state_from(outcome)
    response = JsonResponse(
        {
            "queue_token": queue_token,
            "sequence": outcome.sequence,
            **state.as_dict(),
            "joined": outcome.joined,
        }
    )
    _set_queue_token_cookie(response, event_id, queue_token)
    return response


async def position(request: HttpRequest, event_id: str) -> JsonResponse:
    """GET /api/queue/{event_id}/position — where am I, authoritatively?

    The v0 polling endpoint. It survives after SSE lands (Phase 10) as the debugging and fallback
    path, and as the reference the cheap position arithmetic is reconciled against (design.md §6).

    Returns: 200 QueueState · 404 unknown_event · 404 unknown_token · 405 · 503
    """
    if request.method != "GET":
        return JsonResponse({"detail": "method_not_allowed"}, status=405)

    if not is_valid_event_id(event_id):
        return JsonResponse({"detail": "unknown_event"}, status=404)

    queue_token = _queue_token_from(request, event_id)
    if queue_token is None:
        # 404 unknown_token, not 400. A malformed token and an unknown one are indistinguishable
        # to a client, and answering differently would confirm whether a given token exists
        # (build-plan §8). Unlike /join, there is nothing useful to do with a caller who has no
        # valid token here — minting one would invent a place in line they never queued for.
        return JsonResponse({"detail": "unknown_token"}, status=404)

    try:
        outcome = await get_queue_repository().position(event_id, queue_token)
    except UnknownEvent:
        return JsonResponse({"detail": "unknown_event"}, status=404)
    except UnknownToken:
        return JsonResponse({"detail": "unknown_token"}, status=404)
    except RedisError:
        return JsonResponse({"detail": "redis_unavailable"}, status=503)

    if isinstance(outcome, AdmittedOutcome):
        # v0 delivers passes by polling, so the admission payload rides along with the state.
        # Phase 10's SSE `admitted` frame makes this the fallback rather than the main path, but
        # it stays: a waiter who was mid-reconnect at the moment of admission finds their pass
        # here and nowhere else.
        body = state_from_admission(outcome).as_dict()
        body["admission"] = outcome.admission
        return JsonResponse(body)

    return JsonResponse(state_from_position(outcome).as_dict())


async def stream(request: HttpRequest, event_id: str) -> StreamingHttpResponse | JsonResponse:
    """GET /api/queue/{event_id}/stream — the waiter's position, live, over SSE.

    Replaces polling: one long-lived connection receives a `position` frame whenever an admission
    batch moves the queue, and a final `admitted` frame carrying the pass. Anything knowable before
    the stream opens is a normal HTTP status; once it is open the status line is already sent, so
    there is nothing left to report but frames (build-plan §3.1).

    Returns: 200 text/event-stream · 404 unknown_event / unknown_token · 405 · 503.
    """
    if request.method != "GET":
        return JsonResponse({"detail": "method_not_allowed"}, status=405)
    if not is_valid_event_id(event_id):
        return JsonResponse({"detail": "unknown_event"}, status=404)

    queue_token = _queue_token_from(request, event_id)
    if queue_token is None:
        return JsonResponse({"detail": "unknown_token"}, status=404)

    # Resolve the waiter's situation ONCE, before opening the stream, so a bad token is a clean 404
    # instead of a stream that opens and immediately dies.
    try:
        outcome = await get_queue_repository().position(event_id, queue_token)
    except UnknownEvent:
        return JsonResponse({"detail": "unknown_event"}, status=404)
    except UnknownToken:
        return JsonResponse({"detail": "unknown_token"}, status=404)
    except RedisError:
        return JsonResponse({"detail": "redis_unavailable"}, status=503)

    response = StreamingHttpResponse(
        _event_stream(event_id, queue_token, outcome),
        content_type="text/event-stream",
    )
    response["Cache-Control"] = "no-cache"
    # Without this a reverse proxy (Nginx/Caddy) buffers the response and the browser receives
    # nothing until the stream ends — which for SSE is never (CLAUDE.md §8, build-plan §3.1).
    response["X-Accel-Buffering"] = "no"
    return response


async def _event_stream(
    event_id: str, queue_token: str, outcome: PositionOutcome | AdmittedOutcome
) -> AsyncIterator[str]:
    """The SSE body. Yields frames until the waiter is admitted or the browser disconnects.

    Position is arithmetic on a pinned sequence (design.md §6), recomputed from each broadcast with
    zero Redis per tick. Every SSE_RECONCILE_SECONDS the stream re-checks the authoritative ZRANK
    and re-pins the sequence, correcting the drift abandonment introduces — the one per-connection
    Redis cost of this path. The displayed position is clamped so a correction never moves it up.
    """
    # Tell EventSource to reconnect 3s after a drop, rather than its uncontrolled default.
    yield "retry: 3000\n\n"

    if isinstance(outcome, AdmittedOutcome):
        # Already admitted at connect — hand over the pass and close. Nothing more to stream.
        yield _frame("admitted", outcome.admission)
        return

    sequence = outcome.position + outcome.admitted_total
    last_shown = outcome.position
    admitted_total = outcome.admitted_total
    total_waiting = outcome.total_waiting
    rate_per_min = outcome.rate_per_min
    yield _frame("position", state_from_position(outcome).as_dict())

    broadcaster = get_broadcaster()
    inbox: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
    broadcaster.register(event_id, inbox)
    SSE_CONNECTIONS.inc()
    monotonic = asyncio.get_running_loop().time
    next_reconcile = monotonic() + settings.SSE_RECONCILE_SECONDS
    try:
        while True:
            fresh = False
            try:
                update = await asyncio.wait_for(inbox.get(), timeout=settings.SSE_HEARTBEAT_SECONDS)
                admitted_total = int(update["admitted_total"])
                total_waiting = int(update["total_waiting"])
                rate_per_min = int(update["rate_per_min"])
                fresh = True
            except asyncio.TimeoutError:
                yield ": ping\n\n"  # heartbeat: stop idle proxies dropping the connection

            # Periodic reconciliation against the authoritative ZRANK. Re-pinning the sequence here
            # corrects the abandonment drift the cheap arithmetic accumulates (design.md §6).
            if monotonic() >= next_reconcile:
                next_reconcile = monotonic() + settings.SSE_RECONCILE_SECONDS
                truth = await _reconcile(event_id, queue_token)
                if isinstance(truth, AdmittedOutcome):
                    yield _frame("admitted", truth.admission)
                    return
                if isinstance(truth, PositionOutcome):
                    if position_from(sequence, truth.admitted_total) != truth.position:
                        POSITION_DRIFT.inc()  # the arithmetic had drifted; reconciliation fixed it
                    sequence = truth.position + truth.admitted_total
                    admitted_total = truth.admitted_total
                    total_waiting = truth.total_waiting
                    rate_per_min = truth.rate_per_min
                    fresh = True

            if not fresh:
                continue  # a heartbeat with nothing new to report

            if sequence <= admitted_total:
                # Reached the front and been admitted — hand over the pass, then the stream is done.
                truth = await _reconcile(event_id, queue_token)
                if isinstance(truth, AdmittedOutcome):
                    yield _frame("admitted", truth.admission)
                    return
                # Pass not visible yet (TTL race, or the known pop/sign gap in decisions.md) — keep
                # streaming; the next broadcast or reconciliation resolves it.
                continue

            # Clamp so a reconciliation correction is never SHOWN moving the position up (FR-7).
            last_shown = clamp_position(position_from(sequence, admitted_total), last_shown)
            state = QueueState(
                position=last_shown,
                total_waiting=total_waiting,
                admitted_total=admitted_total,
                eta_seconds=eta_seconds_for(last_shown, rate_per_min),
                state=WaiterState.WAITING,
            )
            yield _frame("position", state.as_dict())
    finally:
        # Runs on admission, on client disconnect (GeneratorExit) and on cancellation — so a closed
        # browser tab always releases its slot in the fan-out and the connection gauge.
        broadcaster.unregister(event_id, inbox)
        SSE_CONNECTIONS.dec()


def _frame(event: str, data: dict[str, object]) -> str:
    """One SSE frame: an event name and a JSON data line, terminated by a blank line."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def _reconcile(
    event_id: str, queue_token: str
) -> PositionOutcome | AdmittedOutcome | None:
    """The authoritative situation from Redis (ZRANK), or None on a transient failure.

    Used both to correct drift periodically and to fetch the pass once the arithmetic says a waiter
    has reached the front. A transient failure returns None so the stream keeps going on the last
    known state rather than dropping the connection.
    """
    try:
        return await get_queue_repository().position(event_id, queue_token)
    except (UnknownEvent, UnknownToken, RedisError):
        return None


def _queue_token_from(request: HttpRequest, event_id: str) -> str | None:
    """The caller's queue token: cookie first, then `?t=`. None if absent or malformed.

    `?t=` must be accepted because EventSource cannot set headers (build-plan §1), and the same
    token has to work for both the polling and the SSE path. The cookie wins when both are
    present: it is the one the server set, so it is the one that is not a stale copy-paste.
    """
    for candidate in (request.COOKIES.get(f"qf_{event_id}"), request.GET.get("t")):
        if candidate and is_valid_queue_token(candidate):
            return candidate
    return None


def _set_queue_token_cookie(response: JsonResponse, event_id: str, queue_token: str) -> None:
    """Persist the queue token so a refresh keeps the place (Journey B).

    Per-event name, so queueing for two drops in one browser does not have one place overwrite
    the other. HttpOnly because no page script needs to read it and the token is the place in
    the queue. SameSite=Lax so a cross-site POST cannot join on someone's behalf.
    """
    response.set_cookie(
        f"qf_{event_id}",
        queue_token,
        max_age=settings.QUEUE_COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        samesite="Lax",
        secure=settings.QUEUE_COOKIE_SECURE,
        path="/",
    )
