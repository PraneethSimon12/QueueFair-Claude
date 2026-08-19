"""Run the dynamic-backpressure control loop for one event.

Run **exactly one** instance per event. Unlike run_admitter — which is deliberately leaderless and
safe to run many times — two backpressure loops would race on `rate_per_min` with conflicting AIMD
decisions and oscillate. The controller docstring in core/backpressure.py explains why.

    python manage.py run_backpressure coldplay-mumbai-2026

It needs Prometheus scraping the booking service (monitoring/prometheus.yml) to have a p99 to read;
with no signal it holds the current rate rather than guessing.
"""

import asyncio
import logging
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from adapters.backpressure_factory import build_backpressure_controller
from adapters.redis_client import close_redis
from core.backpressure import UnknownEventError
from core.validation import is_valid_event_id

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the dynamic-backpressure loop for an event. Run ONE instance per event."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("event_id", help="slug, e.g. coldplay-mumbai-2026")
        parser.add_argument(
            "--interval",
            type=float,
            default=None,
            help=f"seconds between control steps (default {settings.BACKPRESSURE_INTERVAL_SECONDS})",
        )
        parser.add_argument(
            "--once",
            action="store_true",
            help="run a single control step and exit — for scripted demos and tests",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        event_id: str = options["event_id"]
        if not is_valid_event_id(event_id):
            raise CommandError(f"{event_id!r} is not a valid event id")

        interval = options["interval"] or settings.BACKPRESSURE_INTERVAL_SECONDS
        asyncio.run(self._run(event_id, interval, once=options["once"]))

    async def _run(self, event_id: str, interval: float, *, once: bool) -> None:
        controller = build_backpressure_controller()
        try:
            if once:
                try:
                    rate = await controller.adjust_once(event_id)
                except UnknownEventError as exc:
                    raise CommandError(f"no event configured for {event_id!r}") from exc
                self.stdout.write(f"rate now {rate}/min")
                return
            try:
                await controller.run_forever(event_id, interval)
            except (KeyboardInterrupt, asyncio.CancelledError):
                self.stdout.write(self.style.WARNING("\nbackpressure stopped"))
        finally:
            await close_redis()
