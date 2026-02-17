#!/usr/bin/env bash
# Change the installed systemd service to use port 8002 and restart.
# Run: sudo ./scripts/fix_service_port.sh

set -e
SERVICE_FILE="/etc/systemd/system/klikk-financials-v4.service"
SERVICE_NAME="klikk-financials-v4"

if [ ! -f "$SERVICE_FILE" ]; then
  echo "Error: $SERVICE_FILE not found. Install the service first."
  exit 1
fi

echo "Changing port 8000 -> 8002 in $SERVICE_FILE..."
sed -i 's/0.0.0.0:8000/0.0.0.0:8002/g' "$SERVICE_FILE"
grep 'bind' "$SERVICE_FILE" || true

echo "Reloading systemd and restarting $SERVICE_NAME..."
systemctl daemon-reload
systemctl restart "$SERVICE_NAME"
systemctl status "$SERVICE_NAME" --no-pager

echo "Done. App should now be on http://0.0.0.0:8002"
