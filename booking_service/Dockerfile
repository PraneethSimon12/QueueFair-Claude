# Booking service — sync Django + DRF + PostgreSQL, served by Gunicorn (WSGI) on Linux.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Apply migrations, then serve. `exec` so gunicorn becomes PID 1 and receives signals cleanly.
CMD ["sh", "-c", "python manage.py migrate --noinput && exec gunicorn booking_service.wsgi:application -w 4 -b 0.0.0.0:8000 --access-logfile -"]
