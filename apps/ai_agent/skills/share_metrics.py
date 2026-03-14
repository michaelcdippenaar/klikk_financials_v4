"""
Skill: Share metrics — dividend per share, payout ratio, EPS, yield, etc.

Formulas:
- DPS (dividend per share) = total_dividends / shares_outstanding
- EPS (earnings per share) = net_income / shares_outstanding
- Payout ratio = total_dividends / net_income = DPS / EPS
- Dividend yield = DPS / share_price (annualised if DPS is annual)
- Retention ratio = 1 - payout_ratio
"""
from __future__ import annotations

import os
import sys
from typing import Any




def share_metrics_calculate(
    total_dividends: float | None = None,
    dividend_per_share: float | None = None,
    net_income: float | None = None,
    earnings_per_share: float | None = None,
    shares_outstanding: float | None = None,
    share_price: float | None = None,
) -> dict[str, Any]:
    """
    Calculate share metrics: DPS, EPS, payout ratio, dividend yield, retention ratio.

    Provide either:
    - total_dividends + shares_outstanding (to get DPS), and/or net_income (for EPS, payout ratio)
    - Or dividend_per_share and/or earnings_per_share directly.

    share_price is optional; if given, dividend_yield = DPS / share_price.
    All monetary values in same units (e.g. millions or dollars).
    """
    # Resolve DPS
    dps = None
    if dividend_per_share is not None:
        try:
            dps = float(dividend_per_share)
        except (TypeError, ValueError):
            dps = None
    if dps is None and total_dividends is not None and shares_outstanding is not None:
        try:
            tot = float(total_dividends)
            sh = float(shares_outstanding)
            if sh <= 0:
                return {"error": "shares_outstanding must be positive."}
            dps = tot / sh
        except (TypeError, ValueError):
            pass

    # Resolve EPS
    eps = None
    if earnings_per_share is not None:
        try:
            eps = float(earnings_per_share)
        except (TypeError, ValueError):
            eps = None
    if eps is None and net_income is not None and shares_outstanding is not None:
        try:
            ni = float(net_income)
            sh = float(shares_outstanding)
            if sh <= 0:
                return {"error": "shares_outstanding must be positive."}
            eps = ni / sh
        except (TypeError, ValueError):
            pass

    result = {"inputs_used": {}}

    if dps is not None:
        result["dividend_per_share"] = round(dps, 4)
        result["inputs_used"]["dividend_per_share"] = dps
    if eps is not None:
        result["earnings_per_share"] = round(eps, 4)
        result["inputs_used"]["earnings_per_share"] = eps

    # Payout ratio: dividends / earnings = DPS / EPS
    if dps is not None and eps is not None and abs(eps) > 1e-20:
        payout_ratio = dps / eps
        result["payout_ratio"] = round(payout_ratio, 4)
        result["payout_ratio_pct"] = round(payout_ratio * 100, 2)
        result["retention_ratio"] = round(1 - payout_ratio, 4)
        result["retention_ratio_pct"] = round((1 - payout_ratio) * 100, 2)
    elif total_dividends is not None and net_income is not None and abs(float(net_income)) > 1e-20:
        payout_ratio = float(total_dividends) / float(net_income)
        result["payout_ratio"] = round(payout_ratio, 4)
        result["payout_ratio_pct"] = round(payout_ratio * 100, 2)
        result["retention_ratio"] = round(1 - payout_ratio, 4)
        result["retention_ratio_pct"] = round((1 - payout_ratio) * 100, 2)

    # Dividend yield = DPS / share_price
    if dps is not None and share_price is not None:
        try:
            price = float(share_price)
            if price > 1e-20:
                result["dividend_yield"] = round(dps / price, 4)
                result["dividend_yield_pct"] = round((dps / price) * 100, 2)
            else:
                result["error"] = "share_price must be positive for dividend yield."
        except (TypeError, ValueError):
            result["error"] = "Invalid share_price."

    if len(result) <= 1 or (len(result) == 2 and "inputs_used" in result):
        result["error"] = (
            "Provide at least: (total_dividends + shares_outstanding) or dividend_per_share; "
            "and for payout ratio: (net_income + shares_outstanding) or earnings_per_share."
        )
    return result


def share_metrics_multi_period(
    periods: list[str],
    total_dividends_per_period: list[float] | None = None,
    dividend_per_share_per_period: list[float] | None = None,
    net_income_per_period: list[float] | None = None,
    earnings_per_share_per_period: list[float] | None = None,
    shares_outstanding: float | None = None,
) -> dict[str, Any]:
    """
    Calculate DPS, EPS, payout ratio for each period when you have time series of dividends and earnings.
    periods: list of labels (e.g. ['2023', '2024', '2025']).
    shares_outstanding can be a single number (used for all periods) or list of same length as periods.
    """
    n = len(periods)
    if n == 0:
        return {"error": "periods must be a non-empty list."}

    # Resolve shares per period
    if shares_outstanding is not None:
        try:
            sh = shares_outstanding
            if isinstance(sh, (list, tuple)):
                sh_list = [float(s) for s in sh]
                if len(sh_list) != n:
                    return {"error": "shares_outstanding list length must match periods."}
            else:
                sh_list = [float(sh)] * n
        except (TypeError, ValueError):
            return {"error": "Invalid shares_outstanding."}
    else:
        sh_list = None

    # Build DPS and EPS per period
    dps_list = []
    eps_list = []
    if dividend_per_share_per_period is not None:
        dps_list = [float(x) for x in dividend_per_share_per_period[:n]]
    elif total_dividends_per_period is not None and sh_list:
        tot_list = [float(x) for x in total_dividends_per_period[:n]]
        dps_list = [tot_list[i] / sh_list[i] if sh_list[i] > 0 else 0 for i in range(min(len(tot_list), n))]
    if len(dps_list) < n:
        dps_list.extend([None] * (n - len(dps_list)))

    if earnings_per_share_per_period is not None:
        eps_list = [float(x) for x in earnings_per_share_per_period[:n]]
    elif net_income_per_period is not None and sh_list:
        ni_list = [float(x) for x in net_income_per_period[:n]]
        eps_list = [ni_list[i] / sh_list[i] if sh_list[i] > 0 else 0 for i in range(min(len(ni_list), n))]
    if len(eps_list) < n:
        eps_list.extend([None] * (n - len(eps_list)))

    rows = []
    for i in range(n):
        row = {"period": periods[i]}
        if i < len(dps_list) and dps_list[i] is not None:
            row["dividend_per_share"] = round(dps_list[i], 4)
        if i < len(eps_list) and eps_list[i] is not None:
            row["earnings_per_share"] = round(eps_list[i], 4)
        if row.get("dividend_per_share") is not None and row.get("earnings_per_share") is not None:
            eps_val = row["earnings_per_share"]
            if abs(eps_val) > 1e-20:
                row["payout_ratio"] = round(row["dividend_per_share"] / eps_val, 4)
                row["payout_ratio_pct"] = round(row["payout_ratio"] * 100, 2)
        rows.append(row)

    return {"periods": periods, "metrics": rows}


# ---------------------------------------------------------------------------
#  Tool schemas
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "name": "share_metrics_calculate",
        "description": (
            "Calculate share metrics: dividend per share (DPS), earnings per share (EPS), "
            "payout ratio (dividends/earnings), retention ratio, dividend yield (if share_price given). "
            "Provide total_dividends + shares_outstanding and/or dividend_per_share; "
            "net_income + shares_outstanding and/or earnings_per_share. Optionally share_price for yield."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "total_dividends": {"type": "number", "description": "Total dividends paid (same units as net_income)."},
                "dividend_per_share": {"type": "number", "description": "DPS if already known."},
                "net_income": {"type": "number", "description": "Net income / earnings."},
                "earnings_per_share": {"type": "number", "description": "EPS if already known."},
                "shares_outstanding": {"type": "number", "description": "Number of shares."},
                "share_price": {"type": "number", "description": "Current share price (for dividend yield)."},
            },
        },
    },
    {
        "name": "share_metrics_multi_period",
        "description": (
            "Calculate DPS, EPS, payout ratio for each period given time series of dividends and earnings. "
            "Input: periods (labels), total_dividends_per_period or dividend_per_share_per_period, "
            "net_income_per_period or earnings_per_share_per_period, and shares_outstanding (single or list)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "periods": {"type": "array", "items": {"type": "string"}, "description": "Period labels e.g. ['2023','2024']."},
                "total_dividends_per_period": {"type": "array", "items": {"type": "number"}},
                "dividend_per_share_per_period": {"type": "array", "items": {"type": "number"}},
                "net_income_per_period": {"type": "array", "items": {"type": "number"}},
                "earnings_per_share_per_period": {"type": "array", "items": {"type": "number"}},
                "shares_outstanding": {"type": "number", "description": "Single value or list per period."},
            },
            "required": ["periods"],
        },
    },
]

TOOL_FUNCTIONS = {
    "share_metrics_calculate": share_metrics_calculate,
    "share_metrics_multi_period": share_metrics_multi_period,
}
