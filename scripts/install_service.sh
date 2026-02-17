#!/usr/bin/env bash
# Install systemd service so the app starts automatically on boot.
# Run once with: sudo ./scripts/install_service.sh

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
SERVICE_NAME="klikk-financials-v4.service"
SERVICE_FILE="$SCRIPT_DIR/$SERVICE_NAME"

if [ ! -f "$SERVICE_FILE" ]; then
  echo "Error: $SERVICE_FILE not found."
  exit 1
fi

echo "Installing $SERVICE_NAME..."
sudo systemctl stop "$SERVICE_NAME" 2>/dev/null || true
sudo cp "$SERVICE_FILE" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
echo "Enabled. Starting service..."
sudo systemctl start "$SERVICE_NAME"
sudo systemctl status "$SERVICE_NAME" --no-pager

echo ""
echo "Done. The app will start on boot and is running now on http://0.0.0.0:8002"
echo "Commands:"
echo "  sudo systemctl status $SERVICE_NAME   # status"
echo "  sudo systemctl restart $SERVICE_NAME # restart"
echo "  sudo systemctl stop $SERVICE_NAME    # stop"
echo "  sudo journalctl -u $SERVICE_NAME -f  # logs"
