#!/bin/sh
set -e

echo "Running migrations..."
python manage.py migrate --noinput

echo "Collecting static files..."
python manage.py collectstatic --noinput

echo "Starting Gunicorn..."
exec gunicorn \
    --workers 3 \
    --bind 0.0.0.0:8001 \
    --timeout 3600 \
    --graceful-timeout 3600 \
    --access-logfile - \
    --error-logfile - \
    klikk_business_intelligence.wsgi:application
