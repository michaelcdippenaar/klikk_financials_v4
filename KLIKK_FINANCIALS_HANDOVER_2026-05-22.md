# Klikk Financials / Proxmox Handover - 2026-05-22

This handover is for continuing work in Codex Desktop or Claude Desktop.

## Current Goal

Klikk Financials is being migrated from the old mixed environment into a dedicated Proxmox VM. The immediate accounting goal is:

- Pull official reports from the Xero API.
- Store those report values in the database.
- Compare Xero official report output to our reconstructed database reporting layer.
- Treat Xero reports as the reconciliation benchmark.

## Infrastructure State

Proxmox host:

- Host: `pve`
- Host IP: `192.168.1.126`
- Proxmox UI: `https://192.168.1.126:8006`
- Management dashboard: `http://192.168.1.126:8090`
- Public admin IP: `102.135.240.221`
- `console.8-bit.space` routes through `general-services` to the dedicated Klikk Financials VM.

Important VMs:

- `120 general-services` - `192.168.1.128`, public `102.135.240.221/29`, Caddy reverse proxy.
- `130 app-docker` - `192.168.1.130`, app containers for Vault33, Property Manager, ISPX.
- `132 redhat-ibm-cognos` - `192.168.1.132`, IBM PAW/TM1, internal only.
- `133 klikk-financials` - `192.168.1.133`, dedicated Klikk/Group Financials VM.

Klikk Financials VM paths:

- Compose: `/srv/klikk-financials/compose/docker-compose.yml`
- Backend repo: `/srv/klikk-financials/compose/klikk_financials_v4`
- Console repo: `/srv/klikk-financials/compose/klikk_portal`
- Postgres data: `/srv/klikk-financials/postgres`
- Backups: `/srv/klikk-financials/backups`

URLs:

- Public console: `https://console.8-bit.space/app/pipeline`
- Internal console: `http://192.168.1.133:8002`
- Internal backend: `http://192.168.1.133:8001`
- Backend tenants smoke test: `http://192.168.1.133:8001/xero/core/tenants/`

## Proxmox Guardrails Already Applied

- Core VMs auto-start in order: `data-core`, `general-services`, `app-docker`, `klikk-financials`.
- Heavy VMs stay manual start: `vault-gpu-ai`, old `klikk-property-manager`, `redhat-ibm-cognos`.
- CPU caps set so one VM cannot monopolize the host.
- Extra 8 GB host swapfile: `/swapfile-proxmox-guard`.
- Host monitor runs every 5 minutes:
  - Script: `/usr/local/sbin/proxmox-guardrail-check.sh`
  - Log: `/var/log/proxmox-guardrails.log`

## Storage Cleanup Done

Cleaned local Proxmox storage:

- Removed `/root/RedHat-IBM-Cognos2.ova` after import.
- Removed stale `/var/tmp/.guestfs-0`.
- `local` dropped from about `71.92%` used to about `22.70%`.

## Klikk Financials App State

Docker services on VM `133`:

- `klikk-financials-postgres`
- `klikk-financials-v4`
- `klikk-financials-console`

Postgres:

- DB: `klikk_financials_v4`
- DB user: `klikk_user`
- Postgres exposed only on VM loopback: `127.0.0.1:5432`

Daily DB backup:

- `/srv/klikk-financials/scripts/backup-postgres.sh`
- Cron at `02:15`, keeping 14 days.

Recent manual backup before reconciliation work:

- `/srv/klikk-financials/backups/klikk_financials_v4_pre_klikk_tb_fix_20260522_124836.dump`

## Git State

Backend repo:

- Repo: `git@github.com:michaelcdippenaar/klikk_financials_v4.git`
- Branch: `main`
- Latest pushed commit: `554db25 Reconcile Xero P&L trail balance`

Earlier relevant pushed commits:

- `1027b60 Reduce Xero journal bulk batch size`
- `599d1c1 Fix Xero callback and metadata sync`

Console repo:

- Repo: `git@github.com:michaelcdippenaar/klikk_portal.git`
- Earlier relevant pushed commit: `3c2f773 Rename portal to Klikk Financials Console`

Untracked files remain on the backend VM repo and were intentionally not pushed:

- `.env.app-docker`
- `.env.app-docker.bak.*`
- `*.bak.20260522_*`

Do not commit these unless inspected carefully; they may contain secrets or machine-specific config.

## Xero Reconciliation Principle

We are comparing:

```text
Official reports pulled from Xero API
vs
Our database's reconstructed report/trail-balance layer
```

Xero official report output is the benchmark.

Relevant Xero API docs checked:

- Profit and Loss report endpoint: `/Reports/ProfitAndLoss`
- Parameters include `fromDate`, `toDate`, `periods`, `timeframe`, `trackingCategoryID`, `trackingOptionID`, `standardLayout`, `paymentsOnly`
- Required scope: `accounting.reports.read`
- Docs: `https://xeroapi.github.io/xero-node/accounting/index.html#getReportProfitAndLoss`

## Important Accounting Finding

Legacy `journal` rows from the old Xero Journals API pipeline duplicate transaction-sourced rows.

Correct reporting basis for Klikk P&L is now:

```text
transaction rows + manual_journal rows - documented journal exclusions
```

The source journal rows are still retained. The analytical trail balance excludes legacy `journal` rows and applies documented exclusions.

## Code Changes Made

In backend repo `/srv/klikk-financials/compose/klikk_financials_v4`:

### `apps/xero/xero_cube/models.py`

Trail balance SQL now:

- Excludes legacy `j.journal_type = 'journal'`.
- Applies `xero_data_xerojournalexclusion` via `NOT EXISTS`.

### `apps/xero/xero_cube/services.py`

`import_pnl_by_tracking` now deletes only the date range being imported.

Why: Xero P&L report API has a 365-day practical/report range pattern. We need to import history in chunks without wiping previously imported Xero P&L comparison rows.

### `apps/xero/xero_data/models.py`

Added `XeroJournalExclusion`.

Purpose: document audit-safe exclusions for report reconciliation without deleting source data.

### `apps/xero/xero_data/migrations/0011_add_journal_exclusion.py`

Creates `XeroJournalExclusion`.

Note: `manage.py migrate xero_data` said no migrations to apply because the migration had already been applied/generated in the container flow. Verify with:

```bash
ssh mc@192.168.1.133 'cd /srv/klikk-financials/compose && sudo docker compose exec -T postgres psql -U klikk_user -d klikk_financials_v4 -c "select app, name, applied from django_migrations where app='\''xero_data'\'' order by name;"'
```

The table exists:

```text
xero_data_xerojournalexclusion
```

## Klikk-Specific Journal Exclusions Added

Inserted 4 active exclusions for tenant:

```text
Klikk (Pty) Ltd
41ebfa0e-012e-4ff1-82ba-a9a7585c536c
```

Rows:

- `2025-06-30`, manual journal `82660`, description `Prorata VAT input on general expenses for the year`
- `2025-06-30`, manual journal `296176`, description `Prorata VAT input on general expenses for the year`
- `2025-06-30`, manual journal `640630`, description `Prorata VAT input on general expenses for the year`
- `2025-07-31`, manual journal `865424`, description `Recognising interest received (capital gain)`

Reason:

- June: duplicate prorata VAT manual journals that Xero official P&L excludes.
- July: Anchor capital-gain manual journal excluded from Xero official P&L.

## Reconciliation Result

Imported official Xero P&L report comparison data for Klikk for:

```text
2025-06-01 to 2026-05-22
```

Result after trail-balance rebuild and exclusions:

```text
2025-06 OK diff -0.05
2025-07 OK diff  0.00
2025-08 OK diff  0.00
2025-09 OK diff  0.05
2025-10 OK diff  0.00
2025-11 OK diff  0.01
2025-12 OK diff  0.00
2026-01 OK diff -0.01
2026-02 OK diff  0.01
2026-03 OK diff  0.00
2026-04 OK diff  0.01
2026-05 OK diff -0.04
```

Tolerance used: `0.05`.

## Commands Used / Useful Commands

Check Klikk containers:

```bash
ssh mc@192.168.1.133 'cd /srv/klikk-financials/compose && sudo docker compose ps'
```

Rebuild backend image:

```bash
ssh mc@192.168.1.133 'cd /srv/klikk-financials/compose && sudo docker compose up -d --build klikk-financials'
```

Rebuild Klikk trail balance only:

```bash
ssh mc@192.168.1.133 'cd /srv/klikk-financials/compose && sudo docker compose exec -T klikk-financials python manage.py shell -c "from apps.xero.xero_cube.services import create_trail_balance, calculate_balance_sheet_balance_to_date; tenant=\"41ebfa0e-012e-4ff1-82ba-a9a7585c536c\"; create_trail_balance(tenant, rebuild=True); calculate_balance_sheet_balance_to_date(tenant); print(\"DONE\")"'
```

Import Xero official P&L comparison data for Klikk latest 12 months:

```bash
ssh mc@192.168.1.133 'curl -sS --max-time 300 -X POST http://127.0.0.1:8001/xero/cube/import-pnl-by-tracking/ -H "Content-Type: application/json" -d "{\"tenant_id\":\"41ebfa0e-012e-4ff1-82ba-a9a7585c536c\",\"from_date\":\"2025-06-01\",\"to_date\":\"2026-05-22\"}"'
```

Check backend API:

```bash
ssh mc@192.168.1.133 'curl -sS --max-time 20 http://127.0.0.1:8001/xero/core/tenants/ | head -c 300'
```

## What Remains

1. Continue importing Xero official P&L report data for Klikk in <=12 month chunks going backwards.
2. After each chunk, run the monthly comparison.
3. Add only documented `XeroJournalExclusion` rows for true report differences; do not delete source journal rows.
4. Improve the UI page at `https://console.8-bit.space/app/pipeline/compare` so it clearly shows:
   - Xero official report total
   - DB reconstructed total
   - difference
   - documented exclusions
   - status per month/account
5. Later repeat the same process for Tremly and Dippenaar Family after Klikk is fully reconciled.

## Cautions

- Do not run a full all-tenants transaction import yet.
- Do not expose PAW/TM1 publicly.
- Do not commit `.env` files or `.bak` files.
- BigQuery export currently fails/skips because Google credentials are not configured. Local trail-balance rebuild still completes successfully.
- The Xero P&L import makes API calls: one overall call plan plus tracking option calls. Keep imports chunked and controlled.

## Current Confidence

Klikk latest 12 months are reconciled against official Xero P&L report output within rounding tolerance. The new code path is committed and pushed. The next session should continue with historical chunks for Klikk first, then only move to other tenants once Klikk is clean.
