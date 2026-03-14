"""
Skill: Pattern Analysis & Anomaly Detection
Detect variances, outliers, and trends in GL and cashflow data via TM1py.
"""
from __future__ import annotations

import sys
import os
from typing import Any

from apps.ai_agent.agent.config import TM1_CONFIG
from TM1py import TM1Service

MONTH_ORDER = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def analyse_gl_variance(
    year: str,
    month: str,
    entity: str = "All_Entity",
    version_actual: str = "actual",
    version_budget: str = "budget",
    top_n: int = 20,
) -> dict[str, Any]:
    """
    Compare actual vs budget for GL accounts in a given period.
    Returns the top_n accounts by absolute variance.

    year: e.g. '2025'
    month: e.g. 'Jul'
    entity: Entity element name, default 'All_Entity'
    version_actual: Actual version name, default 'actual'
    version_budget: Budget version name, default 'budget'
    top_n: Number of top variances to return (default 20)
    """
    mdx_template = """
    SELECT
      {{[measure_gl_src_trial_balance].[amount]}} ON 0,
      {{[account].[All_Account].Children}} ON 1
    FROM [gl_src_trial_balance]
    WHERE (
      [year].[{year}], [month].[{month}],
      [version].[{version}], [entity].[{entity}],
      [contact].[All_Contact],
      [tracking_1].[All_Tracking_1],
      [tracking_2].[All_Tracking_2]
    )
    """
    try:
        with TM1Service(**TM1_CONFIG) as tm1:
            actual_cells = tm1.cells.execute_mdx(
                mdx_template.format(year=year, month=month,
                                    version=version_actual, entity=entity)
            )
            budget_cells = tm1.cells.execute_mdx(
                mdx_template.format(year=year, month=month,
                                    version=version_budget, entity=entity)
            )

        actual = {list(k)[1]: v for k, v in actual_cells.items() if v}
        budget = {list(k)[1]: v for k, v in budget_cells.items() if v}

        all_accounts = set(actual) | set(budget)
        variances = []
        for acc in all_accounts:
            a = actual.get(acc, 0) or 0
            b = budget.get(acc, 0) or 0
            var = a - b
            pct = (var / b * 100) if b else None
            variances.append({
                "account": acc,
                "actual": round(a, 2),
                "budget": round(b, 2),
                "variance": round(var, 2),
                "variance_pct": round(pct, 1) if pct is not None else None,
            })

        variances.sort(key=lambda x: abs(x["variance"]), reverse=True)
        return {
            "year": year,
            "month": month,
            "entity": entity,
            "top_variances": variances[:top_n],
            "total_accounts_with_data": len(variances),
        }
    except Exception as e:
        return {"error": str(e)}


def detect_gl_anomalies(
    year: str,
    month: str,
    entity: str = "All_Entity",
    std_threshold: float = 2.0,
) -> dict[str, Any]:
    """
    Detect GL accounts whose amount in the given period is an outlier
    compared to the prior 12 months (beyond std_threshold standard deviations).

    year: e.g. '2025'
    month: e.g. 'Jul'
    entity: Entity element, default 'All_Entity'
    std_threshold: Z-score threshold for anomaly detection (default 2.0)
    """
    try:
        with TM1Service(**TM1_CONFIG) as tm1:
            # Get 13 months of data: 12 prior + current
            month_idx = MONTH_ORDER.index(month)
            periods: list[tuple[str, str]] = []
            for i in range(12, -1, -1):
                mi = (month_idx - i) % 12
                yr_offset = (month_idx - i) // 12
                yr = str(int(year) - yr_offset)
                periods.append((yr, MONTH_ORDER[mi]))

            account_history: dict[str, list[float]] = {}
            for yr, mo in periods:
                mdx = f"""
                SELECT {{[measure_gl_src_trial_balance].[amount]}} ON 0,
                       {{[account].[All_Account].Children}} ON 1
                FROM [gl_src_trial_balance]
                WHERE ([year].[{yr}],[month].[{mo}],[version].[actual],
                       [entity].[{entity}],[contact].[All_Contact],
                       [tracking_1].[All_Tracking_1],[tracking_2].[All_Tracking_2])
                """
                cells = tm1.cells.execute_mdx(mdx)
                for coords, val in cells.items():
                    acc = list(coords)[1]
                    account_history.setdefault(acc, []).append(val or 0)

        anomalies = []
        for acc, history in account_history.items():
            if len(history) < 3:
                continue
            prior = history[:-1]
            current = history[-1]
            mean = sum(prior) / len(prior)
            variance = sum((x - mean) ** 2 for x in prior) / len(prior)
            std = variance ** 0.5
            if std < 0.01:
                continue
            z = (current - mean) / std
            if abs(z) >= std_threshold:
                anomalies.append({
                    "account": acc,
                    "current_value": round(current, 2),
                    "prior_mean": round(mean, 2),
                    "prior_std": round(std, 2),
                    "z_score": round(z, 2),
                    "direction": "above" if z > 0 else "below",
                })

        anomalies.sort(key=lambda x: abs(x["z_score"]), reverse=True)
        return {
            "year": year,
            "month": month,
            "entity": entity,
            "std_threshold": std_threshold,
            "anomalies": anomalies,
            "anomaly_count": len(anomalies),
        }
    except Exception as e:
        return {"error": str(e)}


def compare_periods(
    entity: str,
    year_a: str,
    month_a: str,
    year_b: str,
    month_b: str,
    version: str = "actual",
) -> dict[str, Any]:
    """
    Compare GL amounts between two periods for an entity.
    Returns account-level delta sorted by absolute change.

    entity: Entity element, e.g. 'All_Entity'
    year_a / month_a: First period, e.g. '2025', 'Jul'
    year_b / month_b: Second (comparison) period
    version: TM1 version element (default 'actual')
    """
    mdx_template = """
    SELECT {{[measure_gl_src_trial_balance].[amount]}} ON 0,
           {{[account].[All_Account].Children}} ON 1
    FROM [gl_src_trial_balance]
    WHERE ([year].[{year}],[month].[{month}],[version].[{version}],
           [entity].[{entity}],[contact].[All_Contact],
           [tracking_1].[All_Tracking_1],[tracking_2].[All_Tracking_2])
    """
    try:
        with TM1Service(**TM1_CONFIG) as tm1:
            cells_a = tm1.cells.execute_mdx(
                mdx_template.format(year=year_a, month=month_a,
                                    version=version, entity=entity)
            )
            cells_b = tm1.cells.execute_mdx(
                mdx_template.format(year=year_b, month=month_b,
                                    version=version, entity=entity)
            )

        vals_a = {list(k)[1]: v or 0 for k, v in cells_a.items()}
        vals_b = {list(k)[1]: v or 0 for k, v in cells_b.items()}
        all_accs = set(vals_a) | set(vals_b)

        deltas = []
        for acc in all_accs:
            a = vals_a.get(acc, 0)
            b = vals_b.get(acc, 0)
            delta = a - b
            if abs(delta) > 0.01:
                deltas.append({
                    "account": acc,
                    f"{year_a}_{month_a}": round(a, 2),
                    f"{year_b}_{month_b}": round(b, 2),
                    "delta": round(delta, 2),
                })

        deltas.sort(key=lambda x: abs(x["delta"]), reverse=True)
        return {
            "period_a": f"{year_a} {month_a}",
            "period_b": f"{year_b} {month_b}",
            "entity": entity,
            "version": version,
            "deltas": deltas[:50],
            "total_accounts_with_change": len(deltas),
        }
    except Exception as e:
        return {"error": str(e)}


def find_unmapped_cashflow_accounts() -> dict[str, Any]:
    """
    Find GL accounts that have amounts in gl_src_trial_balance but are not
    mapped in cashflow_cnt_mapping (cf_activity is empty or 'unmapped_cashflow_activity').
    """
    try:
        with TM1Service(**TM1_CONFIG) as tm1:
            # Get all account-cf_activity mappings
            mapping_mdx = """
            SELECT {[measure_cashflow_cnt_mapping].[cf_activity]} ON 0,
                   {[account].[All_Account].Children} ON 1
            FROM [cashflow_cnt_mapping]
            """
            mapping_cells = tm1.cells.execute_mdx(mapping_mdx)

        mapped = {}
        for coords, val in mapping_cells.items():
            acc = list(coords)[0]
            mapped[acc] = val or ""

        unmapped = [
            acc for acc, cf in mapped.items()
            if not cf or cf in ("", "unmapped_cashflow_activity")
        ]
        return {
            "unmapped_accounts": sorted(unmapped),
            "unmapped_count": len(unmapped),
            "total_accounts_checked": len(mapped),
        }
    except Exception as e:
        return {"error": str(e)}


# --- Tool schemas ---

TOOL_SCHEMAS = [
    {
        "name": "analyse_gl_variance",
        "description": "Compare actual vs budget for GL accounts in a period. Returns top accounts by absolute variance.",
        "input_schema": {
            "type": "object",
            "properties": {
                "year": {"type": "string", "description": "Calendar year, e.g. '2025'"},
                "month": {"type": "string", "description": "Month name, e.g. 'Jul'"},
                "entity": {"type": "string", "description": "Entity element (default 'All_Entity')"},
                "version_actual": {"type": "string", "description": "Actual version name (default 'actual')"},
                "version_budget": {"type": "string", "description": "Budget version name (default 'budget')"},
                "top_n": {"type": "integer", "description": "Number of top variances to return (default 20)"},
            },
            "required": ["year", "month"],
        },
    },
    {
        "name": "detect_gl_anomalies",
        "description": "Detect GL accounts whose current-period amount is statistically anomalous vs the prior 12 months.",
        "input_schema": {
            "type": "object",
            "properties": {
                "year": {"type": "string", "description": "e.g. '2025'"},
                "month": {"type": "string", "description": "e.g. 'Jul'"},
                "entity": {"type": "string", "description": "Entity element (default 'All_Entity')"},
                "std_threshold": {"type": "number", "description": "Z-score threshold (default 2.0)"},
            },
            "required": ["year", "month"],
        },
    },
    {
        "name": "compare_periods",
        "description": "Compare GL amounts between two periods for an entity. Returns account-level deltas.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": "Entity element"},
                "year_a": {"type": "string", "description": "First period year"},
                "month_a": {"type": "string", "description": "First period month"},
                "year_b": {"type": "string", "description": "Second period year"},
                "month_b": {"type": "string", "description": "Second period month"},
                "version": {"type": "string", "description": "Version element (default 'actual')"},
            },
            "required": ["entity", "year_a", "month_a", "year_b", "month_b"],
        },
    },
    {
        "name": "find_unmapped_cashflow_accounts",
        "description": "Find GL accounts not mapped in cashflow_cnt_mapping (missing cashflow routing).",
        "input_schema": {"type": "object", "properties": {}},
    },
]

TOOL_FUNCTIONS = {
    "analyse_gl_variance": analyse_gl_variance,
    "detect_gl_anomalies": detect_gl_anomalies,
    "compare_periods": compare_periods,
    "find_unmapped_cashflow_accounts": find_unmapped_cashflow_accounts,
}
