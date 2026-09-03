# Scheduled jobs on VM 133 (not in this repo)

The four cron jobs that drive Klikk Financials live on the VM, outside git.
Nothing in the repo reminds anyone they exist or that they were edited, so
record every edit here with its reasoning.

| When (VM local, SAST) | Script | What |
|---|---|---|
| 02:45 daily (user cron `mc`) | `/srv/klikk-financials/scripts/daily-klikk-financials-update.sh` | Nightly pipeline per tenant, then budgeted invoice + quote store syncs (explicit 7-day `modified_since`, `INVOICE_BUDGET=40` calls), then document discovery. Logs to `/srv/klikk-financials/logs/daily-update.log`. |
| 03:30 daily | `/srv/klikk-financials/scripts/daily-tm1-full-refresh.sh` | TM1 / Planning Analytics refresh from the trail balance. |
| 20:00 daily | `/srv/klikk-financials/scripts/xero-doc-backfill.sh` (copy of `scripts/xero-doc-backfill.sh` in this repo) | Attachment backfill, **every tenant** (each has its own 1,000/day Xero window). Per-tenant `.xero-doc-backfill.<tenant>.done` marker; a finished tenant is skipped. Logs to `/srv/klikk-financials/logs/xero-doc-backfill.log`. |
| 06:00 Mondays (`/etc/cron.d/klikk-sync`, root) | `/usr/local/sbin/klikk-sync.sh weekly` | Invoices, quotes, aged AR/AP for the three tenants, Investec bank. Logs to `/var/log/klikk-sync/`. |

All four call `manage.py` inside the container; none go through HTTP, so DRF
permissions are never in their path (see `SECURITY-NOTE.md`).

## Edit log

### 2026-09-03 — attachment backfill covers all tenants, and is now version-controlled

The script is now version-controlled at `scripts/xero-doc-backfill.sh` in this
repo; the old copy lived only on the VM next to four `.bak-YYYYMMDD` files.

It is installed on the VM as a **copy, not a symlink** — deliberately, and
differently from `daily-klikk-financials-update.sh`. That script symlinks into
`/srv/klikk-financials/compose/klikk_financials_v4`, which `CLAUDE.md` calls a
scratch checkout that is *not* what serves and is not kept at `origin/main`. A
symlink into it therefore points at whatever commit that clone happens to sit
on, which is a worse guarantee than a copy with a dated backup. (The same
caveat applies to `daily-klikk-financials-update.sh` today: check that clone's
HEAD before assuming the nightly runs the version you just merged.)

Install after changing this file — the two must be kept byte-identical:

```bash
scp scripts/xero-doc-backfill.sh klikk-financials:/tmp/xero-doc-backfill.new
ssh klikk-financials 'cp -a /srv/klikk-financials/scripts/xero-doc-backfill.sh \
    /srv/klikk-financials/scripts/xero-doc-backfill.sh.bak-$(date +%Y%m%d) \
  && cp /tmp/xero-doc-backfill.new /srv/klikk-financials/scripts/xero-doc-backfill.sh \
  && chmod +x /srv/klikk-financials/scripts/xero-doc-backfill.sh \
  && bash -n /srv/klikk-financials/scripts/xero-doc-backfill.sh'
```

Why: `TENANT` was hardcoded to Klikk, so in the year the job has existed Tremly
and Dippenaar Family never had a single discovery pass. 15,566 of 33,015
transaction sources sat at `has_attachments=NULL` — not "no attachment", but
"never asked". Measured on 2026-09-03: the true gap (Xero says `HasAttachments`,
we hold no `XeroDocument`) was **2 sources / 4 journal lines** for Klikk, which
had run to completion on 2026-08-30. The unasked tenants held **861 sources /
2,347 non-mirror journal lines**, ~95% of it Tremly invoices from FY2015-FY2019.

The tenant list now comes from `xero_core_xerotenant` at run time, skipping
`reauth_required` rows (hitting those only burns budget). Budgets are per
tenant, not a shared pool, so `NIGHTLY_CALLS` / `HEADROOM` apply per tenant and
the loop does not divide them. A tenant needing more than one night resumes on
the next run — fetched rows drop out of the candidate queue by themselves — so
Tremly's ~1,800-call requirement drains over several nights inside the
1,000/day cap rather than in one sweep that would lock out the 02:45 nightly.

Known gap this change does NOT close: the 02:45 script's incremental document
sync is still hardcoded to one tenant (`DOC_SYNC_TENANT`, the Klikk GUID), so
Tremly and Dippenaar have no incremental attachment pickup. It does not bite
yet — the backfill visits them nightly until their flagged queue is empty — but
the moment a tenant earns its `.done` marker, new attachments on that tenant
stop being collected. Klikk is unaffected because the incremental sync is the
tenant it points at.

Note the daily cap is **1,000, not the 5,000 in Xero's public docs** — measured
2026-08-19 after the ids-queue run died at 1,036 calls, re-confirmed on both
Tremly and Dippenaar on 2026-09-03. Keep the ~300-call floor: the 02:45 job
runs every tenant.

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
