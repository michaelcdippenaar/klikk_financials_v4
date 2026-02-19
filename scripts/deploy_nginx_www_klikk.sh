#!/bin/bash
# Deploy nginx config so https://www.klikk.co.za/ → portal (8080), /backend/ → 8001, etc.
# Run: sudo ./scripts/deploy_nginx_www_klikk.sh
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cp "$REPO_ROOT/scripts/nginx-klikk-financials-v4.conf" /etc/nginx/sites-available/klikk-financials-v4
nginx -t && systemctl reload nginx
echo "Nginx reloaded. Test: https://www.klikk.co.za/ (portal), https://www.klikk.co.za/backend/ (API), https://www.klikk.co.za/django-admin/ (admin)."
