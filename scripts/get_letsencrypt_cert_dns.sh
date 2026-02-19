#!/bin/bash
# Get a Let's Encrypt certificate using DNS-01 challenge (no port 80 needed).
# Use this when HTTP-01 fails with "Timeout during connect" (e.g. firewall blocks Let's Encrypt).
#
# NXDOMAIN / "DNS record exists for this domain" means the TXT record was not found. Do this:
#
# 1) When certbot shows the name and value, ADD THE RECORD at your DNS provider first.
#
# 2) Record to add (exactly as certbot shows):
#    Type:  TXT
#    Name:  _acme-challenge.www   (for www.klikk.co.za)
#           Some providers want full name: _acme-challenge.www.klikk.co.za
#           Others want only the subdomain part: _acme-challenge.www
#    Value: (the long string certbot prints, no quotes)
#
# 3) Save, then WAIT 2–5 minutes. Verify:
#    dig TXT _acme-challenge.www.klikk.co.za +short
#    Or: https://toolbox.googleapps.com/apps/dig/#TXT/_acme-challenge.www.klikk.co.za
#
# 4) Only when the TXT value appears in dig, press Enter in certbot.
#
# Usage:
#   sudo ./scripts/get_letsencrypt_cert_dns.sh www.klikk.co.za
#   sudo ./scripts/get_letsencrypt_cert_dns.sh www.klikk.co.za mc@klikk.co.za
#
set -e
DOMAIN="${1:?Usage: $0 <domain> [email]}"
EMAIL="${2:-}"

CERT_CONF="/etc/letsencrypt/live/$DOMAIN/fullchain.pem"
KEY_CONF="/etc/letsencrypt/live/$DOMAIN/privkey.pem"
NGINX_CONF="/etc/nginx/sites-available/klikk-financials-v4"

# 1) Get certificate via DNS-01 (interactive: you add TXT record then press Enter)
if [[ -n "$EMAIL" ]]; then
  certbot certonly --manual --preferred-challenges dns -d "$DOMAIN" --agree-tos --email "$EMAIL"
else
  certbot certonly --manual --preferred-challenges dns -d "$DOMAIN" --agree-tos
fi

# 2) Write HTTPS nginx config
if [[ -f "$CERT_CONF" ]]; then
  cat > "$NGINX_CONF" <<EOF
# Klikk Financials v4 – HTTPS (Let's Encrypt via DNS-01)
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
EOF
  ln -sf "$NGINX_CONF" /etc/nginx/sites-enabled/klikk-financials-v4 2>/dev/null || true
  nginx -t && systemctl reload nginx
  echo "Done. HTTPS is enabled for https://$DOMAIN"
else
  echo "Certificate not found at $CERT_CONF"
  exit 1
fi
