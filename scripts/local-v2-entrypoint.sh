#!/bin/sh
set -eu

if [ "${DJANGO_SETTINGS_MODULE:-}" != "klikk_business_intelligence.settings.local_v2" ]; then
    echo "Refusing to start without the dedicated local V2 settings module." >&2
    exit 1
fi

python manage.py migrate --noinput
python manage.py seed_local_v2

exec uvicorn \
    klikk_business_intelligence.asgi:application \
    --host 0.0.0.0 \
    --port 8001 \
    --workers 1 \
    --timeout-keep-alive 30 \
    --no-access-log

