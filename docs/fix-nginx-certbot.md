# Fix www.klikk.co.za: duplicate server_name and Certbot

## What went wrong

1. **Conflicting server name** – More than one Nginx config defines `server_name www.klikk.co.za` or `paw.klikk.co.za`. Nginx keeps only the first and ignores the rest, so the wrong server block may be used.
2. **Certbot failed for klikk.co.za** – Let’s Encrypt could not reach `http://klikk.co.za/.well-known/acme-challenge/...`. That usually means `klikk.co.za` is not pointing to this server, or the duplicate configs prevented the challenge from being served.

## Fix 1: Use only www.klikk.co.za for now

Get HTTPS working for **www.klikk.co.za** only (skip `klikk.co.za` until DNS is correct):

```bash
sudo certbot certonly --nginx -d www.klikk.co.za
```

If it says you already have a certificate for www.klikk.co.za, you’re fine – go to Fix 2.

To add **klikk.co.za** later (after its DNS points to 102.135.240.222):

```bash
sudo certbot certonly --nginx -d www.klikk.co.za -d klikk.co.za
```

## Fix 2: Remove duplicate Nginx server blocks

Find which configs define the same names:

```bash
sudo grep -l "www.klikk.co.za\|paw.klikk.co.za" /etc/nginx/sites-enabled/*
```

You’ll see something like:

- `/etc/nginx/sites-enabled/klikk.co.za`
- `/etc/nginx/sites-enabled/default`
- `/etc/nginx/sites-enabled/paw.klikk.co.za`
- …

**Option A – One site per file (recommended)**  
Keep a single file that handles **www.klikk.co.za** (and optionally **klikk.co.za**), and make sure no other file in `sites-enabled` uses the same `server_name`:

- Disable the duplicate by removing its symlink:
  ```bash
  sudo rm /etc/nginx/sites-enabled/<duplicate-config>
  ```
- Or open the duplicate file and remove or comment out the `server { ... }` block that contains `server_name www.klikk.co.za ...;`.

**Option B – Merge into one server block**  
If you want both www.klikk.co.za and paw.klikk.co.za on the same server, they must be in **one** `server` block (or in separate blocks in the same file without duplication elsewhere). For example, one block can have:

```nginx
server_name www.klikk.co.za klikk.co.za;
```

and another:

```nginx
server_name paw.klikk.co.za;
```

Then ensure no other file in `sites-enabled` declares the same names.

After editing:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

## Fix 3: Ensure the right server block has SSL for www.klikk.co.za

The server block that handles `www.klikk.co.za` on port 443 should have:

```nginx
ssl_certificate     /etc/letsencrypt/live/www.klikk.co.za/fullchain.pem;
ssl_certificate_key /etc/letsencrypt/live/www.klikk.co.za/privkey.pem;
```

Certbot often adds these when you run `certbot --nginx -d www.klikk.co.za`. If you edited config by hand, add these lines (and `include /etc/letsencrypt/options-ssl-nginx.conf;` if present).

## Quick sequence

```bash
# 1. Cert for www only
sudo certbot certonly --nginx -d www.klikk.co.za

# 2. See which configs conflict
sudo grep -l "www.klikk.co.za\|paw.klikk.co.za" /etc/nginx/sites-enabled/*

# 3. Remove or edit the duplicate (example: disable default for these names)
# sudo rm /etc/nginx/sites-enabled/<conflicting-file>
# or edit the file to drop the duplicate server_name

# 4. Test and reload
sudo nginx -t && sudo systemctl reload nginx
```

Then try https://www.klikk.co.za again.
