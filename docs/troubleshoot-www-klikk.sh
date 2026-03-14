#!/bin/bash
# Run this on the server that hosts www.klikk.co.za to see why the site might not be working.

echo "=== 1. Docker containers (portal=8080, django=8001) ==="
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null || echo "Docker not running or no containers"

echo ""
echo "=== 2. Ports 80, 443, 8080, 8001 ==="
for p in 80 443 8080 8001; do
  if command -v ss &>/dev/null; then
    r=$(ss -tlnp 2>/dev/null | awk -v p="$p" '$4 ~ ":"p"$" {print $0}')
  else
    r=$(netstat -tlnp 2>/dev/null | awk -v p="$p" '$4 ~ ":"p"$" {print $0}')
  fi
  [ -n "$r" ] && echo "Port $p: LISTEN" || echo "Port $p: not listening"
done

echo ""
echo "=== 3. Nginx ==="
if systemctl is-active nginx &>/dev/null; then
  echo "nginx: running"
  sudo nginx -t 2>&1
else
  echo "nginx: not running (systemctl status nginx)"
fi

echo ""
echo "=== 4. Nginx site config for klikk.co.za ==="
ls -la /etc/nginx/sites-enabled/*klikk* 2>/dev/null || echo "No klikk site in sites-enabled"
ls -la /etc/nginx/sites-available/*klikk* 2>/dev/null || true

echo ""
echo "=== 5. SSL certs (Let's Encrypt) ==="
ls -la /etc/letsencrypt/live/www.klikk.co.za/ 2>/dev/null || echo "No certs for www.klikk.co.za"

echo ""
echo "=== 6. Local curl to portal and backend ==="
curl -s -o /dev/null -w "localhost:8080 → %{http_code}\n" --connect-timeout 2 http://127.0.0.1:8080/ || echo "localhost:8080 failed"
curl -s -o /dev/null -w "localhost:8001 → %{http_code}\n" --connect-timeout 2 http://127.0.0.1:8001/admin/ || echo "localhost:8001 failed"
