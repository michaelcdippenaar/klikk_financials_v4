"""
Skill: Statistics — pattern detection and forecasting from historical series.

Works on time series: list of (period, value) or (x, value).
- Trend: linear regression slope, intercept, R².
- Patterns: growth rate, volatility, trend direction.
- Forecast: next N periods from linear trend or simple average.
"""
from __future__ import annotations

import os
import sys
from typing import Any

import numpy as np




def _parse_series(values: list[float] | None = None, periods: list[str] | None = None,
                  data_points: list[dict] | None = None) -> tuple[list[float], list[float], list[str]]:
    """Return (x_numeric, y_values, period_labels). x is 0-based index if no numeric x given."""
    if data_points:
        periods_out = []
        y_out = []
        for i, pt in enumerate(data_points):
            if isinstance(pt, dict):
                p = pt.get("period") or pt.get("date") or pt.get("x") or str(i)
                v = pt.get("value") or pt.get("y") or pt.get("amount")
            else:
                p, v = str(i), (pt[1] if len(pt) >= 2 else pt)
            periods_out.append(str(p))
            try:
                y_out.append(float(v))
            except (TypeError, ValueError):
                y_out.append(0.0)
        x_out = list(range(len(y_out)))
        return x_out, y_out, periods_out

    if values is not None:
        y_out = [float(v) for v in values]
        n = len(y_out)
        x_out = list(range(n))
        periods_out = periods if periods and len(periods) >= n else [str(i) for i in range(n)]
        return x_out, y_out, periods_out

    return [], [], []


def statistics_pattern_trend(
    values: list[float] | None = None,
    periods: list[str] | None = None,
    data_points: list[dict] | None = None,
) -> dict[str, Any]:
    """
    Detect trend and patterns in a time series.
    Provide either: values (and optionally periods), or data_points as list of {period, value}.

    Returns: linear trend (slope, intercept, R²), trend_direction (up/down/flat),
    growth_rate_pct (first to last), volatility (std), mean.
    """
    x, y, period_labels = _parse_series(values=values, periods=periods, data_points=data_points)
    if len(y) < 2:
        return {"error": "Need at least 2 data points for trend analysis."}

    x_arr = np.array(x, dtype=float)
    y_arr = np.array(y, dtype=float)
    n = len(y_arr)

    # Linear regression: y = slope * x + intercept
    x_mean = np.mean(x_arr)
    y_mean = np.mean(y_arr)
    ss_xy = np.sum((x_arr - x_mean) * (y_arr - y_mean))
    ss_xx = np.sum((x_arr - x_mean) ** 2)
    if abs(ss_xx) < 1e-20:
        slope = 0.0
    else:
        slope = ss_xy / ss_xx
    intercept = y_mean - slope * x_mean

    # R²
    y_pred = slope * x_arr + intercept
    ss_res = np.sum((y_arr - y_pred) ** 2)
    ss_tot = np.sum((y_arr - y_mean) ** 2)
    r_squared = (1 - ss_res / ss_tot) if ss_tot > 1e-20 else 0.0

    # Growth rate (first to last)
    first_val = float(y_arr[0])
    last_val = float(y_arr[-1])
    if abs(first_val) > 1e-20:
        growth_rate_pct = ((last_val - first_val) / abs(first_val)) * 100
    else:
        growth_rate_pct = (100.0 if last_val > 0 else 0.0) if last_val != first_val else 0.0

    # Volatility (sample std)
    vol = float(np.std(y_arr)) if n > 1 else 0.0

    # Trend direction
    if abs(slope) < 1e-10 * (np.max(y_arr) - np.min(y_arr) + 1):
        trend_direction = "flat"
    else:
        trend_direction = "up" if slope > 0 else "down"

    return {
        "n_points": n,
        "periods": period_labels,
        "trend": {
            "slope": round(float(slope), 6),
            "intercept": round(float(intercept), 4),
            "r_squared": round(r_squared, 4),
        },
        "trend_direction": trend_direction,
        "growth_rate_pct": round(growth_rate_pct, 2),
        "mean": round(float(y_mean), 4),
        "volatility_std": round(vol, 4),
        "first_value": round(first_val, 4),
        "last_value": round(last_val, 4),
    }


def statistics_forecast(
    values: list[float] | None = None,
    periods: list[str] | None = None,
    data_points: list[dict] | None = None,
    periods_ahead: int = 3,
    method: str = "trend",
) -> dict[str, Any]:
    """
    Forecast next periods from historical series.
    method: 'trend' = linear extrapolation; 'average' = use mean of history.
    Provide either values (and optionally periods) or data_points as list of {period, value}.
    """
    x, y, period_labels = _parse_series(values=values, periods=periods, data_points=data_points)
    if not y:
        return {"error": "No data points provided."}

    periods_ahead = max(1, min(int(periods_ahead), 24))
    method = (method or "trend").lower()

    x_arr = np.array(x, dtype=float)
    y_arr = np.array(y, dtype=float)
    n = len(y_arr)

    forecasts = []
    if method == "average":
        mean_val = float(np.mean(y_arr))
        for i in range(1, periods_ahead + 1):
            forecasts.append({"period_index": n + i, "forecast_value": round(mean_val, 4), "method": "average"})
    else:
        # Linear trend
        x_mean = np.mean(x_arr)
        y_mean = np.mean(y_arr)
        ss_xy = np.sum((x_arr - x_mean) * (y_arr - y_mean))
        ss_xx = np.sum((x_arr - x_mean) ** 2)
        slope = (ss_xy / ss_xx) if abs(ss_xx) > 1e-20 else 0.0
        intercept = y_mean - slope * x_mean
        for i in range(1, periods_ahead + 1):
            x_next = n + i - 1  # continue x sequence
            val = slope * x_next + intercept
            forecasts.append({"period_index": x_next + 1, "forecast_value": round(float(val), 4), "method": "trend"})

    return {
        "n_history": n,
        "periods_ahead": periods_ahead,
        "method": method,
        "forecasts": forecasts,
        "note": "period_index is 1-based next position; assign your own period labels (e.g. next month names) when presenting.",
    }


def statistics_detect_patterns(
    values: list[float] | None = None,
    periods: list[str] | None = None,
    data_points: list[dict] | None = None,
) -> dict[str, Any]:
    """
    Summarise patterns in a time series: trend direction, volatility, growth,
    and a simple seasonality hint (compare first half vs second half mean).
    """
    x, y, period_labels = _parse_series(values=values, periods=periods, data_points=data_points)
    if len(y) < 2:
        return {"error": "Need at least 2 data points."}

    y_arr = np.array(y, dtype=float)
    n = len(y_arr)
    mean_all = float(np.mean(y_arr))
    std_all = float(np.std(y_arr)) if n > 1 else 0.0
    first_val = float(y_arr[0])
    last_val = float(y_arr[-1])
    growth_pct = ((last_val - first_val) / abs(first_val) * 100) if abs(first_val) > 1e-20 else 0.0

    # Trend
    x_arr = np.array(x, dtype=float)
    x_mean = np.mean(x_arr)
    y_mean = np.mean(y_arr)
    ss_xy = np.sum((x_arr - x_mean) * (y_arr - y_mean))
    ss_xx = np.sum((x_arr - x_mean) ** 2)
    slope = (ss_xy / ss_xx) if abs(ss_xx) > 1e-20 else 0.0
    direction = "up" if slope > 0 else ("down" if slope < 0 else "flat")

    # First half vs second half (simple seasonality hint)
    mid = n // 2
    mean_first = float(np.mean(y_arr[:mid])) if mid > 0 else mean_all
    mean_second = float(np.mean(y_arr[mid:])) if mid < n else mean_all
    half_shift_pct = ((mean_second - mean_first) / abs(mean_first) * 100) if abs(mean_first) > 1e-20 else 0.0

    return {
        "n_points": n,
        "trend_direction": direction,
        "mean": round(mean_all, 4),
        "volatility_std": round(std_all, 4),
        "growth_first_to_last_pct": round(growth_pct, 2),
        "first_half_mean": round(mean_first, 4),
        "second_half_mean": round(mean_second, 4),
        "first_vs_second_half_shift_pct": round(half_shift_pct, 2),
        "min": round(float(np.min(y_arr)), 4),
        "max": round(float(np.max(y_arr)), 4),
    }


# ---------------------------------------------------------------------------
#  Tool schemas
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "name": "statistics_pattern_trend",
        "description": (
            "Detect trend and patterns in a time series. Input: list of values and optional period labels, "
            "or data_points as list of {period, value}. Returns linear trend (slope, intercept, R²), "
            "trend_direction (up/down/flat), growth_rate_pct, volatility (std), mean."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "values": {"type": "array", "items": {"type": "number"}, "description": "Time series values in order."},
                "periods": {"type": "array", "items": {"type": "string"}, "description": "Optional period labels (e.g. months)."},
                "data_points": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Alternative: list of {period, value} or {date, value}.",
                },
            },
        },
    },
    {
        "name": "statistics_forecast",
        "description": (
            "Forecast next periods from historical series. method: 'trend' (linear extrapolation) or 'average'. "
            "Input: values + optional periods, or data_points. Returns forecast_value for each period ahead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "values": {"type": "array", "items": {"type": "number"}},
                "periods": {"type": "array", "items": {"type": "string"}},
                "data_points": {"type": "array", "items": {"type": "object"}},
                "periods_ahead": {"type": "integer", "description": "Number of periods to forecast (default 3, max 24)."},
                "method": {"type": "string", "description": "'trend' or 'average' (default 'trend')."},
            },
        },
    },
    {
        "name": "statistics_detect_patterns",
        "description": (
            "Summarise patterns: trend direction, volatility, growth first-to-last, "
            "first half vs second half mean (simple seasonality hint). Input: values and optional periods or data_points."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "values": {"type": "array", "items": {"type": "number"}},
                "periods": {"type": "array", "items": {"type": "string"}},
                "data_points": {"type": "array", "items": {"type": "object"}},
            },
        },
    },
]

TOOL_FUNCTIONS = {
    "statistics_pattern_trend": statistics_pattern_trend,
    "statistics_forecast": statistics_forecast,
    "statistics_detect_patterns": statistics_detect_patterns,
}
