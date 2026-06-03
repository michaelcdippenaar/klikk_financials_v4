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


def _pull(entity, year, version, accounts):
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

    cur = _pull(entity, year, "actual", accounts)
    pri = _pull(entity, prior, "actual", accounts)

    rows = []
    total_cur = 0.0
    for acct in accounts:
        a = cur.get(acct, 0.0)
        p = pri.get(acct, 0.0)
        if abs(a) < 0.005 and abs(p) < 0.005:
            continue
        total_cur += a
        rows.append({
            "account_id": acct,
            "name": names.get(acct, acct),
            "group": group_of.get(acct, ""),
            "recurring_actual": round(a, 2),
            "recurring_prior": round(p, 2),
            "yoy_delta": round(a - p, 2),
            "yoy_pct": round((a - p) / abs(p) * 100, 1) if abs(p) > 0.005 else None,
        })

    for r in rows:
        r["pct_of_cost"] = round(r["recurring_actual"] / total_cur * 100, 1) if abs(total_cur) > 0.005 else 0.0

    # Overlay user-set targets (Postgres) → RAG. Targets are goals, not plan/actuals.
    total_target, total_rag = _apply_targets(entity, year, total_cur, rows)

    # rank for cut: biggest recurring cost, then fastest YoY growth
    by_size = sorted(rows, key=lambda r: abs(r["recurring_actual"]), reverse=True)
    by_growth = [r for r in rows if r["yoy_delta"] > 0]
    by_growth.sort(key=lambda r: r["yoy_delta"], reverse=True)

    rag_counts = {"green": 0, "amber": 0, "red": 0, "none": 0}
    for r in rows:
        rag_counts[r.get("rag", "none")] += 1

    return {
        "entity": entity, "year": year, "prior_year": prior,
        "basis": "Recurring cash expense (excludes non-cash & one-offs)",
        "total_recurring_cost": round(total_cur, 2),
        "total_target": total_target,
        "total_rag": total_rag,
        "rag_counts": rag_counts,
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
