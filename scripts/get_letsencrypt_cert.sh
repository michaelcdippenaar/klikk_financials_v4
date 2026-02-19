#!/bin/bash
# Obtain a Let's Encrypt certificate for Klikk Financials v4 and enable HTTPS.
#
# Prerequisites:
#   - A domain name (e.g. app.klikk.co.za) with a DNS A record pointing to this server.
#   - Port 80 (and 443) open from the INTERNET to this server (cloud firewall / security group).
#
# If you see "Timeout during connect (likely firewall problem)":
#   - Open inbound TCP 80 and 443 in your cloud provider (AWS Security Group, Azure NSG, etc.).
#   - Or use DNS-01 instead (no port 80 needed): sudo ./scripts/get_letsencrypt_cert_dns.sh DOMAIN EMAIL
#
# Usage:
#   sudo ./scripts/get_letsencrypt_cert.sh your-domain.com
#   sudo ./scripts/get_letsencrypt_cert.sh app.klikk.co.za your@email.com
#
set -e
DOMAIN="${1:?Usage: $0 <domain> [email]}"
EMAIL="${2:-}"

# Optional: install certbot (uncomment if needed)
# apt-get update && apt-get install -y certbot python3-certbot-nginx

# 1) Deploy HTTP-only config so Let's Encrypt can do the HTTP-01 challenge
#    (no HTTPS redirect yet; certbot will add the 443 block)
NGINX_CONF="/etc/nginx/sites-available/klikk-financials-v4"
mkdir -p /var/www/letsencrypt
chown www-data:www-data /var/www/letsencrypt 2>/dev/null || true

cat > "$NGINX_CONF" <<EOF
# Temporary HTTP-only config for certbot; certbot will add the 443 block
server {
    listen 80;
    server_name $DOMAIN;

    location /.well-known/acme-challenge/ {
        root /var/www/letsencrypt;
        allow all;
    }

    location / {
        proxy_pass http://127.0.0.1:8001;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_connect_timeout 1800s;
        proxy_send_timeout 1800s;
        proxy_read_timeout 1800s;
        send_timeout 1800s;
    }
}
EOF

ln -sf "$NGINX_CONF" /etc/nginx/sites-enabled/klikk-financials-v4 2>/dev/null || true
nginx -t && systemctl reload nginx

# 2) Get certificate (certbot will add listen 443 and ssl_* to the same file)
if [[ -n "$EMAIL" ]]; then
  certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --email "$EMAIL"
else
  certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos
fi

# 3) Write final HTTPS config (redirect 80→443, use Let's Encrypt cert paths)
CERT_CONF="/etc/letsencrypt/live/$DOMAIN/fullchain.pem"
KEY_CONF="/etc/letsencrypt/live/$DOMAIN/privkey.pem"
if [[ -f "$CERT_CONF" ]]; then
  cat > "$NGINX_CONF" <<FINAL
# Klikk Financials v4 – HTTPS (certificate from Let's Encrypt)
server {
    listen 80;
    server_name $DOMAIN;
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl;
    server_name $DOMAIN;

    ssl_certificate     $CERT_CONF;
    ssl_certificate_key $KEY_CONF;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers on;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384;

    location / {
        proxy_pass http://127.0.0.1:8001;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_connect_timeout 1800s;
        proxy_send_timeout 1800s;
        proxy_read_timeout 1800s;
        send_timeout 1800s;
    }
}
FINAL
  nginx -t && systemctl reload nginx
  echo "Done. HTTPS is enabled for https://$DOMAIN"
else
  echo "Certificate not found at $CERT_CONF; check certbot output above."
  exit 1
fi
