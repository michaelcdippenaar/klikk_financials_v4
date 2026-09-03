#!/usr/bin/env bash
#
# Xero attachment backfill — all tenants (2026-09-03).
#
# Canonical copy lives in the backend repo at
#   klikk_financials_v4/scripts/xero-doc-backfill.sh
# and is installed to /srv/klikk-financials/scripts/xero-doc-backfill.sh as a
# COPY (not a symlink -- see docs/vm_scheduled_jobs.md for why: the clone that
# daily-klikk-financials-update.sh symlinks into is a scratch checkout that is
# not kept at origin/main). Keep the two byte-identical; the install command is
# in that doc.
#
# History:
#   2026-08-17  ids-queue design: probed GET /{Endpoint}/{Guid}/Attachments once
#               per transaction (17,281 calls just to learn who had files).
#   2026-08-19  HasAttachments design: a DISCOVERY pass over the paged list
#               endpoints at pageSize=1000 (~19 calls for all of Klikk), then a
#               FETCH pass only for rows flagged true with no stored document.
#   2026-09-03  ALL TENANTS. The tenant was hardcoded to Klikk, so Tremly and
#               Dippenaar Family had never had a discovery pass at all: 15,566
#               transaction sources sat at has_attachments=NULL and 861 of them
#               (2,347 non-mirror journal lines, ~95% Tremly invoices from
#               FY2015-FY2019) held documents Xero had that we did not. Klikk
#               itself finished on 2026-08-30 and its marker keeps it a no-op.
#
# Budget guards: flock single-flight across the WHOLE run; one pre-flight probe
# per tenant that also reads X-DayLimit-Remaining; fetch bounded by
# --max-api-calls AND a --headroom floor read live from that header;
# DailyLimitReached / re-auth => exit 3 => that tenant stands down for the night
# and the loop moves on. Budgets are PER TENANT (each has its own daily window),
# so the loop does not share one pool.
#
# BUDGET FACT (measured 2026-08-19, re-confirmed 2026-09-03): a tenant's
# X-DayLimit-Remaining reads ~1,000 right after the daily reset (~14:26 UTC),
# i.e. the cap is 1,000/day, NOT the 5,000 in Xero's public docs. The caps below
# leave ~300 per tenant for the 02:45 nightly, which runs every tenant.
#
# A tenant is marked done when its fetch reports no flagged rows left without
# documents; a tenant needing more than one night's budget simply resumes on the
# next run, because fetched rows drop out of the candidate queue by themselves.
set -u
DIR=/srv/klikk-financials/scripts
LOG=/srv/klikk-financials/logs/xero-doc-backfill.log
NIGHTLY_CALLS=${NIGHTLY_CALLS:-500}    # fetch-pass cap PER TENANT (list + download calls)
HEADROOM=${HEADROOM:-300}              # never drive a tenant's X-DayLimit-Remaining below this
MIN_START=$((HEADROOM + 100))          # don't even start a tenant below this

# Empty => every XeroTenant in the DB. Set TENANT_IDS="guid guid" for a one-off.
TENANT_IDS="${TENANT_IDS:-}"

exec 9>/tmp/xero-doc-backfill.lock
flock -n 9 || { echo "[$(date)] another backfill running, skipping" >> "$LOG"; exit 0; }
{
echo "[$(date "+%F %T")] backfill start (all tenants; cap=$NIGHTLY_CALLS/tenant headroom=$HEADROOM)"

if [ -z "$TENANT_IDS" ]; then
  # Skip tenants a human has to re-authorize; hitting them only burns budget.
  TENANT_IDS=$(docker exec klikk-financials-postgres psql -U klikk_user -d klikk_financials_v4 \
      -tAc "SELECT tenant_id FROM xero_core_xerotenant WHERE NOT reauth_required ORDER BY tenant_name" 2>/dev/null)
fi
if [ -z "$TENANT_IDS" ]; then
  echo "[$(date "+%F %T")] no tenants to process, aborting"; exit 0
fi

for TENANT in $TENANT_IDS; do
  DONE="$DIR/.xero-doc-backfill.$TENANT.done"
  # Legacy single-tenant marker (Klikk finished 2026-08-30 under the old name).
  if [ "$TENANT" = "41ebfa0e-012e-4ff1-82ba-a9a7585c536c" ] && [ -f "$DIR/.xero-doc-backfill.done" ] && [ ! -f "$DONE" ]; then
    mv "$DIR/.xero-doc-backfill.done" "$DONE"
    echo "[$(date "+%F %T")] $TENANT: migrated legacy done marker"
  fi
  [ -f "$DONE" ] && { echo "[$(date "+%F %T")] $TENANT: already complete, skipping"; continue; }

  # PRE-FLIGHT: exactly one list call. Confirms auth + tenant + reads the live
  # daily allowance; abort this tenant on any failure so a locked-out day never
  # burns budget.
  probe=$(docker exec klikk-financials-v4 python manage.py sync_xero_documents "$TENANT" --probe 2>&1 | tail -1); prc=$?
  case "$probe" in
    *"probe OK"*) ;;
    *) echo "[$(date "+%F %T")] $TENANT: pre-flight failed (rc=$prc), skipping: $probe"; continue;;
  esac
  remaining=$(printf "%s" "$probe" | sed -n 's/.*X-DayLimit-Remaining=\([0-9]*\).*/\1/p')
  echo "[$(date "+%F %T")] $TENANT: pre-flight OK: $probe"
  if [ -n "$remaining" ] && [ "$remaining" -lt "$MIN_START" ]; then
    echo "[$(date "+%F %T")] $TENANT: only $remaining calls left today (< $MIN_START); standing down"
    continue
  fi

  # DISCOVERY (full) + FETCH (bounded). One invocation, one bulk job per tenant.
  raw=$(docker exec klikk-financials-v4 python manage.py sync_xero_documents "$TENANT" \
          --max-api-calls "$NIGHTLY_CALLS" --headroom "$HEADROOM" 2>&1); rc=$?
  printf "%s\n" "$raw" | grep -E "^Discovery|^  (Invoice|BankTransaction|CreditNote):|^Synced|Stopped early|Daily|429|401|403|Unauthorized|invalid_grant|Traceback|re-authorization" \
    | sed "s/^/[$(date "+%F %T")] $TENANT: /"
  case "$rc" in
    0) ;;
    3) echo "[$(date "+%F %T")] $TENANT: stopped for budget/auth (rc=3); standing down for tonight";;
    *) echo "[$(date "+%F %T")] $TENANT: fetch finished with per-item errors (rc=$rc); see container logs";;
  esac

  # COMPLETE when the fetch pass reports no flagged rows left without documents.
  left=$(printf "%s\n" "$raw" | sed -n 's/.*Remaining: \([0-9]*\),.*/\1/p' | tail -1)
  if [ "$rc" -ne 3 ] && [ -n "$left" ] && [ "$left" -eq 0 ]; then
    echo "[$(date "+%F %T")] $TENANT: backfill COMPLETE (no flagged transactions without documents)"
    touch "$DONE"
  fi
  echo "[$(date "+%F %T")] $TENANT: end (rc=$rc, remaining-flagged=${left:-?})"
done

echo "[$(date "+%F %T")] backfill end (all tenants)"
} >> "$LOG" 2>&1
