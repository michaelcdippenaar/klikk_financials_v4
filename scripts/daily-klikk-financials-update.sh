#!/usr/bin/env bash
#
# Nightly Klikk Financials refresh (cron: 02:45 SAST).
#
# Canonical copy lives in the backend repo at
#   klikk_financials_v4/scripts/daily-klikk-financials-update.sh
# and is installed to /srv/klikk-financials/scripts/ on the VM as a COPY, not
# a symlink: the only clone on that box is a scratch checkout that is not kept
# at origin/main, so a symlink into it would run an arbitrary old commit. See
# the install command in docs/vm_scheduled_jobs.md -- repo and VM must be kept
# byte-identical, so re-run it after changing this file.
#
set -euo pipefail

COMPOSE_DIR="/srv/klikk-financials/compose"
LOG_DIR="/srv/klikk-financials/logs"
LOCK_FILE="/tmp/klikk-financials-daily-update.lock"

# Empty => run every XeroTenant in the DB (the normal nightly behaviour).
# Set TENANT_ID=<guid> to run a single tenant for a one-off / manual run.
TENANT_ID="${TENANT_ID:-}"

# ---------------------------------------------------------------------------
# Xero API budget for the whole nightly run.
#
# The Klikk tenant is capped at 1,000 calls/day — a FIXED window that resets at
# ~14:26 UTC — not the 5,000 in Xero's public docs. The 20:00 UTC document
# backfill is written to leave a ~300-call floor for this job, so everything
# below has to fit inside that floor:
#
#   pipeline (metadata + transactions)   ~20-60 calls, not separately capped
#   invoice store sync                   <= INVOICE_BUDGET per tenant
#   quote sync                           <= QUOTE_BUDGET   per tenant
#   document sync                        whatever is LEFT of NIGHTLY_XERO_BUDGET
#
# The document sync's cap is DERIVED from what the invoice/quote steps actually
# spent rather than being a fixed 300. A cheap night (the normal case — a 7-day
# incremental window is usually 2-5 calls) leaves the doc sync its full
# allowance; an expensive night shrinks the doc sync instead of blowing through
# the tenant cap.
NIGHTLY_XERO_BUDGET="${NIGHTLY_XERO_BUDGET:-300}"
DOC_SYNC_MIN_BUDGET=50
# Floor under Xero's own X-DayLimit-Remaining: the doc sync stops cleanly (exit
# 3) rather than draining a tenant's window. Stated explicitly because the doc
# sync now runs for every tenant, so it must not depend on the command's
# default staying at 300.
DOC_SYNC_HEADROOM="${DOC_SYNC_HEADROOM:-300}"

mkdir -p "$LOG_DIR"
cd "$COMPOSE_DIR"

PIPELINE_OUT="$(mktemp)"
trap 'rm -f "$PIPELINE_OUT"' EXIT

{
  if [ -n "$TENANT_ID" ]; then
    printf '[%s] Starting daily Klikk Financials update for tenant %s\n' "$(date -Is)" "$TENANT_ID"
  else
    printf '[%s] Starting daily Klikk Financials update for ALL tenants\n' "$(date -Is)"
  fi

  status=0
  # One docker exec under one flock; the tenant loop lives in Python so the
  # lock is taken once for the whole run, not once per tenant.
  #
  # stdout is teed so the log keeps streaming in real time AND the invoice /
  # quote steps' API-call total can be read back out to size the doc-sync cap.
  flock -n "$LOCK_FILE" sudo docker compose exec -T klikk-financials python manage.py shell <<PY | tee "$PIPELINE_OUT" || status=$?
import json
import sys
import time
from datetime import datetime, timedelta, timezone as dt_timezone

from apps.xero.xero_core.models import XeroTenant
from apps.planning_analytics.services.pipeline import run_pipeline
from apps.xero.xero_data.invoices_service import sync_xero_invoices
from apps.xero.xero_data.quotes_service import sync_xero_quotes
from apps.xero.xero_sync.api_call_logging import log_xero_api_calls

# --- Xero call budget for the post-pipeline steps -------------------------
#
# Shared across ALL tenants, then capped again per tenant per step. Invoices
# page at ~100 rows/call so a 7-day incremental window is normally 1-2 calls;
# quotes cost one DETAIL call per modified quote, which is why they get the
# tighter budget and go last.
EXTRA_BUDGET_TOTAL = 120     # invoices + quotes, every tenant combined
INVOICE_BUDGET = 40          # per tenant, per night
QUOTE_BUDGET = 25            # per tenant, per night
LOOKBACK_DAYS = 7            # incremental window (overlap safety vs. cost)
DAY_REMAINING_FLOOR = 200    # skip the extras entirely below this

budget_state = {"left": EXTRA_BUDGET_TOTAL, "spent": 0}

override = "${TENANT_ID}".strip()
if override:
    tenants = [(override, override)]
else:
    tenants = list(
        XeroTenant.objects.order_by("tenant_name").values_list("tenant_id", "tenant_name")
    )

if not tenants:
    print("No XeroTenant rows found - nothing to sync.", file=sys.stderr)
    sys.exit(1)

print("Tenants to process: %d" % len(tenants))

failed = []


def day_remaining(tenant_id):
    """Xero's own X-DayLimit-Remaining for this tenant, as last seen by the API
    client and persisted by its HTTP guard.

    Free: this is a Postgres read, never a probe call. It is fresh because the
    pipeline for this tenant has just finished making real calls.
    """
    try:
        from apps.xero.xero_core.models import XeroApiQuota
        row = XeroApiQuota.objects.filter(tenant_id=tenant_id).first()
        return row.day_remaining if row else None
    except Exception:
        return None


def run_extra_step(label, process_key, fn, per_tenant_budget, tenant_obj, tenant_name):
    """Run one budgeted post-pipeline Xero step and account for its calls.

    Never raises: a failure here is recorded and the nightly carries on, exactly
    like the pipeline's own per-tenant isolation.
    """
    if budget_state["left"] <= 0:
        print("STEP %s: SKIPPED (nightly extras budget exhausted)" % label)
        return

    remaining = day_remaining(tenant_obj.tenant_id)
    if remaining is not None and remaining < DAY_REMAINING_FLOOR:
        print("STEP %s: SKIPPED (Xero day_remaining=%s below floor %d)"
              % (label, remaining, DAY_REMAINING_FLOOR))
        return

    budget = min(per_tenant_budget, budget_state["left"])
    # Naive UTC, matching what the management commands hand these services
    # (strptime of a YYYY-MM-DD arg); the value goes to Xero's If-Modified-Since
    # header, not the ORM, so it must not carry a tzinfo the SDK will reformat.
    since = datetime.now(dt_timezone.utc).replace(tzinfo=None) - timedelta(days=LOOKBACK_DAYS)
    started = time.time()
    try:
        stats = fn(tenant_obj, since, budget)
    except Exception as exc:
        print("STEP %s: EXCEPTION %s: %s" % (label, type(exc).__name__, exc), file=sys.stderr)
        failed.append((tenant_name, label, str(exc)))
        return

    calls = int(stats.get("api_calls") or 0)
    budget_state["left"] -= calls
    budget_state["spent"] += calls
    log_xero_api_calls(process_key, calls, tenant=tenant_obj)

    summary = {k: v for k, v in stats.items() if k in (
        "created", "updated", "errors", "invoice_count", "quote_count",
        "line_items_total", "budget_exhausted",
    )}
    print("STEP %s: api_calls=%d budget=%d elapsed=%.1fs %s"
          % (label, calls, budget, time.time() - started, json.dumps(summary, default=str)))

    if stats.get("budget_exhausted"):
        print("STEP %s: WARNING budget exhausted - some rows were not synced tonight" % label)
    if stats.get("errors"):
        failed.append((tenant_name, label, "%s row error(s)" % stats["errors"]))


for tenant_id, tenant_name in tenants:
    print("=" * 72)
    print("TENANT: %s (%s)" % (tenant_name, tenant_id))
    print("=" * 72)
    started = time.time()

    try:
        result = run_pipeline(
            tenant_id,
            load_all=False,
            rebuild_trail_balance=False,
            exclude_manual_journals=False,
            calculate_pnl_ytd=True,
        )
    except Exception as exc:
        # Never let one tenant abort the rest of the run.
        print("EXCEPTION: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        failed.append((tenant_name, "exception", str(exc)))
        continue

    print(json.dumps(result, indent=2, default=str))

    for step in result.get("steps", []):
        if not step.get("success", False):
            failed.append((tenant_name, step.get("step"), step.get("message")))

    # --- Post-pipeline Xero steps -----------------------------------------
    #
    # These run AFTER the pipeline (so the tenant's quota reading is fresh) and
    # BEFORE the document sync below (which is sized from what they spend).
    # The pipeline's own aborts are respected: no point spending budget on a
    # tenant whose token is dead or whose daily limit is already gone.
    aborted = result.get("aborted")
    if aborted:
        print("Post-pipeline steps SKIPPED for %s (pipeline aborted: %s)" % (tenant_name, aborted))
    else:
        try:
            tenant_obj = XeroTenant.objects.get(tenant_id=tenant_id)
        except XeroTenant.DoesNotExist:
            tenant_obj = None
            print("Post-pipeline steps SKIPPED: no XeroTenant row for %s" % tenant_id, file=sys.stderr)

        if tenant_obj is not None:
            run_extra_step(
                "sync_invoices", "invoices",
                lambda t, since, budget: sync_xero_invoices(
                    t, modified_since=since, max_api_calls=budget),
                INVOICE_BUDGET, tenant_obj, tenant_name,
            )
            run_extra_step(
                "sync_quotes", "quotes",
                lambda t, since, budget: sync_xero_quotes(
                    t, modified_since=since, max_api_calls=budget),
                QUOTE_BUDGET, tenant_obj, tenant_name,
            )

    print("elapsed: %.1fs" % (time.time() - started))

print("=" * 72)
# Read by the shell below to size the document-sync cap. Keep the format stable.
print("NIGHTLY_EXTRA_API_CALLS=%d" % budget_state["spent"])

if failed:
    print("Daily update failed steps:", file=sys.stderr)
    for tenant_name, step, message in failed:
        print("- [%s] %s: %s" % (tenant_name, step, message), file=sys.stderr)
    sys.exit(1)

print("All %d tenant(s) completed successfully." % len(tenants))
PY

  # How much of the nightly Xero budget the invoice/quote steps actually spent.
  # Defaults to 0 if the marker is missing (e.g. the pipeline died early), which
  # leaves the doc sync its full historical allowance.
  extra_calls="$(sed -n 's/^NIGHTLY_EXTRA_API_CALLS=\([0-9]\{1,\}\)$/\1/p' "$PIPELINE_OUT" | tail -1)"
  extra_calls="${extra_calls:-0}"

  doc_budget=$(( NIGHTLY_XERO_BUDGET - extra_calls ))
  if [ "$doc_budget" -lt "$DOC_SYNC_MIN_BUDGET" ]; then
    doc_budget="$DOC_SYNC_MIN_BUDGET"
  fi

  # Incremental Xero attachment sync, EVERY tenant. Runs AFTER the pipeline (and
  # after the invoice/quote steps) so UpdatedDateUTC in the local mirror is
  # fresh. 3-day lookback gives overlap safety. A failure for one tenant is
  # logged and the loop moves on, and none of it fails the nightly run
  # (attachment errors are non-critical).
  #
  # The tenant list is read from the DB at run time, skipping rows a human has
  # to re-authorize (hitting those only burns budget) -- the same shape as
  # scripts/xero-doc-backfill.sh. It used to be the Klikk GUID, hardcoded, so
  # Tremly and Dippenaar Family had no incremental attachment pickup at all;
  # see the edit log in docs/vm_scheduled_jobs.md.
  #
  # doc_budget and DOC_SYNC_HEADROOM are PER TENANT and are NOT divided by the
  # loop: each tenant has its own ~1,000-call/day Xero window (docs/xero_api_budget.md).
  # extra_calls, subtracted from the budget above, is the invoice/quote spend
  # across all tenants combined -- conservative on purpose.
  #
  # Set DOC_SYNC_TENANT_IDS="guid guid" (or TENANT_ID=<guid>) for a one-off.
  DOC_SYNC_TENANT_IDS="${DOC_SYNC_TENANT_IDS:-$TENANT_ID}"
  if [ -z "$DOC_SYNC_TENANT_IDS" ]; then
    DOC_SYNC_TENANT_IDS="$(sudo docker compose exec -T postgres \
      psql -U klikk_user -d klikk_financials_v4 -tAc \
      'SELECT tenant_id FROM xero_core_xerotenant WHERE NOT reauth_required ORDER BY tenant_name' \
      2>/dev/null || true)"
  fi

  doc_status=0
  if [ -z "$DOC_SYNC_TENANT_IDS" ]; then
    printf '[%s] Incremental Xero document sync SKIPPED: no tenants resolved\n' "$(date -Is)"
  fi
  for doc_tenant in $DOC_SYNC_TENANT_IDS; do
    printf '[%s] Starting incremental Xero document sync for tenant %s (budget=%s/tenant, headroom=%s, extras spent=%s of %s)\n' \
      "$(date -Is)" "$doc_tenant" "$doc_budget" "$DOC_SYNC_HEADROOM" "$extra_calls" "$NIGHTLY_XERO_BUDGET"
    tenant_doc_status=0
    sudo docker compose exec -T klikk-financials \
      python manage.py sync_xero_documents "$doc_tenant" --since 3 \
      --max-api-calls "$doc_budget" --headroom "$DOC_SYNC_HEADROOM" || tenant_doc_status=$?
    printf '[%s] Finished incremental Xero document sync for tenant %s (exit=%d)\n' \
      "$(date -Is)" "$doc_tenant" "$tenant_doc_status"
    if [ "$tenant_doc_status" -ne 0 ]; then
      doc_status="$tenant_doc_status"
    fi
  done

  printf '[%s] Finished daily Klikk Financials update (exit=%d)\n' "$(date -Is)" "$status"
  exit "$status"
} >> "$LOG_DIR/daily-update.log" 2>&1
