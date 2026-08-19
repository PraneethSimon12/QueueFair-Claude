"""Gunicorn config — its only job is prometheus-client multiprocess hygiene.

Identical in shape to queue_service/gunicorn.conf.py (Phase 13). The booking image runs 4 workers,
so /metrics must aggregate across them; that needs two hooks: clear stale metric files at startup
(files from a previous run would be aggregated forever otherwise), and drop a dead worker's series
so /metrics stops reporting a worker that exited.
"""

import os

_PROM_DIR = os.environ.get("PROMETHEUS_MULTIPROC_DIR")


def on_starting(server):  # noqa: ANN001, ARG001 - gunicorn hook signature
    if _PROM_DIR and os.path.isdir(_PROM_DIR):
        for name in os.listdir(_PROM_DIR):
            os.remove(os.path.join(_PROM_DIR, name))


def child_exit(server, worker):  # noqa: ANN001, ARG001 - gunicorn hook signature
    if _PROM_DIR:
        from prometheus_client import multiprocess

        multiprocess.mark_process_dead(worker.pid)
