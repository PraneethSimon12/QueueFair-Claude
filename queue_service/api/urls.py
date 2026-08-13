"""URLconf for the queue service.

No trailing slashes anywhere, and APPEND_SLASH is unreachable because CommonMiddleware is not
installed (and cannot be — see MIDDLEWARE in settings.py). Clients must hit the exact path.
Queue endpoints will mount under /api/queue/ from Phase 6; /healthz stays at the root because
process supervisors and load balancers expect it there.
"""

from django.urls import path

from . import views

urlpatterns = [
    # The v0 waiting-room page, served same-origin so the browser can call join/position without
    # CORS (which this service's MIDDLEWARE=[] cannot provide). Removed at Phase 12 (Caddy).
    path("", views.index, name="index"),
    path("healthz", views.healthz, name="healthz"),
    path("metrics", views.metrics, name="metrics"),
    # <slug:> is tighter than <str:> and rejects a path segment that could not be an event id
    # before the view runs. core.validation.is_valid_event_id is stricter still (lowercase, no
    # underscores); both paths end in the same 404, so the extra check costs a client nothing.
    path("api/queue/<slug:event_id>/join", views.join, name="join"),
    path("api/queue/<slug:event_id>/position", views.position, name="position"),
    path("api/queue/<slug:event_id>/stream", views.stream, name="stream"),
]
