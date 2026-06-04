"""
Cost & Sustainability Cockpit — Recurring-Cash Cost-Cut Finder.

Reads from TM1 (gl_src_trial_balance) live — requires VM 132 (TM1) up. Surfaces
the *controllable* cost base: recurring cash expense by account, this year vs
prior, ranked by size and YoY growth. Non-cash (depreciation) and non-recurring
one-offs are excluded via the 'Amount - Recurring Cash Flow' measure, so the
report shows only what can actually be cut.
"""
from . import mdx_query as mq

CUBE = "gl_src_trial_balance"
MEAS_DIM = "measure_gl_src_trial_balance"
RECURRING = "Amount - Recurring Cash Flow"
DEFAULT_GROUPS = ["OVERHEADS", "DIRECTCOSTS", "EXPENSE"]

# TM1 element Type: 1=Numeric (leaf), 2=String, 3=Consolidated
_CONS = (3, "Consolidated", "C")


def _leaves(dim, parent, group, group_of, order, seen):
    """Recurse a consolidation to its leaf (postable) accounts; first group wins."""
    for c in mq.dimension_children(dim, parent):
        nm, typ = c["name"], c.get("type")
        if typ in _CONS:
            _leaves(dim, nm, group, group_of, order, seen)
        elif nm not in seen:
            seen.add(nm)
            group_of[nm] = group
            order.append(nm)


def _account_key(name):
    """Extract the stable account code (e.g. 'kl_HH--EM01/1') from the friendly
    TM1 name attribute 'kl_HH--EM01/1_Employee Expenses...'. Code = first two
    underscore tokens ('kl' + body); the rest is the description."""
    if not name:
        return name
    parts = str(name).split("_")
    if len(parts) >= 2 and parts[0] == "kl":
        return "kl_" + parts[1]
    return name


def _behaviour_map():
    """{account_key: {behaviour, driver, cuttability, is_addressable}} from the
    CostBehaviour table (CFO seed + any user overrides)."""
    from apps.planning_analytics.models import CostBehaviour
    return {cb.account_key: {
                "behaviour": cb.behaviour, "driver": cb.driver,
                "cuttability": cb.cuttability, "is_addressable": cb.is_addressable,
                "is_manageable": cb.is_manageable}
            for cb in CostBehaviour.objects.all()}


def _pull(entity, year, version, accounts, months=None):
    """Recurring-cash by account. months=None -> full year (All_Month). A list of
    month members (e.g. ['Jan','Feb',...]) -> sum just those months (YTD), so an
    in-progress year compares like-for-like against the same prior-year months."""
    if not months and months is not None:
        # explicit empty list (e.g. future year): no closed months
        return {a: 0.0 for a in accounts}
    if months is None:
        res = mq.run_pivot(
            cube=CUBE,
            rows=[{"dimension": "account", "members": accounts}],
            cols=[{"dimension": MEAS_DIM, "members": [RECURRING]}],
            filters={
                "year": str(year), "month": "All_Month", "version": version, "entity": entity,
                "contact": "All_Contact", "tracking_1": "All_Tracking_1", "tracking_2": "All_Tracking_2",
            },
            suppress=False,
        )
        out = {}
        for row in res["rows"]:
            acct = row["members"][-1]
            v = row["cells"][0]["value"] if row["cells"] else None
            out[acct] = float(v) if isinstance(v, (int, float)) else 0.0
        return out
    # YTD: account x month grid, measure pinned in WHERE; sum the month cells.
    res = mq.run_pivot(
        cube=CUBE,
        rows=[{"dimension": "account", "members": accounts}],
        cols=[{"dimension": "month", "members": months}],
        filters={
            "year": str(year), "version": version, "entity": entity,
            "contact": "All_Contact", "tracking_1": "All_Tracking_1", "tracking_2": "All_Tracking_2",
            MEAS_DIM: RECURRING,
        },
        suppress=False,
    )
    out = {}
    for row in res["rows"]:
        acct = row["members"][-1]
        s = 0.0
        for c in row["cells"]:
            v = c.get("value")
            if isinstance(v, (int, float)):
                s += float(v)
        out[acct] = s
    return out


# Calendar month order (the cube is calendar-keyed: year=2026 carries Jan..Jun).
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _period(year):
    """(months, year_in_progress, months_elapsed, label) for the requested year.
    Past year -> full 12 (None => All_Month). Current year -> CLOSED calendar
    months only (before the in-progress current month)."""
    import datetime
    today = datetime.date.today()
    y = int(year)
    if y < today.year:
        return None, False, 12, "Full year"
    if y > today.year:
        return [], True, 0, "Not started"
    n = max(0, today.month - 1)  # closed months this calendar year
    closed = _MONTHS[:n]
    label = "YTD Jan–%s (%d mo)" % (_MONTHS[n - 1], n) if n else "YTD (0 closed mo)"
    return closed, True, n, label


def cost_cut_report(entity, year, groups=None):
    groups = groups or DEFAULT_GROUPS
    year = int(year)
    prior = year - 1

    group_of = {}
    accounts = []
    seen = set()
    for g in groups:                              # recurse each rollup to leaves; first group wins (no double-count)
        _leaves("account", g, g, group_of, accounts, seen)

    names = mq.element_names("account", accounts)  # {uuid: friendly name}

    # Like-for-like period: a complete past year is full-vs-full; an in-progress
    # year is YTD (closed months) vs the SAME closed months of the prior year —
    # never partial-vs-full (which produced the bogus -60.7%).
    period_months, year_in_progress, months_elapsed, period_label = _period(year)
    cur = _pull(entity, year, "actual", accounts, months=period_months)
    pri = _pull(entity, prior, "actual", accounts, months=period_months)
    behaviour = _behaviour_map()

    rows = []
    total_cur = 0.0
    for acct in accounts:
        a = cur.get(acct, 0.0)
        p = pri.get(acct, 0.0)
        if abs(a) < 0.005 and abs(p) < 0.005:
            continue
        total_cur += a
        nm = names.get(acct, acct)
        b = behaviour.get(_account_key(nm))
        if b:
            beh, drv, cut, addr = b["behaviour"], b["driver"], b["cuttability"], b["is_addressable"]
            mgmt = b.get("is_manageable", False)
        else:
            beh, drv, cut, addr, mgmt = "unclassified", "", "T2", True, False
        rows.append({
            "account_id": acct,
            "account_key": _account_key(nm),
            "name": nm,
            "group": group_of.get(acct, ""),
            "behaviour": beh,
            "driver": drv,
            "cuttability": cut,
            "is_addressable": addr,
            "is_manageable": mgmt,
            "recurring_actual": round(a, 2),
            "recurring_prior": round(p, 2),
            "yoy_delta": round(a - p, 2),
            "yoy_pct": round((a - p) / abs(p) * 100, 1) if abs(p) > 0.005 else None,
        })

    # % of the ADDRESSABLE base (not total) so a leaf's "% of cost" reconciles to
    # its group's "% of addressable" subtotal — same denominator as the headline.
    addressable_operating_cost = round(
        sum(r["recurring_actual"] for r in rows if r.get("is_addressable", True)), 2)
    pct_base = addressable_operating_cost if abs(addressable_operating_cost) > 0.005 else total_cur
    for r in rows:
        r["pct_of_cost"] = round(r["recurring_actual"] / pct_base * 100, 1) if abs(pct_base) > 0.005 else 0.0

    # Overlay user-set targets (Postgres) → RAG. Targets are goals, not plan/actuals.
    total_target, total_rag = _apply_targets(entity, year, total_cur, rows)

    # rank for cut: biggest recurring cost, then fastest YoY growth
    by_size = sorted(rows, key=lambda r: abs(r["recurring_actual"]), reverse=True)
    by_growth = [r for r in rows if r["yoy_delta"] > 0]
    by_growth.sort(key=lambda r: r["yoy_delta"], reverse=True)

    rag_counts = {"green": 0, "amber": 0, "red": 0, "none": 0}
    for r in rows:
        rag_counts[r.get("rag", "none")] += 1

    # Cost-behaviour split (CFO classification) — fixed/variable/semi/non-controllable.
    behaviour_totals = {"fixed": 0.0, "variable": 0.0, "semi_variable": 0.0,
                        "non_controllable": 0.0, "unclassified": 0.0}
    for r in rows:
        behaviour_totals[r.get("behaviour", "unclassified")] = round(
            behaviour_totals.get(r.get("behaviour", "unclassified"), 0.0) + r["recurring_actual"], 2)
    fixed_v = behaviour_totals["fixed"]
    var_v = behaviour_totals["variable"]
    addressable = round(fixed_v + var_v + behaviour_totals["semi_variable"], 2)

    # Cuttability (CFO addressable lens). Headline = addressable operating cost;
    # T0 / non-addressable (income tax, finance costs, statutory payroll
    # derivatives, contra/recovery) sits BELOW THE LINE — visible, not counted.
    addressable_rows = [r for r in rows if r.get("is_addressable", True)]
    below_rows = [r for r in rows if not r.get("is_addressable", True)]
    # addressable_operating_cost already computed above (pct base).
    below_the_line_total = round(sum(r["recurring_actual"] for r in below_rows), 2)
    tier_totals = {}
    for r in addressable_rows:
        t = r.get("cuttability", "T2")
        tier_totals[t] = round(tier_totals.get(t, 0.0) + r["recurring_actual"], 2)
    below_the_line = sorted(below_rows, key=lambda r: abs(r["recurring_actual"]), reverse=True)

    # Manageable cost — MC's top cost-cutting opportunities (editable hit-list).
    manageable_total = round(sum(r["recurring_actual"] for r in rows if r.get("is_manageable")), 2)

    # Seasonal run-rate (estimate) for an in-progress year: scale the YTD addressable
    # cost by how the PRIOR year's full year related to its same-period YTD — respects
    # seasonality instead of a naive x12/N. One extra total-level pull on the prior full year.
    annualised_estimate = None
    if year_in_progress and 0 < months_elapsed < 12:
        prior_full = _pull(entity, prior, "actual", accounts, months=None)
        prior_full_addr = round(sum(
            v for acct, v in prior_full.items()
            if behaviour.get(_account_key(names.get(acct, acct)), {}).get("is_addressable", True)), 2)
        prior_ytd_addr = round(sum(r["recurring_prior"] for r in addressable_rows), 2)
        if abs(prior_ytd_addr) > 0.005:
            annualised_estimate = round(addressable_operating_cost * (prior_full_addr / prior_ytd_addr), 2)

    comparison_basis = ("YTD vs prior-year YTD (same %d months)" % months_elapsed
                        if year_in_progress else "Full year vs prior year")
    return {
        "entity": entity, "year": year, "prior_year": prior,
        "basis": "Recurring cash expense (excludes non-cash & one-offs)",
        "year_in_progress": year_in_progress,
        "months_elapsed": months_elapsed,
        "period_label": period_label,
        "comparison_basis": comparison_basis,
        "annualised_estimate": annualised_estimate,
        "total_recurring_cost": round(total_cur, 2),
        "total_target": total_target,
        "total_rag": total_rag,
        "rag_counts": rag_counts,
        "behaviour_totals": behaviour_totals,
        "addressable_base": addressable,
        "fixed_variable_ratio": round(fixed_v / var_v, 2) if abs(var_v) > 0.005 else None,
        "addressable_operating_cost": addressable_operating_cost,
        "below_the_line": below_the_line,
        "below_the_line_total": below_the_line_total,
        "tier_totals": tier_totals,
        "manageable_total": manageable_total,
        "accounts": by_size,
        "top_opportunities": by_growth[:10],
        "source": "TM1 gl_src_trial_balance (live)",
    }


def _apply_targets(entity, year, total_actual, rows):
    """Attach 'target' + 'rag' to each account row and return the total target.
    Targets live in Postgres (apps.planning_analytics.models.KPITarget)."""
    from apps.planning_analytics.models import KPITarget
    qs = KPITarget.objects.filter(period_year=year, entity_id__in=[entity, ""])
    # entity-specific wins over org-wide ("") for the same metric_key
    by_metric = {}
    for t in qs:
        cur = by_metric.get(t.metric_key)
        if cur is None or (cur.entity_id == "" and t.entity_id == entity):
            by_metric[t.metric_key] = t
    for r in rows:
        t = by_metric.get("cost_cut.account.%s" % r["account_id"])
        if t:
            r["target"] = float(t.target_value)
            r["rag"] = t.rag(r["recurring_actual"])
        else:
            r["target"] = None
            r["rag"] = "none"
    total_t = by_metric.get("cost_cut.total")
    if not total_t:
        return None, "none"
    return float(total_t.target_value), total_t.rag(total_actual)
