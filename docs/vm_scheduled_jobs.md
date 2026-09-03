# Scheduled jobs on VM 133 (not in this repo)

The four cron jobs that drive Klikk Financials live on the VM, outside git.
Nothing in the repo reminds anyone they exist or that they were edited, so
record every edit here with its reasoning.

| When (VM local, SAST) | Script | What |
|---|---|---|
| 02:45 daily (user cron `mc`) | `/srv/klikk-financials/scripts/daily-klikk-financials-update.sh` (copy of `scripts/daily-klikk-financials-update.sh` in this repo) | Nightly pipeline per tenant, then budgeted invoice + quote store syncs (explicit 7-day `modified_since`, `INVOICE_BUDGET=40` calls), then a 3-day incremental document sync **per tenant** (each tenant's own 1,000/day window; `--headroom 300`). Logs to `/srv/klikk-financials/logs/daily-update.log`. |
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
tenant it points at. **Closed the same day — see the next entry.**

Note the daily cap is **1,000, not the 5,000 in Xero's public docs** — measured
2026-08-19 after the ids-queue run died at 1,036 calls, re-confirmed on both
Tremly and Dippenaar on 2026-09-03. Keep the ~300-call floor: the 02:45 job
runs every tenant.

### 2026-09-03 — the 02:45 incremental document sync covers every tenant

`scripts/daily-klikk-financials-update.sh` in this repo. Same bug class as the
backfill entry above, in the second of the two scripts that carried it.

Why: line 252 read `DOC_SYNC_TENANT="41ebfa0e-..."` — the Klikk GUID, hardcoded
— so the incremental attachment sync had only ever run for Klikk. Tremly and
Dippenaar Family were never visited by it.

Why it had not bitten, and why it was about to. Coverage of a tenant comes from
exactly two jobs: the 20:00 backfill, which stops visiting a tenant the moment
that tenant earns its `.xero-doc-backfill.<tenant>.done` marker, and this
incremental sync, which pointed at one GUID. A tenant is covered while at least
one of them visits it. On 2026-09-03 the markers read:

| Tenant | `.done` marker | Backfill | Incremental | Covered? |
|---|---|---|---|---|
| Klikk | 2026-08-30 | skips | yes | yes — but only because line 252 named this exact GUID |
| Dippenaar Family | 2026-09-03 13:09 | skips | no | **no — nothing collected its attachments from tonight** |
| Tremly | none yet | still visiting | no | yes, for ~3 more nights until its queue drains |

So Klikk's safety was a coincidence between a marker and a hardcoded GUID, and
Dippenaar had already fallen through the gap when this was fixed.

The fix follows `scripts/xero-doc-backfill.sh` rather than inventing a second
pattern: the tenant list is read from `xero_core_xerotenant` at run time,
skipping `reauth_required` rows, and the sync loops over it. A failure for one
tenant is logged and the loop moves on; the aggregate exit code is still
reported, and document errors still do not fail the nightly.

Budget: `doc_budget` and the new explicit `DOC_SYNC_HEADROOM=300` are **per
tenant** and are NOT divided by the loop, because each tenant has its own
~1,000-call/day Xero window (`docs/xero_api_budget.md`). `--headroom` is now
passed explicitly rather than relying on the command's `DEFAULT_HEADROOM`
staying at 300, since this loop now runs three times a night instead of once.
`extra_calls`, still subtracted from `NIGHTLY_XERO_BUDGET`, is the invoice/quote
spend across all tenants combined — conservative on purpose.

`TENANT_ID=<guid>` (the existing single-tenant override) now also scopes the
document sync, and `DOC_SYNC_TENANT_IDS="guid guid"` scopes just that step.

Verified 2026-09-03 15:51 UTC by running the changed block verbatim against the
VM with a deliberately small `doc_budget=40`. All three tenants were visited:

| Tenant | discovery | fetch | total calls | exit | `X-DayLimit-Remaining` after |
|---|---|---|---|---|---|
| Dippenaar Family | 3 | 0 | 3 | 0 | 964 |
| Klikk | 3 | 40 | 43 | 0 | 955 |
| Tremly | 1 | 0 | 1 | 3 | 295 |

Tremly stood down on the headroom floor (`X-DayLimit-Remaining=295 < 300`) after
one call — the 20:00 backfill had already drained its window that afternoon.
That is the guard working, and it also demonstrated the loop does not abort on
a per-tenant exit 3: Klikk ran after Dippenaar and Tremly's stand-down was the
last step. Klikk's "stopped early: max-api-calls" is the artificial 40-call test
budget, not a production condition.

No back-window catch-up is needed **provided this is installed and running by
the 02:45 run of 2026-09-06.** The `--since 3` window cannot reach across a gap
wider than three days, and Dippenaar's coverage stopped at 13:09 on 2026-09-03.
Tremly needs nothing by construction: the backfill's completion test is "no
flagged rows left without documents", so the handover to the incremental sync is
seamless whenever its marker appears. If the install does slip past 2026-09-06,
do NOT widen `--since` (it would cost a full re-discovery inside the nightly's
300-call slice) — instead delete that tenant's marker and let the 20:00 backfill
re-run one night:

```bash
ssh klikk-financials 'rm /srv/klikk-financials/scripts/.xero-doc-backfill.<tenant-guid>.done'
```

Header comment (lines 5-11) rewritten in the same commit: it described the VM
install as a symlink "to the backend repo", which was already wrong — it pointed
into the scratch checkout — and argued for the pattern being removed. Written
by the session doing the installation change; carried here to keep one file to
one editor.

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

### 2026-09-03 — the 02:45 nightly is installed as a copy, not a symlink into the scratch checkout

`/srv/klikk-financials/scripts/daily-klikk-financials-update.sh` was a symlink
to `/srv/klikk-financials/compose/klikk_financials_v4/scripts/daily-klikk-financials-update.sh`.
That directory is the scratch checkout — `CLAUDE.md` says it "is a scratch
checkout, not what serves", because the containers bind-mount per-commit release
directories (`klikk_financials_v4_releases/<full-sha>`) onto `/app` instead.
Nobody keeps it at `origin/main`. So cron executed whatever commit that clone
happened to sit on, and would drift further every merge.

(`CLAUDE.md` is **not** in this repo and not on the VM — it is untracked at the
root of the local working checkout, and the quote above is from its deploy
section. That is the only place the citation is checkable.)

The failure mode is the bad kind: the job still succeeds. It just runs old code,
which surfaces as stale or missing data, never as an error.

It is now a real file with a dated backup, matching how
`scripts/xero-doc-backfill.sh` was installed earlier the same day (see the entry
above for that reasoning, which applies unchanged here).

**As installed, 2026-09-03 15:57 UTC.** Installed from `origin/main` at
`4b955fd` — the tip, deliberately, rather than the equivalent branch SHA, so
there is only one answer to "what is installed". Repo and VM verified
byte-identical at md5 `e901c4162c4665d413065cc35750a9b9`. The backup at
`daily-klikk-financials-update.sh.bak-20260903` is itself a symlink: `cp -a`
preserved it, so the backup records *that the install was a symlink and where it
pointed*, which is the fact worth keeping. Its content at that time was md5
`0c0b575618f68e17dc4b105c64b36363`, recoverable from git at `b763735` or
`0de76c8`. The scratch checkout was confirmed clean and still at `b763735`
afterwards — proof the install did not write through the symlink.

**Survey — this was the only instance, and the answer is "one script".** Before
changing anything, the whole box was swept so nobody needs to repeat it: every
symlink under `/usr/local`, `/etc` and `/srv/klikk-financials` resolving into
`klikk_financials_v4` or `klikk_portal`; `/etc/cron.d/`, `/etc/cron*`,
`/var/spool/cron` and `/etc/systemd` grepped for the scratch paths; all systemd
timers listed; every user's crontab dumped. Exactly one hit, the one above.
Nothing under `/usr/local/sbin/` points into either checkout — the scripts there
(`klikk-sync.sh`, `audit-psql.sh`, `audit-precompute.sh`, `mcp-rebuild`) are all
real files. The four cron jobs are in `mc`'s user crontab and
`/etc/cron.d/klikk-sync`, not root's crontab.

**It was latent, not biting.** The clone sat at `b763735`, 12 commits behind
`origin/main` (`0de76c8`) — but this file was byte-identical at both
(md5 `0c0b575618f68e17dc4b105c64b36363`). Cron was running correct content by
luck, not by design. The conversion was therefore behaviourally a no-op on the
day it was made, which is the cheapest possible moment to make it.

Those two facts together are the whole reason this pattern exists: because the
clone is never updated, a merge to `origin/main` does **not** reach cron. It
reaches cron when the install command below is run, and at no other time.

**Fast-forwarding the scratch checkout was considered and rejected.** It would
have bought nothing on the day (the file already matched the tip) and it would
make the clone look authoritative, which `CLAUDE.md` explicitly says it is not.
The next person would then trust it. Pinning the install to a copy refreshed by
a recorded command keeps the clone as disposable as it is meant to be.

**Trap for anyone reusing the backfill's install command here — verified on the
VM before installing.** `cp` onto a symlink *follows* it. The naive command
would have written the new content through the symlink into the scratch
checkout, dirtied a clone nobody watches, left the symlink in place, converted
nothing, and exited 0.

The problem is not that the command is wrong; it is that its failure mode is
**silent success**. Everyone would have believed the hazard was closed while it
was untouched, and the next person to look would have found a dirty clone with
no explanation. A command that cannot fail loudly is worse than one that fails.
That is why the sequence below both removes the symlink first and then *asserts*
`test ! -L` — the assertion is the durable part, and it is repeated in the
standing checks so a future "helpful" re-link is caught rather than assumed
away.

Stated generally, because it will come up again: the backfill's install command
is correct for **refreshing** a file that is already a copy, and unsafe as a
template for **converting** a symlink into one.

Install after changing this file — repo and VM must be kept byte-identical:

```bash
scp scripts/daily-klikk-financials-update.sh \
  klikk-financials:/tmp/daily-klikk-financials-update.new
ssh klikk-financials 'D=/srv/klikk-financials/scripts/daily-klikk-financials-update.sh \
  && cp -a "$D" "$D.bak-$(date +%Y%m%d)" \
  && rm -f "$D" \
  && cp /tmp/daily-klikk-financials-update.new "$D" \
  && chmod +x "$D" \
  && bash -n "$D" \
  && test ! -L "$D" && echo "OK: real file, not a symlink"'
```

**Post-install verification — re-runnable, and worth re-running any time.** Run
from a checkout at the commit you believe is installed. Byte-identity and
not-being-a-symlink are the two properties that make a copy trustworthy, so both
should be verifiable rather than assumed:

```bash
# 1. it is a real file, not a symlink (catches a re-link)
# 2. cron's path resolves to the installed file itself
# 3. it parses
ssh klikk-financials 'D=/srv/klikk-financials/scripts/daily-klikk-financials-update.sh
  test ! -L "$D" && echo "1 OK: real file"      || echo "1 FAIL: symlink -> $(readlink "$D")"
  crontab -l -u mc | grep -q "$D" && echo "2 OK: cron points here" || echo "2 FAIL: cron entry moved"
  bash -n "$D" && echo "3 OK: parses"
  md5sum "$D"'

# 4. VM matches this repo, byte for byte
md5sum scripts/daily-klikk-financials-update.sh   # macOS: md5 -q <file>
```

If check 1 fails, someone re-linked it and cron is back on the scratch clone.
If check 4 differs, the VM is running something other than this repo. Either
way the install command above is the fix.

**This install carries a deadline.** The tenant fix it delivers is what gives
Dippenaar and Tremly incremental attachment pickup, and the incremental sync's
`--since 3` window cannot reach back further than three days. Dippenaar's
coverage gap opens at 2026-09-03 13:09 (the moment it earned its
`.xero-doc-backfill.<guid>.done` marker and dropped out of the 20:00 backfill's
queue). The last nightly run whose 3-day window still reaches back past that
moment is **02:45 on 2026-09-06** — so the install must be live before it for
there to be no gap at all.

Past that deadline the recovery is NOT to widen `--since`. It is to delete that
tenant's marker so the 20:00 backfill re-covers it for a night:

```bash
ssh klikk-financials 'rm /srv/klikk-financials/scripts/.xero-doc-backfill.<guid>.done'
```

**Smoke check the one genuinely new command, after installing.** Every other
step in the script already used `sudo docker compose exec` and is proven in
cron, but the tenant enumeration is a new command in a new place, and it is
written `2>/dev/null || true` — so if it ever fails it resolves to an empty
tenant list and the document sync is skipped, logging
`SKIPPED: no tenants resolved` rather than failing. Confirm it actually returns
tenants rather than trusting that `bash -n` passed:

```bash
ssh klikk-financials 'sudo -n true && echo "sudo OK: NOPASSWD, no tty needed (as cron runs)"
  cd /srv/klikk-financials/compose && sudo -n docker compose exec -T postgres     psql -U klikk_user -d klikk_financials_v4 -tAc     "SELECT tenant_id FROM xero_core_xerotenant WHERE NOT reauth_required ORDER BY tenant_name"'
```

Verified on 2026-09-03 before installing: `sudo -n` succeeds (`mc` has
`NOPASSWD: ALL`) and the query returns the 3 expected tenants. Note the DB role
is `klikk_user`, not `postgres` — `-U postgres` fails with
`role "postgres" does not exist`.

**Safe window for this kind of edit.** The jobs this touches must not be skipped
or rescheduled — they carry the nightly Xero sync and the trial-balance rebuild.
Fires are 02:45, 03:30 and 20:00 daily plus 06:00 Mondays (VM is UTC). This
change was made mid-afternoon UTC, ~4 h clear of the 20:00 backfill and ~11 h
clear of the 02:45 nightly, so no run overlapped it.

The pre-existing backups from August (`.bak-20260817`, `.bak.20260817`,
`.bak.20260819`, `.bak.20260819-preinvoicesync`) were left in place — they are
the only record of those versions. Note they mix `.bak-` and `.bak.` separators;
new ones use `.bak-YYYYMMDD`.
