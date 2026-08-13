"""Gunicorn config — its only job is prometheus-client multiprocess hygiene (CLAUDE.md §8).

Bind and worker count stay on the command line in the Dockerfile; this file exists for the two
hooks multiprocess mode needs: clear stale metric files at startup (files from a previous run would
otherwise be aggregated forever), and drop a dead worker's live gauges so /metrics stops counting
connections that closed with the process.
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
