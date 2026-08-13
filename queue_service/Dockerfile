# Queue service — async Django on ASGI. This is the first time it runs on Linux, and the first
# time Gunicorn + UvicornWorker (and uvloop, via uvicorn[standard]) actually apply — see the
# platform markers in requirements.txt.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PROMETHEUS_MULTIPROC_DIR=/tmp/prom

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Where every worker (and the admitter) writes its metric files; /metrics aggregates them.
RUN mkdir -p /tmp/prom

EXPOSE 8001

# Gunicorn is the process manager; each worker is an ASGI Uvicorn worker. Long-lived SSE
# connections are why this is ASGI and not WSGI (settings.py). Workers kept modest — the box is
# small (§7) and the bottleneck is Redis, not CPU. -c wires the multiprocess metric hooks.
CMD ["gunicorn", "queue_service.asgi:application", "-c", "gunicorn.conf.py", "-k", "uvicorn.workers.UvicornWorker", "-w", "4", "-b", "0.0.0.0:8001", "--access-logfile", "-"]
