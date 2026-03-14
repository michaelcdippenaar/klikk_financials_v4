# Routing www.klikk.co.za to Portal + Django

Serve everything under **www.klikk.co.za** using a single reverse proxy:

- **www.klikk.co.za/** → Portal (Quasar SPA, container port 8080)
- **www.klikk.co.za/backend/** → Django API (container port 8001; `/backend` is stripped so Django sees `/api/...`, `/xero/...`, etc.)

The portal is built with `VITE_API_BASE_URL=/backend`, so it already calls the same origin at `/backend/...`. No frontend rebuild needed.

## 1. DNS

Point **www.klikk.co.za** and **klikk.co.za** (optional) to your server’s public IP (e.g. `102.135.240.222`).

## 2. Reverse proxy (choose one)

### Option A – Nginx

1. Copy the config:
   ```bash
   sudo cp docs/nginx-www-klikk.co.za.conf /etc/nginx/sites-available/klikk.co.za
   sudo ln -s /etc/nginx/sites-available/klikk.co.za /etc/nginx/sites-enabled/
   ```
2. Test and reload:
   ```bash
   sudo nginx -t && sudo systemctl reload nginx
   ```
3. Add HTTPS with Let’s Encrypt:
   ```bash
   sudo certbot --nginx -d www.klikk.co.za -d klikk.co.za
   ```
4. In the Nginx config, uncomment the HTTPS server block and the `return 301` in the HTTP block, then `sudo nginx -t && sudo systemctl reload nginx`.

### Option B – Caddy (auto HTTPS)

1. Install Caddy, then use `docs/Caddyfile-www-klikk.co.za` as your site config (e.g. `/etc/caddy/Caddyfile`).
2. Run Caddy (e.g. `caddy run --config /etc/caddy/Caddyfile` or via systemd). It will obtain and renew TLS for www.klikk.co.za automatically.

## 3. Django

Staging already allows **www.klikk.co.za** and **klikk.co.za** in:

- `ALLOWED_HOSTS`
- `CORS_ALLOWED_ORIGINS`
- `CSRF_TRUSTED_ORIGINS`

No code change needed. Ensure the app runs with `DJANGO_SETTINGS_MODULE=klikk_business_intelligence.settings.staging` (e.g. in Docker `.env` or `environment`).

## 4. Summary

| URL | Proxied to | Purpose |
|-----|------------|--------|
| https://www.klikk.co.za/ | localhost:8080 | Portal SPA |
| https://www.klikk.co.za/backend/* | localhost:8001 (path `/backend` stripped) | Django API |

After the proxy and DNS are in place, open https://www.klikk.co.za and the portal will call the API at https://www.klikk.co.za/backend/ on the same origin.

---

## Not working? (https://www.klikk.co.za not loading)

Run this **on the server** that hosts the site:

```bash
cd /home/mc/apps/klikk_financials_v4
chmod +x docs/troubleshoot-www-klikk.sh
./docs/troubleshoot-www-klikk.sh
```

Then check:

| Symptom | Likely cause | Fix |
|--------|----------------|-----|
| **https://** times out or “connection refused” | Nothing listening on 443 or no SSL | 1) Ensure nginx is running: `sudo systemctl status nginx`<br>2) Run certbot: `sudo certbot --nginx -d www.klikk.co.za -d klikk.co.za` |
| **http://** works, **https://** doesn’t | SSL not configured | Run certbot (see above). Certbot will add HTTPS and redirect. |
| Both fail | Reverse proxy not in place or containers down | 1) Enable the site: `sudo ln -sf /etc/nginx/sites-available/klikk.co.za /etc/nginx/sites-enabled/` then `sudo nginx -t && sudo systemctl reload nginx`<br>2) Start containers: `cd /home/mc/apps/klikk_financials_v4 && docker compose up -d` and same for `klikk-portal` |
| Port 80/443 “not listening” | Nginx not running or firewall | `sudo systemctl start nginx`; open 80/443 in firewall (e.g. `ufw allow 80,443/tcp && sudo ufw reload`) |
| 502 Bad Gateway | Backend (8080 or 8001) not responding | Start Docker: `docker compose up -d` in both `klikk_financials_v4` and `klikk-portal` |

Use the **HTTPS-ready** Nginx config if you want a single file to install and then run certbot:

```bash
sudo cp docs/nginx-www-klikk.co.za-https.conf /etc/nginx/sites-available/klikk.co.za
sudo ln -sf /etc/nginx/sites-available/klikk.co.za /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d www.klikk.co.za -d klikk.co.za
```

