# Local dev on MC's Mac — runbook

Audience: Codex (or any agent/human) picking up `klikk_financials_v4` on **MC's MacBook
Pro**, not the VM. Everything here was verified on 2026-08-22. For the VM, see
`BRIEF_FOR_CODEX_2026-08-22.md` — the two are different machines with different
deploy models, and the VM's rules do **not** apply here.

---

## 1. Toolchain

| | Version | Notes |
|---|---|---|
| Python | 3.13.13 | in `.venv/` — **use `.venv/bin/python`**, not system `python3` |
| Django | 5.2.13 | |
| PostgreSQL | 15.17 (Homebrew) | `brew services` → `postgresql@15` started; `@16`/`@17` are NOT running |
| Docker | 29.3.0 (Docker Desktop) | |

> The container image is `python:3.10-slim`. The host venv is 3.13. Code must work on
> both — no 3.11+ only syntax.

---

## 2. Databases

Two local databases, both on `127.0.0.1:5432`:

| DB | What | Use it for |
|---|---|---|
| `klikk_financials_v4` | Original local DB. Data stops **2026-05-26**. Schema current. | Legacy / comparison |
| `klikk_financials_v4_vm` | **Restored from the VM's 2026-08-21 nightly**, then migrated current. 646k rows, all six schemas (`app, audit, kb, public, raw, staging, whatsapp`). | **Default — use this** |

### Credentials — read this before debugging an auth failure

- **The hardcoded fallbacks in `settings/development.py` and `settings/staging.py` are
  both wrong.** Neither authenticates. Don't trust them.
- `klikk_user`'s password was **reset on 2026-08-22 to the value in this repo's `.env`**
  (`DB_PASSWORD`). `.env` is now correct; it was not before.
- The Unix socket uses **peer auth**, so `psql -U klikk_user` over the socket fails.
  Connect over the socket as your own OS user (superuser), or over TCP with the password.

```bash
# socket, as superuser — simplest for psql / manage.py
psql -d klikk_financials_v4_vm

# TCP as klikk_user — what containers use
PGPASSWORD="$(grep '^DB_PASSWORD=' .env | cut -d= -f2-)" \
  psql -h 127.0.0.1 -U klikk_user -d klikk_financials_v4_vm
```

### Table ownership
Objects must be owned by `klikk_user`. Running `manage.py migrate` over the socket as
your OS user creates tables owned by **you**, which the app (connecting as `klikk_user`)
then can't touch. After any such migrate, reassign:

```sql
DO $$
DECLARE r RECORD;
BEGIN
  FOR r IN SELECT c.relkind, n.nspname, c.relname FROM pg_class c
    JOIN pg_roles ro ON ro.oid=c.relowner JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname NOT IN ('pg_catalog','information_schema')
      AND ro.rolname = current_user AND c.relkind IN ('r','p','v','m')
  LOOP
    EXECUTE format('ALTER %s %I.%I OWNER TO klikk_user',
      CASE r.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW' ELSE 'TABLE' END,
      r.nspname, r.relname);
  END LOOP;
END $$;
```
Do tables **before** sequences — sequences linked to a table follow its owner and error
if you try to alter them directly. `REASSIGN OWNED BY` does **not** work here (the role
owns the database itself).

---

## 3. Running the app

### 3a. In Docker (current setup)

`docker-compose.override.yml` exists locally and is **gitignored on purpose** — Docker
Compose auto-loads it, so committing it would silently apply `ALLOWED_HOSTS="*"` and a
local-only DB name to anyone running compose in this repo, the VM checkout included.
Recreate it if missing:

```yaml
services:
  web:
    environment:
      DB_HOST: host.docker.internal
      DB_NAME: klikk_financials_v4_vm
      ALLOWED_HOSTS: "*"      # local only — browser reaches this via a proxy
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

```bash
docker compose build web
docker compose up -d web
docker compose logs -f web
docker compose down          # removes the container (image + volumes survive)
```

**Host Postgres is reachable from the container with no Postgres config changes.**
Docker Desktop on macOS proxies `host.docker.internal` through the host, so a
`127.0.0.1`-bound Postgres accepts it as a local connection. `pg_hba.conf` and
`listen_addresses` were **not** modified and don't need to be. (This is macOS-specific —
on Linux it would require real config changes.)

The entrypoint runs `migrate` then `collectstatic` before uvicorn, so first response is
~30s after `up -d`.

### 3b. From the venv (no Docker)

```bash
DB_HOST=/tmp DB_USER=<your-macos-user> DB_PASSWORD= DB_NAME=klikk_financials_v4_vm \
DJANGO_SETTINGS_MODULE=klikk_business_intelligence.settings.development \
.venv/bin/python -m uvicorn klikk_business_intelligence.asgi:application \
  --host 127.0.0.1 --port 8001
```

`DB_HOST=/tmp` selects the Unix socket. Same pattern works for `manage.py`:

```bash
DB_HOST=/tmp DB_USER=<your-macos-user> DB_PASSWORD= DB_NAME=klikk_financials_v4_vm \
  .venv/bin/python manage.py <command>
```

---

## 4. Verifying it works

```bash
curl -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8001/admin/login/     # 200
curl -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8001/xero/core/tenants/  # 401
```

Expected responses — none of these are faults:

| Route | Code | Why |
|---|---|---|
| `/` | 404 | **There is no root route.** Use `/admin/`. |
| `/xero/core/tenants/` | 401 | Auth required |
| `/api/v2/graphql/` (GET) | 405 | POST-only, per the v2 contract |
| any route, unknown `Host` | 400 | `DisallowedHost`. `DEBUG=False` gives a bare 400 with no explanation — check `ALLOWED_HOSTS` first. |

JWT login, for authenticated probing:

```bash
curl -X POST http://127.0.0.1:8001/api/v2/auth/login/ \
  -H 'Content-Type: application/json' \
  -d '{"username":"<user>","password":"<pass>"}'
# -> .tokens.access ; then: -H "Authorization: Bearer $TOKEN"
```

---

## 5. Refreshing local data from the VM

Nightly dumps live on the VM at `/srv/klikk-financials/backups/` (~157MB, 14 days kept).

```bash
ssh klikk-financials 'cat /srv/klikk-financials/backups/klikk_financials_v4_<DATE>.dump' > vm.dump
createdb -O klikk_user klikk_financials_v4_vm
pg_restore -d klikk_financials_v4_vm --no-owner --role=klikk_user -j 4 vm.dump
```

Then migrate (the dump may predate recent migrations) and fix ownership per §2.

**Expected restore noise:** ~224 errors of the form `role "klikk_audit_ro" does not
exist`. That role exists on the VM, not locally. They are `GRANT` / `ALTER DEFAULT
PRIVILEGES` statements only — **data and schema are unaffected**. `pg_restore` exits 1
because of them; that is not a failed restore. Create the role locally if you want them
to apply.

**Version note:** the VM runs PostgreSQL **16.14**, local is **15.17** — a downgrade
restore. It worked cleanly, but it is not guaranteed to for future dumps. `postgresql@16`
is installed locally (not started) if you ever need a matching server.

---

## 6. Gotchas that cost time

1. **`makemigrations` and absolute paths.** `XeroDocument.file` used to serialize a
   `FileSystemStorage` *instance* into migrations, baking in `MEDIA_ROOT` — so every
   machine generated a conflicting migration. Fixed in `c04609b` by passing the
   callable. **If you add a `FileField` with environment-dependent storage, pass the
   callable, never the result.** Always eyeball `makemigrations --dry-run -v 3` for
   absolute paths before committing.
2. **New dependency.** `web_api_v2` needs `strawberry-graphql-django`. If the venv
   predates that, `pip install -r requirements.txt` again.
3. **`web_api_v2` returns nothing.** `viewerContext` gives `entities: []` and
   `NO_ACCESSIBLE_ENTITIES` because `web_api_v2_userentitymembership` is empty. Auth
   works; entity access is unprovisioned. Not a bug in your code.
4. **Don't commit `docker-compose.override.yml`** (see §3a).
5. **Foreground `sleep` is blocked** in the Claude Code Bash tool — an
   `until curl ...; do sleep 2; done` readiness loop hangs until timeout instead of
   polling. Run such waits in the background.
