"""
Skill: Report Builder — pre-built financial reports rendered as dashboard widgets.

Reports pull data from PostgreSQL (Investec exports, yfinance market data) and/or TM1,
compute derived metrics, and return one or more widgets (DataGrid, KPICard, LineChart, etc.)
that render in the user's browser.

Available reports:
- dividend_report: Google Finance-style dividend summary (history, yield, growth, annual totals)
- holdings_report: Portfolio holdings with P&L, allocation, and performance
- share_performance: Price performance with charts over a configurable period
- transaction_summary: Buy/sell/dividend activity summary by share
- portfolio_overview: Full portfolio dashboard (KPI cards + allocation chart + holdings grid)
"""
from __future__ import annotations

import os
import sys
import uuid
import datetime
import decimal
from typing import Any
from collections import defaultdict

import psycopg2
import psycopg2.extras

from apps.ai_agent.agent.config import settings

_FINANCIALS_DSN = dict(
    host=settings.pg_financials_host,
    port=settings.pg_financials_port,
    dbname=settings.pg_financials_db,
    user=settings.pg_financials_user,
    password=settings.pg_financials_password,
)


def _serialize(v: Any) -> Any:
    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    return v


def _ser_row(row: dict) -> dict:
    return {k: _serialize(v) for k, v in row.items()}


def _widget(widget_type: str, title: str, props: dict,
            width: int = 3, height: str = "md", data: dict | None = None) -> dict:
    cfg = {
        "id": f"w_{uuid.uuid4().hex[:8]}",
        "type": widget_type,
        "title": title,
        "width": width,
        "height": height,
        "props": props,
    }
    if data:
        cfg["data"] = data
    return cfg


def _find_share(cur, search: str) -> dict | None:
    """Fuzzy match a share by symbol, name, or Investec mapping."""
    cur.execute("""
        SELECT s.id, s.symbol, s.name, s.exchange, s.category,
               m.share_name, m.share_name2, m.share_name3,
               m.share_code, m.company
        FROM financial_investments_symbol s
        LEFT JOIN investec_investecjsesharenamemapping m
            ON m.id = s.share_name_mapping_id
        WHERE LOWER(s.symbol) LIKE LOWER(%(q)s)
           OR LOWER(s.name) LIKE LOWER(%(q)s)
           OR LOWER(m.share_name) LIKE LOWER(%(q)s)
           OR LOWER(m.share_name2) LIKE LOWER(%(q)s)
           OR LOWER(m.share_name3) LIKE LOWER(%(q)s)
           OR LOWER(m.share_code) LIKE LOWER(%(q)s)
           OR LOWER(m.company) LIKE LOWER(%(q)s)
        LIMIT 1
    """, {"q": f"%{search}%"})
    row = cur.fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
#  Report: Dividend Report (Google Finance style)
# ---------------------------------------------------------------------------

def build_dividend_report(symbol_search: str, years: int = 5) -> dict[str, Any]:
    """
    Build a Google Finance-style dividend report for a share.
    Shows: dividend history, annual totals, yield, frequency, growth rate.
    Returns multiple widgets: KPI cards + history table + annual chart.

    symbol_search: Share name or symbol (e.g. 'Absa', 'NED.JO', 'Capitec')
    years: How many years of history to include (default 5)
    """
    try:
        with psycopg2.connect(**_FINANCIALS_DSN) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                share = _find_share(cur, symbol_search)
                if not share:
                    return {"error": f"No share found matching '{symbol_search}'"}

                symbol_id = share["id"]
                share_name = share.get("share_name")
                share_code = share.get("share_code")
                display_name = share.get("company") or share.get("name") or share.get("symbol")

                # --- Dividend history from yfinance ---
                cutoff = datetime.date.today() - datetime.timedelta(days=years * 365)
                cur.execute("""
                    SELECT date, amount, currency
                    FROM financial_investments_dividend
                    WHERE symbol_id = %(sid)s AND date >= %(cutoff)s
                    ORDER BY date DESC
                """, {"sid": symbol_id, "cutoff": cutoff})
                dividends = [_ser_row(dict(r)) for r in cur.fetchall()]

                # --- Investec transaction dividends (actual received) ---
                investec_divs = []
                if share_name:
                    cur.execute("""
                        SELECT date, type, value, value_per_share, quantity
                        FROM investec_investecjsetransaction
                        WHERE share_name = %(sn)s
                          AND type IN ('Dividend', 'Special Dividend', 'Foreign Dividend')
                          AND date >= %(cutoff)s
                        ORDER BY date DESC
                    """, {"sn": share_name, "cutoff": cutoff})
                    investec_divs = [_ser_row(dict(r)) for r in cur.fetchall()]

                # --- Latest price ---
                cur.execute("""
                    SELECT close, date FROM financial_investments_pricepoint
                    WHERE symbol_id = %(sid)s
                    ORDER BY date DESC LIMIT 1
                """, {"sid": symbol_id})
                price_row = cur.fetchone()
                latest_price = float(price_row["close"]) if price_row else None
                price_date = price_row["date"].isoformat() if price_row else None

                # --- Latest holding ---
                holding = None
                if share_code:
                    cur.execute("""
                        SELECT quantity, total_value, profit_loss, annual_income_zar
                        FROM investec_investecjseportfolio
                        WHERE share_code = %(code)s
                        ORDER BY date DESC LIMIT 1
                    """, {"code": share_code})
                    h = cur.fetchone()
                    if h:
                        holding = _ser_row(dict(h))

                # --- Monthly performance (TTM yield) ---
                monthly_perf = None
                if share_name:
                    cur.execute("""
                        SELECT dividend_ttm, dividend_yield, closing_price,
                               quantity, total_market_value
                        FROM investec_investecjsesharemonthlyperformance
                        WHERE share_name = %(sn)s
                        ORDER BY date DESC LIMIT 1
                    """, {"sn": share_name})
                    mp = cur.fetchone()
                    if mp:
                        monthly_perf = _ser_row(dict(mp))

        # --- Compute derived metrics ---

        # Annual dividend totals (from yfinance data)
        annual_totals: dict[int, float] = defaultdict(float)
        for d in dividends:
            yr = int(d["date"][:4])
            annual_totals[yr] += d["amount"]

        sorted_years = sorted(annual_totals.keys())

        # Dividend yield
        ttm_dividend = 0.0
        current_year = datetime.date.today().year
        for d in dividends:
            d_date = datetime.date.fromisoformat(d["date"])
            if d_date >= datetime.date.today() - datetime.timedelta(days=365):
                ttm_dividend += d["amount"]

        div_yield = (ttm_dividend / latest_price * 100) if latest_price and ttm_dividend else None

        # Use Investec monthly performance yield if available (more accurate)
        if monthly_perf and monthly_perf.get("dividend_yield"):
            div_yield_display = round(monthly_perf["dividend_yield"] * 100, 2)
        elif div_yield:
            div_yield_display = round(div_yield, 2)
        else:
            div_yield_display = None

        # Dividend frequency (per year, from most recent full year)
        freq = None
        if len(sorted_years) >= 2:
            last_full_year = sorted_years[-2] if sorted_years[-1] == current_year else sorted_years[-1]
            freq_count = sum(1 for d in dividends if d["date"].startswith(str(last_full_year)))
            freq = freq_count

        # Growth rate (CAGR of annual dividends)
        growth_rate = None
        if len(sorted_years) >= 2:
            first_yr = sorted_years[0]
            last_yr = sorted_years[-1]
            if annual_totals[first_yr] > 0 and last_yr > first_yr:
                n = last_yr - first_yr
                growth_rate = round(
                    ((annual_totals[last_yr] / annual_totals[first_yr]) ** (1 / n) - 1) * 100, 2
                )

        # --- Build widgets ---
        widgets = []

        # KPI Cards row
        kpi_cards = []

        kpi_cards.append(_widget("KPICard", "Dividend Yield", {
            "title": "Dividend Yield",
            "value": f"{div_yield_display}%" if div_yield_display else "N/A",
            "format": "text",
            "subtitle": f"TTM div: {ttm_dividend:.2f}" if ttm_dividend else "",
            "status": "ok" if div_yield_display and div_yield_display > 2 else "neutral",
        }, width=1, height="sm"))

        kpi_cards.append(_widget("KPICard", "Annual Dividend", {
            "title": f"{current_year} Dividend",
            "value": f"{annual_totals.get(current_year, 0):.2f}",
            "format": "number",
            "subtitle": f"Prior year: {annual_totals.get(current_year - 1, 0):.2f}",
            "status": "ok",
        }, width=1, height="sm"))

        kpi_cards.append(_widget("KPICard", "Frequency", {
            "title": "Payments / Year",
            "value": str(freq) if freq else "N/A",
            "format": "text",
            "subtitle": "dividends per year" if freq else "",
        }, width=1, height="sm"))

        kpi_cards.append(_widget("KPICard", "Dividend Growth", {
            "title": "CAGR",
            "value": f"{growth_rate}%" if growth_rate is not None else "N/A",
            "format": "text",
            "subtitle": f"{sorted_years[0]}-{sorted_years[-1]}" if len(sorted_years) >= 2 else "",
            "trend": "up" if growth_rate and growth_rate > 0 else ("down" if growth_rate and growth_rate < 0 else None),
            "status": "ok" if growth_rate and growth_rate > 0 else ("warning" if growth_rate and growth_rate < 0 else "neutral"),
        }, width=1, height="sm"))

        widgets.extend(kpi_cards)

        # Dividend history table
        if dividends:
            history_headers = ["Date", "Amount", "Currency"]
            history_rows = [[d["date"], d["amount"], d.get("currency", "ZAR")] for d in dividends]
            widgets.append(_widget("DataGrid", f"{display_name} — Dividend History", {
                "headers": history_headers,
                "rows": history_rows,
                "title": f"{display_name} — Dividend History",
            }, width=2, height="md", data={"headers": history_headers, "rows": history_rows}))

        # Annual totals bar chart
        if len(sorted_years) >= 2:
            widgets.append(_widget("BarChart", f"{display_name} — Annual Dividends", {
                "title": f"{display_name} — Annual Dividends",
                "xAxis": [str(y) for y in sorted_years],
                "series": [{"name": "Total Dividend", "data": [round(annual_totals[y], 2) for y in sorted_years]}],
            }, width=2, height="md"))

        # Investec received dividends (actual cash received)
        if investec_divs:
            inv_headers = ["Date", "Type", "Amount Received", "Per Share", "Qty Held"]
            inv_rows = [
                [d["date"], d["type"], d["value"], d.get("value_per_share", ""), d.get("quantity", "")]
                for d in investec_divs
            ]
            widgets.append(_widget("DataGrid", f"{display_name} — Dividends Received (Investec)", {
                "headers": inv_headers,
                "rows": inv_rows,
                "title": f"Dividends Received — {display_name}",
            }, width=4, height="md", data={"headers": inv_headers, "rows": inv_rows}))

        return {
            "status": "widget_created",
            "message": f"Dividend report for {display_name}",
            "share": {
                "symbol": share.get("symbol"),
                "name": display_name,
                "exchange": share.get("exchange"),
                "latest_price": latest_price,
                "price_date": price_date,
            },
            "summary": {
                "ttm_dividend": round(ttm_dividend, 2) if ttm_dividend else None,
                "dividend_yield_pct": div_yield_display,
                "payments_per_year": freq,
                "cagr_pct": growth_rate,
                "annual_totals": {str(y): round(annual_totals[y], 2) for y in sorted_years},
            },
            "widgets": [w for w in widgets],
        }

    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
#  Report: Holdings Report
# ---------------------------------------------------------------------------

def build_holdings_report(top: int = 50) -> dict[str, Any]:
    """
    Build a portfolio holdings report showing all current positions with P&L,
    allocation percentage, and annual income.
    Returns a DataGrid widget + KPI summary cards.

    top: Max number of holdings to include (default 50)
    """
    try:
        with psycopg2.connect(**_FINANCIALS_DSN) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                # Get latest portfolio snapshot per share
                cur.execute("""
                    SELECT DISTINCT ON (p.share_code)
                        p.company, p.share_code, p.quantity, p.currency,
                        p.unit_cost, p.total_cost, p.price, p.total_value,
                        p.profit_loss, p.portfolio_percent, p.annual_income_zar,
                        p.date
                    FROM investec_investecjseportfolio p
                    ORDER BY p.share_code, p.date DESC
                    LIMIT %(top)s
                """, {"top": top})
                rows = [_ser_row(dict(r)) for r in cur.fetchall()]

        if not rows:
            return {"error": "No portfolio holdings found"}

        # Compute totals
        total_value = sum(r.get("total_value", 0) or 0 for r in rows)
        total_cost = sum(r.get("total_cost", 0) or 0 for r in rows)
        total_pl = sum(r.get("profit_loss", 0) or 0 for r in rows)
        total_income = sum(r.get("annual_income_zar", 0) or 0 for r in rows)
        pl_pct = round(total_pl / total_cost * 100, 2) if total_cost else 0

        widgets = []

        # KPI cards
        widgets.append(_widget("KPICard", "Portfolio Value", {
            "title": "Total Value",
            "value": f"R{total_value:,.0f}",
            "format": "text",
            "subtitle": f"{len(rows)} holdings",
            "status": "ok",
        }, width=1, height="sm"))

        widgets.append(_widget("KPICard", "Profit / Loss", {
            "title": "Total P&L",
            "value": f"R{total_pl:,.0f}",
            "format": "text",
            "subtitle": f"{pl_pct:+.1f}%",
            "status": "ok" if total_pl >= 0 else "critical",
            "trend": "up" if total_pl >= 0 else "down",
        }, width=1, height="sm"))

        widgets.append(_widget("KPICard", "Annual Income", {
            "title": "Annual Income",
            "value": f"R{total_income:,.0f}",
            "format": "text",
            "subtitle": f"yield {total_income / total_value * 100:.1f}%" if total_value else "",
            "status": "ok",
        }, width=1, height="sm"))

        widgets.append(_widget("KPICard", "Cost Basis", {
            "title": "Total Cost",
            "value": f"R{total_cost:,.0f}",
            "format": "text",
        }, width=1, height="sm"))

        # Holdings grid
        headers = ["Company", "Code", "Qty", "Cost", "Price", "Value", "P&L", "P&L%", "Weight%", "Income"]
        grid_rows = []
        for r in sorted(rows, key=lambda x: x.get("total_value", 0) or 0, reverse=True):
            cost = r.get("total_cost", 0) or 0
            pl = r.get("profit_loss", 0) or 0
            pl_row_pct = round(pl / cost * 100, 1) if cost else 0
            grid_rows.append([
                r.get("company", ""),
                r.get("share_code", ""),
                r.get("quantity", 0),
                round(cost),
                r.get("price", 0),
                round(r.get("total_value", 0) or 0),
                round(pl),
                f"{pl_row_pct:+.1f}%",
                f"{r.get('portfolio_percent', 0) or 0:.1f}%",
                round(r.get("annual_income_zar", 0) or 0),
            ])

        widgets.append(_widget("DataGrid", "Portfolio Holdings", {
            "headers": headers,
            "rows": grid_rows,
            "title": "Portfolio Holdings",
        }, width=4, height="lg", data={"headers": headers, "rows": grid_rows}))

        # Allocation pie chart
        pie_data = [
            {"name": r.get("company", r.get("share_code", "")),
             "value": round(r.get("total_value", 0) or 0)}
            for r in sorted(rows, key=lambda x: x.get("total_value", 0) or 0, reverse=True)[:15]
        ]
        widgets.append(_widget("PieChart", "Portfolio Allocation", {
            "title": "Portfolio Allocation",
            "data": pie_data,
        }, width=2, height="md"))

        return {
            "status": "widget_created",
            "message": f"Holdings report: {len(rows)} positions, total value R{total_value:,.0f}",
            "summary": {
                "total_value": round(total_value, 2),
                "total_cost": round(total_cost, 2),
                "total_pl": round(total_pl, 2),
                "pl_pct": pl_pct,
                "annual_income": round(total_income, 2),
                "holdings_count": len(rows),
            },
            "widgets": widgets,
        }

    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
#  Report: Transaction Summary
# ---------------------------------------------------------------------------

def build_transaction_summary(
    symbol_search: str | None = None,
    years: int = 3,
) -> dict[str, Any]:
    """
    Build a transaction activity summary showing buys, sells, dividends, and fees.
    If symbol_search is provided, show for that share only. Otherwise show all.

    symbol_search: Optional share name/symbol filter (e.g. 'Absa'). None = all shares.
    years: How many years of history (default 3)
    """
    try:
        with psycopg2.connect(**_FINANCIALS_DSN) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cutoff = datetime.date.today() - datetime.timedelta(days=years * 365)

                if symbol_search:
                    share = _find_share(cur, symbol_search)
                    if not share or not share.get("share_name"):
                        return {"error": f"No share found matching '{symbol_search}'"}
                    display_name = share.get("company") or share.get("name") or symbol_search

                    cur.execute("""
                        SELECT date, type, description, quantity, value,
                               value_per_share, value_calculated
                        FROM investec_investecjsetransaction
                        WHERE share_name = %(sn)s AND date >= %(cutoff)s
                        ORDER BY date DESC
                    """, {"sn": share["share_name"], "cutoff": cutoff})
                else:
                    display_name = "All Shares"
                    cur.execute("""
                        SELECT date, share_name, type, description, quantity, value,
                               value_per_share, value_calculated
                        FROM investec_investecjsetransaction
                        WHERE date >= %(cutoff)s
                        ORDER BY date DESC
                        LIMIT 500
                    """, {"cutoff": cutoff})

                txns = [_ser_row(dict(r)) for r in cur.fetchall()]

        if not txns:
            return {"error": f"No transactions found for '{display_name}' in last {years} years"}

        # Aggregate by type
        type_totals: dict[str, float] = defaultdict(float)
        type_counts: dict[str, int] = defaultdict(int)
        for t in txns:
            ttype = t.get("type", "Other")
            type_totals[ttype] += abs(t.get("value", 0) or 0)
            type_counts[ttype] += 1

        widgets = []

        # Summary KPIs
        buy_total = type_totals.get("Buy", 0)
        sell_total = type_totals.get("Sell", 0)
        div_total = sum(v for k, v in type_totals.items() if "dividend" in k.lower())

        widgets.append(_widget("KPICard", "Total Bought", {
            "title": "Bought", "value": f"R{buy_total:,.0f}", "format": "text",
            "subtitle": f"{type_counts.get('Buy', 0)} transactions",
        }, width=1, height="sm"))

        widgets.append(_widget("KPICard", "Total Sold", {
            "title": "Sold", "value": f"R{sell_total:,.0f}", "format": "text",
            "subtitle": f"{type_counts.get('Sell', 0)} transactions",
        }, width=1, height="sm"))

        widgets.append(_widget("KPICard", "Dividends Received", {
            "title": "Dividends", "value": f"R{div_total:,.0f}", "format": "text",
            "subtitle": f"{sum(v for k, v in type_counts.items() if 'dividend' in k.lower())} payments",
        }, width=1, height="sm"))

        widgets.append(_widget("KPICard", "Fees Paid", {
            "title": "Fees", "value": f"R{type_totals.get('Fee', 0) + type_totals.get('Broker Fee', 0):,.0f}",
            "format": "text",
        }, width=1, height="sm"))

        # Transaction table
        if symbol_search:
            headers = ["Date", "Type", "Qty", "Value", "Per Share"]
            grid_rows = [
                [t["date"], t.get("type", ""), t.get("quantity", ""),
                 t.get("value", ""), t.get("value_per_share", "")]
                for t in txns
            ]
        else:
            headers = ["Date", "Share", "Type", "Qty", "Value", "Per Share"]
            grid_rows = [
                [t["date"], t.get("share_name", ""), t.get("type", ""),
                 t.get("quantity", ""), t.get("value", ""), t.get("value_per_share", "")]
                for t in txns
            ]

        widgets.append(_widget("DataGrid", f"Transactions — {display_name}", {
            "headers": headers, "rows": grid_rows,
            "title": f"Transactions — {display_name}",
        }, width=4, height="lg", data={"headers": headers, "rows": grid_rows}))

        return {
            "status": "widget_created",
            "message": f"Transaction summary for {display_name}: {len(txns)} transactions",
            "summary": {
                "total_transactions": len(txns),
                "bought": round(buy_total, 2),
                "sold": round(sell_total, 2),
                "dividends_received": round(div_total, 2),
                "by_type": {k: {"count": type_counts[k], "total": round(v, 2)} for k, v in type_totals.items()},
            },
            "widgets": widgets,
        }

    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
#  Report: Dividend Yield Over Time
# ---------------------------------------------------------------------------

def build_dividend_yield_chart(
    symbol_search: str,
    years: int = 5,
) -> dict[str, Any]:
    """
    Build a dividend yield over time chart for a share.
    Calculates yield = annual dividends / average closing price for each year.
    Returns a BarChart + DataGrid showing yield % per year.

    symbol_search: Share name or symbol (e.g. 'Absa', 'NED.JO')
    years: Years of history (default 5)
    """
    try:
        with psycopg2.connect(**_FINANCIALS_DSN) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                share = _find_share(cur, symbol_search)
                if not share:
                    return {"error": f"No share found matching '{symbol_search}'"}

                symbol_id = share["id"]
                display_name = share.get("company") or share.get("name") or share.get("symbol")
                cutoff = datetime.date.today() - datetime.timedelta(days=years * 365)

                # Get dividends per year
                cur.execute("""
                    SELECT EXTRACT(YEAR FROM date)::int AS yr, SUM(amount) AS total_div
                    FROM financial_investments_dividend
                    WHERE symbol_id = %(sid)s AND date >= %(cutoff)s
                    GROUP BY yr ORDER BY yr
                """, {"sid": symbol_id, "cutoff": cutoff})
                div_by_year = {int(r["yr"]): float(r["total_div"]) for r in cur.fetchall()}

                # Get average closing price per year
                cur.execute("""
                    SELECT EXTRACT(YEAR FROM date)::int AS yr, AVG(close) AS avg_price
                    FROM financial_investments_pricepoint
                    WHERE symbol_id = %(sid)s AND date >= %(cutoff)s
                    GROUP BY yr ORDER BY yr
                """, {"sid": symbol_id, "cutoff": cutoff})
                price_by_year = {int(r["yr"]): float(r["avg_price"]) for r in cur.fetchall()}

        # Calculate yield per year
        all_years = sorted(set(div_by_year.keys()) | set(price_by_year.keys()))
        yield_data = []
        for yr in all_years:
            div = div_by_year.get(yr, 0)
            price = price_by_year.get(yr)
            yld = round(div / price * 100, 2) if price and price > 0 else None
            yield_data.append({
                "year": yr,
                "annual_dividend": round(div, 2),
                "avg_price": round(price, 2) if price else None,
                "yield_pct": yld,
            })

        if not yield_data:
            return {"error": f"No dividend or price data found for '{display_name}'"}

        widgets = []

        # Bar chart: yield % per year
        chart_years = [str(d["year"]) for d in yield_data if d["yield_pct"] is not None]
        chart_yields = [d["yield_pct"] for d in yield_data if d["yield_pct"] is not None]
        chart_divs = [d["annual_dividend"] for d in yield_data if d["yield_pct"] is not None]

        if chart_years:
            widgets.append(_widget("BarChart", f"{display_name} — Dividend Yield Over Time", {
                "title": f"{display_name} — Dividend Yield %",
                "xAxis": chart_years,
                "series": [
                    {"name": "Yield %", "data": chart_yields},
                ],
            }, width=3, height="md"))

            # Also show annual dividends as a second chart
            widgets.append(_widget("BarChart", f"{display_name} — Annual Dividends", {
                "title": f"{display_name} — Annual Dividends",
                "xAxis": chart_years,
                "series": [
                    {"name": "Total Dividend", "data": chart_divs},
                ],
            }, width=3, height="md"))

        # Data table with all details
        headers = ["Year", "Annual Dividend", "Avg Price", "Yield %"]
        rows = [
            [d["year"], d["annual_dividend"], d["avg_price"] or "N/A",
             f"{d['yield_pct']}%" if d["yield_pct"] is not None else "N/A"]
            for d in yield_data
        ]
        widgets.append(_widget("DataGrid", f"{display_name} — Yield History", {
            "headers": headers, "rows": rows,
        }, width=2, height="md", data={"headers": headers, "rows": rows}))

        # Current yield KPI
        latest = yield_data[-1] if yield_data else None
        if latest and latest["yield_pct"] is not None:
            widgets.insert(0, _widget("KPICard", "Current Yield", {
                "title": f"{latest['year']} Yield",
                "value": f"{latest['yield_pct']}%",
                "format": "text",
                "subtitle": f"Div: {latest['annual_dividend']}, Avg Price: {latest['avg_price']}",
                "status": "ok" if latest["yield_pct"] > 2 else "neutral",
            }, width=1, height="sm"))

        return {
            "status": "widget_created",
            "message": f"Dividend yield chart for {display_name}",
            "share": {"symbol": share.get("symbol"), "name": display_name},
            "yield_data": yield_data,
            "widgets": widgets,
        }

    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
#  Tool schemas
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "name": "build_dividend_report",
        "description": (
            "Build a Google Finance-style dividend report for a share. "
            "Shows: TTM yield, annual totals, payment frequency, CAGR growth rate, "
            "full dividend history table, annual bar chart, and dividends received from Investec. "
            "Returns multiple dashboard widgets (KPI cards + DataGrid + BarChart)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol_search": {
                    "type": "string",
                    "description": "Share name or symbol (e.g. 'Absa', 'NED.JO', 'Capitec'). Fuzzy matched.",
                },
                "years": {
                    "type": "integer",
                    "description": "Years of dividend history to include (default 5).",
                },
            },
            "required": ["symbol_search"],
        },
    },
    {
        "name": "build_holdings_report",
        "description": (
            "Build a portfolio holdings report from Investec data. "
            "Shows all current positions with company, quantity, cost, price, value, P&L, "
            "allocation %, annual income. Includes summary KPI cards and allocation pie chart."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "top": {
                    "type": "integer",
                    "description": "Max holdings to include (default 50).",
                },
            },
        },
    },
    {
        "name": "build_dividend_yield_chart",
        "description": (
            "Build a dividend yield over time chart for a share. "
            "Calculates yield = annual dividends / average share price for each year. "
            "Returns BarChart (yield % per year), annual dividends BarChart, "
            "DataGrid with year/dividend/price/yield, and current yield KPI card."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol_search": {
                    "type": "string",
                    "description": "Share name or symbol (e.g. 'Absa', 'NED.JO'). Fuzzy matched.",
                },
                "years": {
                    "type": "integer",
                    "description": "Years of history (default 5).",
                },
            },
            "required": ["symbol_search"],
        },
    },
    {
        "name": "build_transaction_summary",
        "description": (
            "Build a transaction activity summary from Investec exports. "
            "Shows buys, sells, dividends received, and fees. "
            "Filter by share (e.g. 'Absa') or show all. "
            "Includes KPI totals + full transaction history grid."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol_search": {
                    "type": "string",
                    "description": "Optional: share name/symbol to filter (e.g. 'Absa'). Omit for all shares.",
                },
                "years": {
                    "type": "integer",
                    "description": "Years of history (default 3).",
                },
            },
        },
    },
]

TOOL_FUNCTIONS = {
    "build_dividend_report": build_dividend_report,
    "build_holdings_report": build_holdings_report,
    "build_dividend_yield_chart": build_dividend_yield_chart,
    "build_transaction_summary": build_transaction_summary,
}
