# Scheduled jobs on VM 133 (not in this repo)

The four cron jobs that drive Klikk Financials live on the VM, outside git.
Nothing in the repo reminds anyone they exist or that they were edited, so
record every edit here with its reasoning.

| When (VM local, SAST) | Script | What |
|---|---|---|
| 02:45 daily (user cron `mc`) | `/srv/klikk-financials/scripts/daily-klikk-financials-update.sh` | Nightly pipeline per tenant, then budgeted invoice + quote store syncs (explicit 7-day `modified_since`, `INVOICE_BUDGET=40` calls), then document discovery. Logs to `/srv/klikk-financials/logs/daily-update.log`. |
| 03:30 daily | `/srv/klikk-financials/scripts/daily-tm1-full-refresh.sh` | TM1 / Planning Analytics refresh from the trail balance. |
| 20:00 daily | `/srv/klikk-financials/scripts/xero-doc-backfill.sh` | Attachment backfill. |
| 06:00 Mondays (`/etc/cron.d/klikk-sync`, root) | `/usr/local/sbin/klikk-sync.sh weekly` | Invoices, quotes, aged AR/AP for the three tenants, Investec bank. Logs to `/var/log/klikk-sync/`. |

All four call `manage.py` inside the container; none go through HTTP, so DRF
permissions are never in their path (see `SECURITY-NOTE.md`).

## Edit log

### 2026-09-03 — `klikk-sync.sh` weekly mode now passes `--full` to `sync_xero_invoices`

Backup: `/usr/local/sbin/klikk-sync.sh.bak-2026-09-03`.

Why: on 2026-09-03 the invoice store sync changed its default. A call with no
`modified_since` used to mean "pull every invoice" (38 Xero calls and ~45 s for
Klikk, rewriting 3,700 unchanged rows). It now defaults to the last successful
`invoice_store` stamp minus a 3-day overlap (`default_modified_since` in
`apps/xero/xero_data/invoices_service.py`), which is one call and under a
second. That default reaches the console's Sync Invoices button, the V2
`invoice-sync` stage and the MCP `xero_sync_invoices` tool; `full=true` (or
`--full`) is the explicit way to pull everything.

The weekly cron's `run sync_xero_invoices --all-tenants` line carried no
cursor, so it had been an implicit full pull every Monday. Xero never returns
deleted or voided invoices through `If-Modified-Since`, and that weekly full
pull was the only thing keeping such invoices out of the local store. Making
it `--full` preserves exactly that behaviour at ~55 Xero calls a week. Quotes
in the same script stay incremental.

If the store ever looks stale for a deleted invoice, the fix is a manual
`python manage.py sync_xero_invoices --tenant-id <id> --full`, not a change
to the default.
