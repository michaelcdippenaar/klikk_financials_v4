# Fix Nginx conflicts for www.klikk.co.za and paw.klikk.co.za

## Current conflict

- **www.klikk.co.za** is defined in both:
  - `/etc/nginx/sites-available/klikk.co.za`
  - `/etc/nginx/sites-available/klikk-financials-v4`
- **paw.klikk.co.za** appears twice (likely two server blocks in `paw`).

## Step 1: One config for www.klikk.co.za

Keep **klikk.co.za** (portal + /backend). Disable **klikk-financials-v4** so it stops defining www.klikk.co.za:

```bash
sudo rm /etc/nginx/sites-enabled/klikk-financials-v4
```

If you need something that was only in klikk-financials-v4 (e.g. a different app), merge that into `klikk.co.za` first, then disable klikk-financials-v4.

## Step 2: Fix paw.klikk.co.za conflict

See how many times `paw.klikk.co.za` appears and on which ports:

```bash
sudo grep -n "server_name\|listen" /etc/nginx/sites-available/paw
```

You want **one** `server { ... }` with `listen 80` and `server_name paw.klikk.co.za` and **one** with `listen 443` and `server_name paw.klikk.co.za`. If there are two blocks with the same `server_name` on the same port, remove or merge the duplicate block in `/etc/nginx/sites-available/paw`, then:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

## Step 3: Reload

```bash
sudo nginx -t && sudo systemctl reload nginx
```

Warnings should be gone. Test https://www.klikk.co.za and https://paw.klikk.co.za.
