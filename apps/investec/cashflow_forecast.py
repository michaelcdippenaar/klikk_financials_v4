"""Report 1 — Combined 13-Week Cash-Flow forecast engine + accessor.

THE INTERFACE CONTRACT with the report builder (ai-engineer). The single public
entry point is:

    from apps.investec.cashflow_forecast import get_cashflow_forecast
    get_cashflow_forecast(anchor_date=None, weeks=13, scenario="base") -> dict

It returns a JSON-serialisable dict (floats at the boundary, following
report_builder._serialize) describing the forward 13-week cash position of the
WHOLE economic unit (Klikk + trusts + LucaNaude + MC personal combined), tied to
the LIVE liquid opening balance derived from Investec running_balance.

Doctrine (FINANCIAL_PRIORITIES.md + GROUP_FINANCIAL_REPORT_PACK_v2.md Report 1):
  - ONE economic unit. Inter-company flows (Tremly mgmt R660k/yr, MLD rent
    R504k/yr, the Klikk->LucaNaude funding loan, the Anchor liquidation->build)
    ELIMINATE at group level — they are modelled (is_internal=True) but NETTED
    OUT of the group cash series.
  - Cash, not profit. Direct forecast.
  - Boerdery R1,469,286 gross-in W1 (VAT-inclusive) -> R191,646 output VAT out
    on the next VAT201 (conservatively placed in the final week, parameterised).
  - Anchor quarterly income CEASES under the liquidate base case.
  - DWT/dividend is OFF by default in the 13-week window (declaration date
    [REFRESH], falls in Q4 on current timing). The mechanic is deterministic and
    degrades gracefully if TM1 is unreachable — never hard-fails on TM1 timeout.

Money is Decimal end-to-end inside the engine; floats only at the serialised
boundary. The invariant the integrator asserts:

  for each week:  opening + inflows - operating_out - personal_out
                          - debt_service - vat_out  ==  closing  (to the cent)
  and             week[N].closing == week[N+1].opening
"""
from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

logger = logging.getLogger(__name__)

# --- Scenario identifiers ---------------------------------------------------
SCENARIO_BASE = "base"
SCENARIO_BOERDERY_PLUS_60 = "boerdery_plus_60"
SCENARIO_NO_BOERDERY = "no_boerdery"
SCENARIOS_AVAILABLE = [SCENARIO_BASE, SCENARIO_BOERDERY_PLUS_60, SCENARIO_NO_BOERDERY]

ZERO = Decimal("0.00")


def _D(x) -> Decimal:
    """Coerce to a 2dp Decimal."""
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
#  Prototype-anchored constants (GROUP_FINANCIAL_REPORT_PACK_v2.md Report 1)
#  All figures are MC-confirmed / prototype-authoritative. Where the prototype
#  cites a weekly rate, we use the prototype's exact rate so the series ties.
# ---------------------------------------------------------------------------

# Recurring weekly outflows (prototype §1A table).
OPERATING_BURN_WEEKLY = _D("36923")        # ~R160k/mo operating burn
PERSONAL_DRAWING_WEEKLY = _D("14423")      # ~R62.5k/mo via the loan account
DEBT_SERVICE_WEEKLY = _D("1131")           # Flexibond R1,131/wk (recommended sequencing)

# Boerdery (Decision 1) — VAT-inclusive gross-in W1.
BOERDERY_GROSS = _D("1469286")             # VAT-incl invoice, confirmed 30 Jun
BOERDERY_OUTPUT_VAT = _D("191646")         # the lumpy output-VAT spike (15%)

# JSE dividends — lumpy (prototype §1A: W8 +40k, W10 +55k, W12 +45k).
JSE_DIVIDENDS = {8: _D("40000"), 10: _D("55000"), 12: _D("45000")}
JSE_DIVIDENDS_TOTAL = sum(JSE_DIVIDENDS.values(), ZERO)  # 140,000

# Facility headroom (prototype §1 Q6).
FACILITY_FLEXIBOND = _D("1663000")
FACILITY_INVESTEC = _D("8500000")
FACILITY_VOLIERE_EARMARK = _D("2755000")
FACILITY_NET_USABLE = FACILITY_FLEXIBOND + FACILITY_INVESTEC - FACILITY_VOLIERE_EARMARK  # ~7,408,000

# Klikk tenant (canonical).
KLIKK_TENANT_ID = "41ebfa0e-012e-4ff1-82ba-a9a7585c536c"

# Default VAT remit week — Category-B-conservative (prototype places it in W13).
DEFAULT_VAT_REMIT_WEEK = 13

# Internal (eliminating) recurring flows — modelled but netted at group level.
# Tremly mgmt R660k/yr, MLD rent R504k/yr (FINANCIAL_PRIORITIES.md). Weekly rate
# for provenance/audit; they net to zero in the group view (one economic unit).
TREMLY_MGMT_WEEKLY = _D(str(round(660000 / 52, 2)))
MLD_RENT_WEEKLY = _D(str(round(504000 / 52, 2)))


# ---------------------------------------------------------------------------
#  Live opening balance (the re-tie to actuals)
# ---------------------------------------------------------------------------

def _liquid_opening_balance(as_of: datetime.date) -> tuple[Decimal, list[dict]]:
    """Group LIQUID opening cash = the MOST-RECENT running_balance per liquid
    account, as at ``as_of``. Excludes the Mortgage Loan Accounts (liquid=False)
    — they are debt facilities, not spendable cash (they sit in facility
    headroom, never in the opening).

    "Most recent" means the account's last *posted* transaction on/before the
    anchor: ordered by ``posting_date`` (the bank's settlement/sequence date),
    then ``posted_order`` (the bank's intra-day sequence), then ``id`` as a final
    deterministic tie-break. We take that row's running_balance — the account's
    current cleared balance — NOT the max/avg balance and NOT the first row by an
    arbitrary order.

    Why posting_date, not transaction_date (the previous, defective key):
      running_balance is the bank's *cleared* balance after a transaction settles,
      so it advances with posting_date + posted_order. transaction_date is the
      *value* date (when the txn economically occurred) and can lag, lead, or be
      NULL relative to posting — ordering by it picked a stale mid-history row
      whose running_balance was a transient peak, overstating opening cash. We
      COALESCE(posting_date, transaction_date) only so an unposted/value-only row
      still sorts, and gate on the same coalesced date <= as_of.

    Returns (total, provenance_rows). Each provenance row records the account,
    entity, capacity, the as-at date, and the balance (for the audit trail).
    """
    from django.db.models import F
    from django.db.models.functions import Coalesce

    from apps.investec.models import InvestecBankAccount
    from apps.investec.owner_map import attribution_for

    total = ZERO
    rows: list[dict] = []
    for acc in InvestecBankAccount.objects.all():
        attr = attribution_for(acc.account_number)
        if not attr or not attr["liquid"]:
            continue
        last = (
            acc.transactions
            .exclude(running_balance__isnull=True)
            .annotate(_effective_date=Coalesce("posting_date", "transaction_date"))
            .filter(_effective_date__lte=as_of)
            # MOST RECENT posted row: latest effective date, then highest intra-day
            # sequence (posted_order), then highest id. .last() flips the asc order
            # to descending, so the genuine latest row is selected.
            .order_by("_effective_date", "posted_order", "id")
            .last()
        )
        if last is None or last.running_balance is None:
            continue
        bal = _D(last.running_balance)
        total += bal
        rows.append({
            "account_number": acc.account_number,
            "entity": attr["entity"],
            "capacity": attr["capacity"],
            "as_at": last.posting_date or last.transaction_date,
            "balance": bal,
        })
    return _D(total), rows


# ---------------------------------------------------------------------------
#  DWT / dividend mechanic — deterministic, TM1-degrade-graceful
# ---------------------------------------------------------------------------

def dwt_cash_on_clear(net_loan_cleared: Decimal) -> Decimal:
    """DWT cash = 25% of the net loan cleared (Decision 3, verified to the rand).

    Deterministic — no TM1 needed. (gross = net / (1-0.20); DWT = 20% of gross
    = 25% of net.)
    """
    return _D(net_loan_cleared) * Decimal("0.25")


def _maybe_probe_tm1_dividend() -> dict | None:
    """Best-effort read of the TM1-backed dividend forecast. NEVER raises — a TM1
    timeout/unreachable host returns None and the forecast proceeds with the
    DWT OFF (its default in the 13-week window). This honours the brief's
    'degrade gracefully if TM1 is unreachable — never hard-fail'.
    """
    try:
        from apps.ai_agent.skills.dividend_forecast import get_dividend_forecast  # noqa: F401
        # The DWT cash mechanic is deterministic and falls OUTSIDE the 13-week
        # window on current timing, so we do NOT block on a TM1 round-trip here.
        # The hook exists for a future run that wants a live declaration date.
        return None
    except Exception as exc:  # pragma: no cover - import/timeouts both fine
        logger.info("TM1 dividend probe unavailable (non-fatal): %s", exc)
        return None


# ---------------------------------------------------------------------------
#  Scenario obligation builders
# ---------------------------------------------------------------------------

@dataclass
class WeekBucket:
    """Accumulator for one forecast week. All Decimal, all positive magnitudes."""
    inflows: Decimal = ZERO
    operating_out: Decimal = ZERO
    personal_out: Decimal = ZERO
    debt_service: Decimal = ZERO
    vat_out: Decimal = ZERO
    # provenance rows produced for this week (line, amount, source, direction, internal)
    lines: list[dict] = field(default_factory=list)

    def add_inflow(self, amount, line, source, *, internal=False):
        amount = _D(amount)
        if not internal:
            self.inflows += amount
        self.lines.append({"line": line, "amount": amount, "source": source,
                            "direction": "in", "internal": internal})

    def add_out(self, bucket: str, amount, line, source, *, internal=False):
        amount = _D(amount)
        if not internal:
            setattr(self, bucket, getattr(self, bucket) + amount)
        self.lines.append({"line": line, "amount": amount, "source": source,
                            "direction": "out", "internal": internal,
                            "bucket": bucket})


def _build_week_buckets(scenario: str, weeks: int, vat_remit_week: int) -> list[WeekBucket]:
    """Produce the per-week obligation buckets for a scenario (group-level,
    inter-co netted). Index 0 == week 1.
    """
    buckets = [WeekBucket() for _ in range(weeks)]

    # --- Recurring weekly outflows (every week) ---
    for i in range(weeks):
        buckets[i].add_out("operating_out", OPERATING_BURN_WEEKLY,
                           "Operating burn", "rule:recurring/operating")
        buckets[i].add_out("personal_out", PERSONAL_DRAWING_WEEKLY,
                           "Personal drawings (loan account)", "rule:recurring/personal")
        buckets[i].add_out("debt_service", DEBT_SERVICE_WEEKLY,
                           "Debt service (Flexibond)", "rule:recurring/debt")
        # Internal recurring flows — modelled, netted (one economic unit).
        buckets[i].add_out("operating_out", TREMLY_MGMT_WEEKLY,
                           "Tremly mgmt fee (internal — eliminates)",
                           "rule:recurring/intercompany", internal=True)
        buckets[i].add_out("operating_out", MLD_RENT_WEEKLY,
                           "MLD rent (internal — eliminates)",
                           "rule:recurring/intercompany", internal=True)

    # --- JSE dividends (lumpy) — present in all scenarios ---
    for wk, amt in JSE_DIVIDENDS.items():
        if wk <= weeks:
            buckets[wk - 1].add_inflow(amt, f"JSE dividend (W{wk})", "prototype/MC-confirmed")

    # --- Boerdery gross-in + its output VAT (scenario-dependent) ---
    boerdery_week = _boerdery_week_for(scenario)  # None => not in window
    if boerdery_week is not None and boerdery_week <= weeks:
        buckets[boerdery_week - 1].add_inflow(
            BOERDERY_GROSS, f"Boerdery gross-in (W{boerdery_week}, VAT-incl)",
            "prototype/MC-confirmed")
        # Output VAT remits on the VAT201 — placed conservatively at vat_remit_week.
        remit = min(vat_remit_week, weeks)
        buckets[remit - 1].add_out(
            "vat_out", BOERDERY_OUTPUT_VAT,
            f"Boerdery output VAT remit (W{remit})", "rule:vat/boerdery")
    # In no_boerdery: no gross-in AND no Boerdery VAT to remit (prototype §1B).

    # --- Anchor income: CEASED in base/liquidate (do NOT model as recurring) ---
    # The Anchor liquidation->build is a balance-sheet wash inside the unit and
    # is intentionally NOT shown as spendable cash (prototype §1F one-unit note).

    # --- DWT/dividend: OFF by default in the 13-week window (Q4 timing) ---
    _maybe_probe_tm1_dividend()  # non-fatal hook

    return buckets


def _boerdery_week_for(scenario: str) -> int | None:
    """Which week the Boerdery R1.469m gross-in lands, per scenario.

    base             -> W1 (30 Jun, recommended sequencing)
    boerdery_plus_60 -> +60 days; on a 13-week window from ~25 Jun that lands
                        ~24 Aug ≈ W9 (the stress 'slips but eventually arrives'
                        case is the 'delayed' variant; the prototype's headline
                        stress is 'not in window' — see no_boerdery).
    no_boerdery      -> never in window (the prototype §1B stress, closing ~-R534k)
    """
    if scenario == SCENARIO_BASE:
        return 1
    if scenario == SCENARIO_BOERDERY_PLUS_60:
        # 60 days after a W1 (~30 Jun) receipt ≈ 29 Aug ≈ week 10 of a 25-Jun anchor.
        return 10
    if scenario == SCENARIO_NO_BOERDERY:
        return None
    return 1


# ---------------------------------------------------------------------------
#  Public accessor — THE CONTRACT
# ---------------------------------------------------------------------------

def get_cashflow_forecast(
    anchor_date: datetime.date | None = None,
    weeks: int = 13,
    scenario: str = SCENARIO_BASE,
) -> dict:
    """Build the combined 13-week group cash-flow forecast.

    anchor_date : week-1 Monday-ish start. Default = today (25 Jun 2026 in the
                  prototype). The opening balance is read as-at anchor_date.
    weeks       : number of forward weeks (default 13).
    scenario    : one of SCENARIOS_AVAILABLE.

    Returns a JSON-serialisable dict honouring the published contract shape.
    The weekly series FOOTS to the cent and chains (closing[N] == opening[N+1]).
    """
    if scenario not in SCENARIOS_AVAILABLE:
        raise ValueError(f"Unknown scenario {scenario!r}. Available: {SCENARIOS_AVAILABLE}")

    if anchor_date is None:
        anchor_date = datetime.date.today()

    vat_remit_week = DEFAULT_VAT_REMIT_WEEK

    # 1. Live opening balance (the re-tie to actuals).
    opening_balance, opening_rows = _liquid_opening_balance(anchor_date)

    # 2. Per-week obligation buckets (group-level, inter-co netted).
    buckets = _build_week_buckets(scenario, weeks, vat_remit_week)

    # 3. Walk the weeks, footing each one and chaining closings -> openings.
    weeks_series: list[dict] = []
    running = opening_balance
    low_point = {"amount": None, "week": None}
    in_break: dict[str, Decimal] = {}
    out_break: dict[str, Decimal] = {}

    for i, b in enumerate(buckets, start=1):
        week_start = anchor_date + datetime.timedelta(weeks=i - 1)
        opening = running
        closing = (opening + b.inflows
                   - b.operating_out - b.personal_out
                   - b.debt_service - b.vat_out)
        closing = _D(closing)
        weeks_series.append({
            "week": i,
            "start_date": week_start,
            "opening": opening,
            "inflows": b.inflows,
            "operating_out": b.operating_out,
            "personal_out": b.personal_out,
            "debt_service": b.debt_service,
            "vat_out": b.vat_out,
            "closing": closing,
        })
        if low_point["amount"] is None or closing < low_point["amount"]:
            low_point = {"amount": closing, "week": i}
        running = closing

        # Accumulate the in/out breakdown by category (external only).
        for ln in b.lines:
            if ln["internal"]:
                continue
            key = ln["line"].split(" (")[0]  # collapse "Boerdery gross-in (W1...)" -> "Boerdery gross-in"
            if ln["direction"] == "in":
                in_break[key] = in_break.get(key, ZERO) + ln["amount"]
            else:
                out_break[key] = out_break.get(key, ZERO) + ln["amount"]

    # 4. Provenance audit trail.
    provenance: list[dict] = []
    provenance.append({
        "line": "opening liquid cash",
        "amount": opening_balance,
        "source": (
            "InvestecBankTransaction running_balance, latest per liquid account "
            f"as at {anchor_date.isoformat()} "
            f"({len(opening_rows)} accounts; mortgage loan accounts excluded)"
        ),
        "type": "actual",
    })
    for r in opening_rows:
        provenance.append({
            "line": f"opening — {r['entity']} ({r['capacity']}) acct {r['account_number']}",
            "amount": r["balance"],
            "source": f"InvestecBankTransaction running_balance @ {r['as_at'].isoformat()}",
            "type": "actual",
        })
    # Forecast lines (de-duplicated, external only) — the audit of forecast rules.
    seen: set[str] = set()
    for b in buckets:
        for ln in b.lines:
            if ln["internal"]:
                continue
            key = ln["line"].split(" (")[0]
            if key in seen:
                continue
            seen.add(key)
            provenance.append({
                "line": ln["line"].split(" (")[0],
                "amount": _line_total(buckets, key, ln["direction"]),
                "source": ln["source"],
                "type": "forecast",
            })

    result = {
        "anchor_date": anchor_date,
        "weeks": weeks,
        "scenario": scenario,
        "opening_balance": opening_balance,
        "weeks_series": weeks_series,
        "in_out_breakdown": {
            "inflows": dict(in_break),
            "outflows": dict(out_break),
        },
        "facility_headroom": {
            "flexibond": FACILITY_FLEXIBOND,
            "investec": FACILITY_INVESTEC,
            "voliere_earmark": FACILITY_VOLIERE_EARMARK,
            "net_usable": FACILITY_NET_USABLE,
        },
        "low_point": low_point,
        "provenance": provenance,
        "scenarios_available": list(SCENARIOS_AVAILABLE),
    }
    return _serialize(result)


def generate_cashflow_forecast(
    anchor_date: datetime.date | None = None,
    weeks: int = 13,
    scenarios: list[str] | None = None,
    run_id: str | None = None,
    *,
    persist: bool = True,
) -> dict:
    """Generate (and optionally persist) forecast runs for one or more scenarios.

    Writes the forward obligation rows into CashflowForecast, REPLACING any prior
    rows for the same (run_id, scenario) inside a single transaction — so a run is
    fully re-generatable and idempotent. Source bank data is never touched.

    run_id defaults to 'cf-<anchor_date>' so a daily/weekly regenerate overwrites
    the same logical run; pass an explicit run_id to keep historical runs.

    Returns a summary stats dict (rows written per scenario).
    """
    from django.db import transaction as db_transaction
    from apps.investec.models import CashflowForecast

    if anchor_date is None:
        anchor_date = datetime.date.today()
    if scenarios is None:
        scenarios = list(SCENARIOS_AVAILABLE)
    if run_id is None:
        run_id = f"cf-{anchor_date.isoformat()}"

    summary = {"run_id": run_id, "anchor_date": anchor_date.isoformat(),
               "weeks": weeks, "persist": persist, "scenarios": {}}

    for scenario in scenarios:
        if scenario not in SCENARIOS_AVAILABLE:
            raise ValueError(f"Unknown scenario {scenario!r}.")
        buckets = _build_week_buckets(scenario, weeks, DEFAULT_VAT_REMIT_WEEK)
        rows: list[CashflowForecast] = []
        for i, b in enumerate(buckets, start=1):
            week_start = anchor_date + datetime.timedelta(weeks=i - 1)
            for ln in b.lines:
                rows.append(CashflowForecast(
                    run_id=run_id,
                    run_date=anchor_date,
                    scenario=scenario,
                    week_start_date=week_start,
                    week_index=i,
                    entity="",          # group-level; entity attribution lives in actuals
                    capacity="",
                    category=ln["line"].split(" (")[0],
                    direction=ln["direction"],
                    amount=ln["amount"],
                    is_internal=ln["internal"],
                    source=ln["source"],
                    note=ln["line"],
                ))
        if persist:
            with db_transaction.atomic():
                # Replace prior rows for this (run_id, scenario) — re-generatable.
                CashflowForecast.objects.filter(run_id=run_id, scenario=scenario).delete()
                CashflowForecast.objects.bulk_create(rows, batch_size=500)
        summary["scenarios"][scenario] = {"rows": len(rows)}

    return summary


def _line_total(buckets: list[WeekBucket], key: str, direction: str) -> Decimal:
    """Sum a named external line across all weeks (for the provenance amount)."""
    total = ZERO
    for b in buckets:
        for ln in b.lines:
            if ln["internal"] or ln["direction"] != direction:
                continue
            if ln["line"].split(" (")[0] == key:
                total += ln["amount"]
    return _D(total)


# ---------------------------------------------------------------------------
#  Serialisation boundary (Decimal -> float, date -> ISO) — mirrors
#  report_builder._serialize so the dict is JSON-safe for the report builder.
# ---------------------------------------------------------------------------

def _serialize(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: _serialize(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_serialize(x) for x in v]
    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v
