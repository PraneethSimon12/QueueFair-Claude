from django.urls import path

from .metrics import metrics_view
from .views import BookingView

urlpatterns = [
    # No trailing slash: this is a POST endpoint, and APPEND_SLASH cannot redirect a POST without
    # losing its body — so clients must hit this exact path.
    path("events/<slug:event_id>/book", BookingView.as_view(), name="book"),
    # The p99 the queue service's backpressure loop reads. Not public (Prometheus scrapes it on the
    # internal network, like the queue's /metrics), but it exposes only latency counts, no secrets.
    path("metrics", metrics_view, name="metrics"),
]
