"""Django settings for the queue service.

This service holds the crowd: thousands of long-lived SSE connections, and Redis as its only
state. Two settings below are load-bearing **concurrency constraints**, not preferences —
`MIDDLEWARE` and `DATABASES`. Each has a comment saying what breaks if it changes, and
`tests/test_invariants.py` fails if either is quietly edited. See CLAUDE.md §8.
"""

import os
from pathlib import Path

# queue_service/queue_service/settings.py -> BASE_DIR is the outer queue_service/, which is where
# api/, core/, adapters/ and lua/ live and what manage.py puts on sys.path.
BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Seed os.environ from a KEY=VALUE .env file, if present.

    Deliberately hand-rolled (Rule 5), same ~12 lines as the booking service. Duplicated rather
    than shared: the two services have no code dependency on each other and introducing one for
    twelve lines would be a worse trade than the duplication. Real environment variables win —
    setdefault() never overwrites something already exported in the shell. Values are read
    verbatim, so do not wrap them in quotes.
    """
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(BASE_DIR / ".env")


# --- Django core --------------------------------------------------------------------------------

# We run no sessions, no CSRF, no admin and no signed cookies, so nothing on the hot path reads
# this. It is still set because Django's signing machinery is imported lazily by anything added
# later, and an unset SECRET_KEY fails at that moment rather than at startup.
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-insecure-queue-service-key")

# Default OFF, unlike the booking service. Under load DEBUG=True is not a debugging aid, it is a
# memory leak and an information disclosure — and this is the service that will be holding 20K
# connections when someone forgets.
DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"

# Host validation happens in HttpRequest.get_host(), which costs a comparison per request. Keep
# the list short (CLAUDE.md §8) and measure before assuming a trim helped. "testserver" is what
# Django's test client sends.
ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get(
        "DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver"
    ).split(",")
    if host.strip()
]

# `api` is here for exactly one reason: Django only discovers management commands inside
# installed apps, and `create_event` has to live somewhere. It has no models and no migrations,
# so this costs one AppConfig at startup and nothing per request. core/ and adapters/ stay plain
# Python packages — nothing about them needs Django to know they exist.
INSTALLED_APPS: list[str] = ["api"]

# ┌─ DO NOT ADD TO THIS LIST WITHOUT PROVING THE ENTRY IS async_capable ─────────────────────────┐
# │ Django adapts between sync and async at the middleware boundary. ONE middleware that is not  │
# │ async_capable makes Django wrap every async view in sync_to_async, which runs it on a thread │
# │ from the ASGI thread pool. With SSE holding thousands of open connections that is thousands  │
# │ of parked threads, and the pool is exhausted long before we get there. This single list is   │
# │ the most likely way to silently destroy this service's entire reason to exist.               │
# │ GZipMiddleware and ConditionalGetMiddleware are additionally fatal on SSE: both must consume  │
# │ the whole response to compress or hash it, so the client receives nothing until the stream    │
# │ ends — which, for SSE, is never.  (CLAUDE.md §8, decisions.md 2026-08-02.)                    │
# └───────────────────────────────────────────────────────────────────────────────────────────────┘
MIDDLEWARE: list[str] = []

ROOT_URLCONF = "queue_service.urls"

# No templates: every response this service produces is JSON or an SSE frame. An empty list means
# the template engine is never even configured, let alone loaded.
TEMPLATES: list[dict[str, object]] = []

ASGI_APPLICATION = "queue_service.asgi.application"

# There is deliberately no WSGI_APPLICATION and no wsgi.py. SSE is a long-lived streaming
# response; under WSGI each waiter would occupy a worker for the duration of their wait, which
# is the exact failure this service exists to avoid.

# ┌─ EMPTY ON PURPOSE — THIS IS A DESIGN CONSTRAINT, NOT AN OMISSION ────────────────────────────┐
# │ The queue service's entire state lives in Redis and it must never touch a database. The ORM  │
# │ is sync-only: calling it from an async view raises SynchronousOnlyOperation, and "fixing"    │
# │ that with sync_to_async just moves the request onto a thread — the MIDDLEWARE trap again,    │
# │ wearing a different hat.                                                                     │
# │ Note what Django actually does with an empty dict, because it is not what it looks like:     │
# │ ConnectionHandler.configure_settings INJECTS a "default" alias backed by                     │
# │ django.db.backends.dummy. So connections["default"] resolves fine and nothing fails at       │
# │ import — the first query is what raises ImproperlyConfigured. That is still the guarantee we │
# │ want (loud, immediate, at the offending line) but the mechanism is the dummy backend, not    │
# │ the absence of a connection. tests/test_invariants.py pins the real behaviour.               │
# └───────────────────────────────────────────────────────────────────────────────────────────────┘
DATABASES: dict[str, dict[str, object]] = {}

USE_TZ = True


# --- Redis --------------------------------------------------------------------------------------

REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")

# Bounded, and low. An unbounded wait on a dead Redis does not surface as an error, it surfaces
# as a request that never returns while still holding its connection — strictly worse than a
# fast 503, because a health check that hangs cannot be distinguished from one that is passing.
REDIS_SOCKET_TIMEOUT_SECONDS = float(os.environ.get("REDIS_SOCKET_TIMEOUT_SECONDS", "2.0"))
REDIS_CONNECT_TIMEOUT_SECONDS = float(os.environ.get("REDIS_CONNECT_TIMEOUT_SECONDS", "2.0"))


# --- The queue-token cookie -----------------------------------------------------------------

# Long enough to outlast any realistic drop, short enough that a token for a finished event does
# not sit in a browser for weeks. The token IS the place in the queue and there are no accounts
# to recover it from (product-spec §9), so losing the cookie loses the place.
QUEUE_COOKIE_MAX_AGE_SECONDS = int(os.environ.get("QUEUE_COOKIE_MAX_AGE_SECONDS", str(6 * 3600)))

# False for local http, or the browser silently discards the cookie and every refresh looks like
# a new waiter. Must be 1 in any deployment with TLS — Phase 12, behind Caddy.
QUEUE_COOKIE_SECURE = os.environ.get("QUEUE_COOKIE_SECURE", "0") == "1"


# --- Admission ---------------------------------------------------------------------------------

# The HS256 secret this service SIGNS admission passes with. It must be byte-identical to the
# booking service's ADMISSION_TOKEN_SECRET, which verifies them — that shared secret is the whole
# trust boundary, and it is why neither service ever calls the other.
#
# Required, with NO fallback default, exactly as in booking_service/settings.py. A guessable
# signing secret means anyone can forge a pass and skip the queue entirely, which is not a bug in
# a feature but the total defeat of the product. A missing value must crash the process at import.
ADMISSION_TOKEN_SECRET = os.environ["ADMISSION_TOKEN_SECRET"]

# 60 seconds, per design.md §4. Long enough to click "book", short enough that unused passes
# cannot accumulate — at 100/min, a 20-minute drop with no expiry would eventually release 2,000
# people onto the booking service at once, which is the stampede this system exists to prevent.
ADMISSION_PASS_TTL_SECONDS = int(os.environ.get("ADMISSION_PASS_TTL_SECONDS", "60"))

# How often each admitter process ticks. This is NOT the rate limiter — the token bucket in
# admit_batch.lua is, and it is global across processes. The interval only sets how long an
# admitted waiter waits to be told; ticking faster than the rate just produces empty batches at
# one Redis call each.
ADMISSION_TICK_SECONDS = float(os.environ.get("ADMISSION_TICK_SECONDS", "1.0"))


# --- SSE ---------------------------------------------------------------------------------------

# A ': ping' comment every this many seconds keeps proxies from dropping an idle connection
# (CLAUDE.md §8). 15s is the conventional SSE heartbeat.
SSE_HEARTBEAT_SECONDS = float(os.environ.get("SSE_HEARTBEAT_SECONDS", "15"))

# How often each open stream re-checks the AUTHORITATIVE ZRANK to correct the position
# arithmetic's abandonment drift (design.md §6; build-plan §5 budgets ~1 per connection per 30s).
# This is the only per-connection Redis cost of the SSE path — everything else is in-memory
# arithmetic — so it trades a small, bounded, predictable load for a position that cannot drift
# without limit.
SSE_RECONCILE_SECONDS = float(os.environ.get("SSE_RECONCILE_SECONDS", "30"))
