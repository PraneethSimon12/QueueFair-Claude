# Booking service — sync Django + DRF + PostgreSQL, served by Gunicorn (WSGI) on Linux.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PROMETHEUS_MULTIPROC_DIR=/tmp/prom

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Where every gunicorn worker writes its metric files; /metrics aggregates them (metrics.py).
RUN mkdir -p /tmp/prom

EXPOSE 8000

# Apply migrations, then serve. `exec` so gunicorn becomes PID 1 and receives signals cleanly.
# `-c gunicorn.conf.py` wires the multiprocess metric hooks (clear stale files, drop dead workers).
CMD ["sh", "-c", "python manage.py migrate --noinput && exec gunicorn booking_service.wsgi:application -c gunicorn.conf.py -w 4 -b 0.0.0.0:8000 --access-logfile -"]
