# Fix https://www.klikk.co.za – timeout and 502

## What the tests showed

- **curl https://www.klikk.co.za** (to 102.135.240.222:443) → **timeout** → inbound 443 is blocked (firewall or cloud).
- **curl http://127.0.0.1/** → 200 but default page (wrong vhost).
- **curl https://127.0.0.1/ -k** → **502 Bad Gateway** (default 443 vhost proxies to something that fails).
- **curl http://127.0.0.1:8080/** → 200 (portal container is fine).

So: Nginx and the portal are OK; the problem is **firewall** (external) and **which vhost** handles 443.

---

## Step 1: Open ports 80 and 443 (firewall)

On the server:

```bash
# See if UFW is active and what’s allowed
sudo ufw status

# If UFW is active, allow HTTP/HTTPS and reload
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw reload
```

If the server is in a **cloud** (AWS, Azure, GCP, etc.), also open **inbound** TCP **80** and **443** in the **security group / firewall** for the instance’s network. The curl timeout to 102.135.240.222:443 will not fix itself until 443 is open there.

---

## Step 2: Check that www.klikk.co.za gets the right vhost on 443

Force the Host header so Nginx uses the klikk.co.za server block:

```bash
curl -sI -k -H "Host: www.klikk.co.za" https://127.0.0.1/
```

- If you get **200 OK** → Nginx config for www.klikk.co.za is fine; once Step 1 is done, https://www.klikk.co.za should work from the internet.
- If you still get **502** → The HTTPS server block in `/etc/nginx/sites-available/klikk.co.za` has a wrong or broken `proxy_pass` (e.g. wrong port or upstream down). Check that it has:
  - `proxy_pass http://127.0.0.1:8080` for `location /`
  - and `proxy_pass http://127.0.0.1:8001/` for `location /backend/`

---

## Step 3: Reload Nginx after any config change

```bash
sudo nginx -t && sudo systemctl reload nginx
```

---

## Summary

| Issue | Fix |
|-------|-----|
| Timeout to https://www.klikk.co.za | Open TCP 80 and 443 in UFW and in cloud security group / firewall |
| 502 on default HTTPS | Ensure you’re hitting the right vhost; fix `proxy_pass` in klikk.co.za for 443 if needed |

After opening 443 (and 80) externally, test again: `curl -sI https://www.klikk.co.za`.
