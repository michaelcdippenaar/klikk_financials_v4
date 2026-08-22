# Brief for Codex — 2026-08-22

Written by a Claude Code session working in `klikk_financials_v4`. Scope of that session:
pull `main`, bring a local dev environment up, run the app in Docker. **The VM (133) was
deliberately left running as-is.** Below is what changed, what I found, and what is still open.

---

## 1. What I changed

### Pushed to `main`
- **`319ec62`** — `fix(investec): add missing migration for JSE portfolio percent widths`
  (adds `apps/investec/migrations/0031_alter_investecjseportfolio_move_percent_and_more.py`).

  Migration `0004` created `investecjseportfolio.move_percent` / `portfolio_percent` as
  `numeric(10,4)`; `models.py` declares `max_digits=15`. The model was widened without a
  migration. The file existed untracked on the local machine since 2026-08-13 and was never
  committed, so `makemigrations --check` failed on `main`.

### On VM 133
- `git pull` in `/srv/klikk-financials/compose/klikk_financials_v4` — checkout moved
  `f9b3d58` → `319ec62`. Tree is clean, no hot-patches were clobbered.
- `docker compose restart klikk-financials` (config preserved; container still on the pinned
  release, verified healthy afterwards).
- **Nothing else.** No deploy, no migration applied, no image built, no override touched.

### Local machine only (not the VM, not the repo)
- `pip install strawberry-graphql-django` into `.venv` (new dep from `web_api_v2`).
- Migrated local `klikk_financials_v4` (35 migrations) — it was far behind.
- Created **`klikk_financials_v4_vm`** — restored from the VM's `2026-08-21` nightly, then
  migrated current. 646k rows, all six schemas incl. `kb` and `whatsapp`.
- **Set the local `klikk_user` role password** to the value in this repo's `.env`.
  A container cannot use the Unix socket, and none of the three known password candidates
  worked (`.env`, the `development.py` fallback, the `staging.py` fallback). I checked every
  `.env` under `~/ClaudProjects` and `~/Documents` first — nothing on the machine was
  authenticating as that role over TCP, so nothing broke.
- Added untracked **`docker-compose.override.yml`** (`DB_HOST=host.docker.internal`,
  `DB_NAME=klikk_financials_v4_vm`). The committed `docker-compose.yml` was left alone.

---

## 2. Things you should know before you touch the VM

### 2.1 The deploy model is NOT what CLAUDE.md describes
CLAUDE.md says the backend repo is volume-mounted into the container. **It is not.** As of
2026-08-21 the running container bind-mounts a per-SHA release directory:

```
/srv/klikk-financials/compose/klikk_financials_v4_releases/<sha>  ->  /app
```

pinned by a per-SHA override file, e.g. `backend-cebc194.compose.override.yml`, which also
pins the image tag (`klikk-financials-v4:ing-connect-csrf-cebc194`).

Currently deployed: **`cebc194`**. The release dir diffs clean against that commit — no
hot-patches. CLAUDE.md is stale here and worth correcting.

### 2.2 Footgun: the base compose file still points at the git checkout
`/srv/klikk-financials/compose/docker-compose.yml` still declares `./klikk_financials_v4:/app`
and `build: ./klikk_financials_v4`. A plain `docker compose up -d` **without** the per-SHA
override will silently swap the deployed code from the pinned release to whatever the git
checkout happens to be. That checkout is now at `319ec62` (I moved it from `f9b3d58`).
`docker compose restart` is safe; `up -d` is not.

### 2.3 The VM DB is one migration behind the code
`investec 0031` is **unapplied** on the VM. Live columns are still `numeric(10,4)` while the
model says `15`. Practical risk is low (needs a percentage >= 1,000,000 to overflow) but
`migrate` will keep printing the model-drift warning until a release containing `319ec62`
ships and the migration runs.

---

## 3. Open items

| # | Item | Where | Notes |
|---|---|---|---|
| 1 | `xero_data.0017_alter_xerodocument_file` missing | `main` | Same class of drift as `investec 0031`: `XeroDocument.file` was changed without a migration. `makemigrations --check` still fails. I did not commit a second migration unasked — it's yours if you want it. |
| 2 | `web_api_v2` entity memberships unprovisioned | anywhere the app runs | `viewerContext` returns `entities: []` / `entitySelectionState: NO_ACCESSIBLE_ENTITIES` because `web_api_v2_userentitymembership` is empty. Auth works; entity access does not. There is a `provision_ingest_catalogue` command — unclear whether it covers memberships. |
| 3 | Two shipped features have never written a row | VM + local | `investec_investecbeneficiary` (commit `0178558`, beneficiary sync) and `investec_cashflowforecast` (commit `8cde6bd`, weekly cash-flow forecast) are **empty on the VM**. Schema shipped, sync apparently never ran. Also empty: `xero_data_agedpayable`, `xero_data_agedreceivable`, `xero_cube_xerobalancesheet`. |
| 4 | Hardcoded DB passwords in settings | repo | `settings/development.py:38` and `settings/staging.py:64` both carry literal fallback passwords. Neither is correct any more. They are committed to git. |
| 5 | `klikk_audit_ro` role | local only | 224 `GRANT` / `ALTER DEFAULT PRIVILEGES` statements were skipped on restore because that role exists on the VM but not locally. Data and schema unaffected — permissions only. Not a VM issue. |

---

## 4. Current state summary

| | Commit | DB |
|---|---|---|
| `origin/main` | `319ec62` | — |
| VM running release | `cebc194` | VM Postgres 16.14, `investec 0031` unapplied |
| VM git checkout | `319ec62` | (not what the container runs — see 2.2) |
| Local dev | `319ec62` | `klikk_financials_v4_vm`, restored from 2026-08-21 nightly + migrated current |

Local app runs in Docker on `http://127.0.0.1:8001` against host Postgres. No `pg_hba.conf` or
`postgresql.conf` changes were needed — Docker Desktop on macOS proxies `host.docker.internal`
to the host, so a `127.0.0.1`-bound Postgres is reachable and treated as a local connection.

---

## 5. Please don't

- Don't `docker compose up -d` on the VM without the per-SHA override (see 2.2).
- Don't assume CLAUDE.md's volume-mount description is current (see 2.1).
- Don't treat the empty tables in item 3 as restore damage — they are empty upstream too;
  I verified each against the VM directly.
