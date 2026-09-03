#!/bin/sh
set -e

echo "Running migrations..."
python manage.py migrate --noinput

# The register's own DDL lives in _ensure_table()/ensure_tables(), guarded by a
# MODULE GLOBAL, so without this it runs on each worker's first AUTHENTICATED
# request. Two consequences, both seen: a container reports healthy with the
# schema unapplied (pages 200, gate 401, column still the old name -- hit twice
# on 2026-09-03), and concurrent CREATE TABLE IF NOT EXISTS across gunicorn
# workers can race on pg_type. Running it here makes the runbook step a check
# rather than a repair, and the first person to touch the new schema is not a
# bookkeeper. Non-fatal on purpose: a boot that cannot reach Postgres yet must
# still start and let the request path retry.
echo "Ensuring register schema..."
python manage.py shell -c "
from apps.xero.xero_data import pivot_comments, cube_mentions
pivot_comments._ensure_table(); cube_mentions.ensure_tables()
print('register schema ok')
" || echo "WARNING: register DDL did not run at boot; it will run on first request"

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
