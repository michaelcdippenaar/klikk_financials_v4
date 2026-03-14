#!/bin/sh
set -e

echo "Running migrations..."
python manage.py migrate --noinput

echo "Collecting static files..."
python manage.py collectstatic --noinput

echo "Starting Uvicorn (ASGI — HTTP + WebSocket)..."
exec uvicorn \
    klikk_business_intelligence.asgi:application \
    --host 0.0.0.0 \
    --port 8001 \
    --workers 1 \
    --timeout-keep-alive 3600 \
    --access-log
