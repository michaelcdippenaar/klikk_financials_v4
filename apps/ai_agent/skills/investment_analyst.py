"""
Skill: Investment Analyst — search and retrieve share/stock data from local database.

Searches across all Investec JSE and Financial Investments tables:
- Investec JSE Share Name Mappings
- Investec JSE Transactions
- Investec JSE Portfolios
- Investec JSE Share Monthly Performances
- Financial Investments Symbols, PricePoints, Dividends, Analyst data, News
"""
from __future__ import annotations

import os
import sys
import datetime
import decimal
import json
from typing import Any

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


# ---------------------------------------------------------------------------
#  Tool: investment_lookup
# ---------------------------------------------------------------------------

def investment_lookup(search: str) -> dict[str, Any]:
    """
    Look up a share by code, symbol, or company name across all database tables.
    Returns identity info, latest holdings, latest price, monthly performance,
    and recent transactions in one call.

    search: Share code, symbol, or company name (e.g. 'SHP', 'Capitec', 'NED.JO')
    """
    try:
        with psycopg2.connect(**_FINANCIALS_DSN) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                result: dict[str, Any] = {"search": search}

                # --- Share Name Mappings ---
                cur.execute("""
                    SELECT id, share_name, share_name2, share_name3, company, share_code
                    FROM investec_investecjsesharenamemapping
                    WHERE LOWER(share_code) LIKE LOWER(%(q)s)
                       OR LOWER(company) LIKE LOWER(%(q)s)
                       OR LOWER(share_name) LIKE LOWER(%(q)s)
                       OR LOWER(share_name2) LIKE LOWER(%(q)s)
                       OR LOWER(share_name3) LIKE LOWER(%(q)s)
                """, {"q": f"%{search}%"})
                mappings = [_ser_row(dict(r)) for r in cur.fetchall()]
                result["share_name_mappings"] = mappings

                # --- Financial Investments Symbol ---
                cur.execute("""
                    SELECT s.id, s.symbol, s.name, s.exchange, s.category,
                           m.share_name, m.share_code, m.company
                    FROM financial_investments_symbol s
                    LEFT JOIN investec_investecjsesharenamemapping m
                        ON m.id = s.share_name_mapping_id
                    WHERE LOWER(s.symbol) LIKE LOWER(%(q)s)
                       OR LOWER(s.name) LIKE LOWER(%(q)s)
                       OR LOWER(m.share_code) LIKE LOWER(%(q)s)
                       OR LOWER(m.company) LIKE LOWER(%(q)s)
                       OR LOWER(m.share_name) LIKE LOWER(%(q)s)
                """, {"q": f"%{search}%"})
                symbols = [_ser_row(dict(r)) for r in cur.fetchall()]
                result["symbols"] = symbols

                # Derive share_name and share_code for further lookups
                share_name = None
                share_code = None
                symbol_id = None
                if symbols:
                    symbol_id = symbols[0].get("id")
                    share_name = symbols[0].get("share_name")
                    share_code = symbols[0].get("share_code")
                if not share_name and mappings:
                    share_name = mappings[0].get("share_name")
                    share_code = mappings[0].get("share_code")

                # --- Latest Portfolio Holdings ---
                if share_code:
                    cur.execute("""
                        SELECT company, share_code, quantity, currency, unit_cost,
                               total_cost, price, total_value, move_percent,
                               portfolio_percent, profit_loss, annual_income_zar, date
                        FROM investec_investecjseportfolio
                        WHERE LOWER(share_code) = LOWER(%(code)s)
                        ORDER BY date DESC LIMIT 3
                    """, {"code": share_code})
                    result["portfolio_holdings"] = [_ser_row(dict(r)) for r in cur.fetchall()]

                # --- Latest Price ---
                if symbol_id:
                    cur.execute("""
                        SELECT date, open, high, low, close, volume, adjusted_close
                        FROM financial_investments_pricepoint
                        WHERE symbol_id = %(sid)s
                        ORDER BY date DESC LIMIT 5
                    """, {"sid": symbol_id})
                    result["latest_prices"] = [_ser_row(dict(r)) for r in cur.fetchall()]

                # --- Monthly Performance ---
                if share_name:
                    cur.execute("""
                        SELECT share_name, date, dividend_type, investec_account,
                               dividend_ttm, closing_price, quantity,
                               total_market_value, dividend_yield
                        FROM investec_investecjsesharemonthlyperformance
                        WHERE LOWER(share_name) = LOWER(%(sn)s)
                        ORDER BY date DESC LIMIT 5
                    """, {"sn": share_name})
                    result["monthly_performance"] = [_ser_row(dict(r)) for r in cur.fetchall()]

                # --- Recent Transactions ---
                if share_name:
                    cur.execute("""
                        SELECT date, account_number, share_name, type, quantity,
                               value, value_per_share, dividend_ttm
                        FROM investec_investecjsetransaction
                        WHERE LOWER(share_name) = LOWER(%(sn)s)
                        ORDER BY date DESC LIMIT 10
                    """, {"sn": share_name})
                    result["recent_transactions"] = [_ser_row(dict(r)) for r in cur.fetchall()]

                if not mappings and not symbols:
                    result["message"] = (
                        f"No share found matching '{search}'. "
                        "Try a different code, symbol, or company name."
                    )

                return result

    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
#  Tool: investment_dividends
# ---------------------------------------------------------------------------

def investment_dividends(search: str, years: int = 5) -> dict[str, Any]:
    """
    Get dividend history for a share from both yfinance and Investec transaction records.

    search: Share code, symbol, or company name
    years: Years of history (default 5)
    """
    try:
        with psycopg2.connect(**_FINANCIALS_DSN) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                # Find the share
                cur.execute("""
                    SELECT s.id, s.symbol, s.name,
                           m.share_name, m.share_code, m.company
                    FROM financial_investments_symbol s
                    LEFT JOIN investec_investecjsesharenamemapping m
                        ON m.id = s.share_name_mapping_id
                    WHERE LOWER(s.symbol) LIKE LOWER(%(q)s)
                       OR LOWER(s.name) LIKE LOWER(%(q)s)
                       OR LOWER(m.share_code) LIKE LOWER(%(q)s)
                       OR LOWER(m.company) LIKE LOWER(%(q)s)
                       OR LOWER(m.share_name) LIKE LOWER(%(q)s)
                    LIMIT 1
                """, {"q": f"%{search}%"})
                share = cur.fetchone()

                if not share:
                    # Try mapping table directly
                    cur.execute("""
                        SELECT share_name, share_code, company
                        FROM investec_investecjsesharenamemapping
                        WHERE LOWER(share_code) LIKE LOWER(%(q)s)
                           OR LOWER(company) LIKE LOWER(%(q)s)
                           OR LOWER(share_name) LIKE LOWER(%(q)s)
                        LIMIT 1
                    """, {"q": f"%{search}%"})
                    mapping = cur.fetchone()
                    if not mapping:
                        return {"error": f"No share found matching '{search}'"}
                    share = dict(mapping)
                    share["id"] = None
                    share["symbol"] = None
                    share["name"] = share.get("company")
                else:
                    share = dict(share)

                cutoff = datetime.date.today() - datetime.timedelta(days=years * 365)
                result: dict[str, Any] = {
                    "search": search,
                    "share": _ser_row(share),
                }

                # yfinance dividends
                if share.get("id"):
                    cur.execute("""
                        SELECT date, amount, currency
                        FROM financial_investments_dividend
                        WHERE symbol_id = %(sid)s AND date >= %(cutoff)s
                        ORDER BY date DESC
                    """, {"sid": share["id"], "cutoff": cutoff})
                    result["yfinance_dividends"] = [_ser_row(dict(r)) for r in cur.fetchall()]

                # Investec transaction dividends
                sn = share.get("share_name")
                if sn:
                    cur.execute("""
                        SELECT date, type, value, value_per_share, quantity
                        FROM investec_investecjsetransaction
                        WHERE LOWER(share_name) = LOWER(%(sn)s)
                          AND type IN ('Dividend', 'Special Dividend', 'Foreign Dividend')
                          AND date >= %(cutoff)s
                        ORDER BY date DESC
                    """, {"sn": sn, "cutoff": cutoff})
                    result["investec_dividends_received"] = [_ser_row(dict(r)) for r in cur.fetchall()]

                    # Monthly performance (TTM yield)
                    cur.execute("""
                        SELECT date, dividend_ttm, dividend_yield, closing_price,
                               quantity, total_market_value
                        FROM investec_investecjsesharemonthlyperformance
                        WHERE LOWER(share_name) = LOWER(%(sn)s)
                        ORDER BY date DESC LIMIT 1
                    """, {"sn": sn})
                    perf = cur.fetchone()
                    if perf:
                        result["latest_ttm_performance"] = _ser_row(dict(perf))

                return result

    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
#  Tool: investment_analyst_data
# ---------------------------------------------------------------------------

def investment_analyst_data(search: str) -> dict[str, Any]:
    """
    Get analyst recommendations, price targets, company info, and recent news
    for a share from the Financial Investments database.

    search: Share code, symbol, or company name
    """
    try:
        with psycopg2.connect(**_FINANCIALS_DSN) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("""
                    SELECT s.id, s.symbol, s.name, s.exchange, s.category
                    FROM financial_investments_symbol s
                    LEFT JOIN investec_investecjsesharenamemapping m
                        ON m.id = s.share_name_mapping_id
                    WHERE LOWER(s.symbol) LIKE LOWER(%(q)s)
                       OR LOWER(s.name) LIKE LOWER(%(q)s)
                       OR LOWER(m.share_code) LIKE LOWER(%(q)s)
                       OR LOWER(m.company) LIKE LOWER(%(q)s)
                    LIMIT 1
                """, {"q": f"%{search}%"})
                share = cur.fetchone()
                if not share:
                    return {"error": f"No symbol found matching '{search}'"}

                share = dict(share)
                symbol_id = share["id"]
                result: dict[str, Any] = {"share": _ser_row(share)}

                # Company info
                cur.execute("""
                    SELECT data, fetched_at FROM financial_investments_symbolinfo
                    WHERE symbol_id = %(sid)s
                    ORDER BY fetched_at DESC LIMIT 1
                """, {"sid": symbol_id})
                row = cur.fetchone()
                if row:
                    data = row["data"] if isinstance(row["data"], dict) else json.loads(row["data"])
                    result["company_info"] = {
                        k: data.get(k) for k in [
                            "longName", "sector", "industry", "country", "marketCap",
                            "trailingPE", "forwardPE", "dividendYield", "payoutRatio",
                            "beta", "fiftyTwoWeekHigh", "fiftyTwoWeekLow",
                            "averageVolume", "longBusinessSummary",
                        ] if data.get(k) is not None
                    }

                # Analyst recommendations
                cur.execute("""
                    SELECT data, fetched_at FROM financial_investments_analystrecommendation
                    WHERE symbol_id = %(sid)s
                    ORDER BY fetched_at DESC LIMIT 1
                """, {"sid": symbol_id})
                row = cur.fetchone()
                if row:
                    result["analyst_recommendations"] = row["data"] if isinstance(row["data"], (dict, list)) else json.loads(row["data"])

                # Analyst price targets
                cur.execute("""
                    SELECT data, fetched_at FROM financial_investments_analystpricetarget
                    WHERE symbol_id = %(sid)s
                    ORDER BY fetched_at DESC LIMIT 1
                """, {"sid": symbol_id})
                row = cur.fetchone()
                if row:
                    result["analyst_price_targets"] = row["data"] if isinstance(row["data"], (dict, list)) else json.loads(row["data"])

                # Earnings
                cur.execute("""
                    SELECT data, fetched_at FROM financial_investments_earningsreport
                    WHERE symbol_id = %(sid)s
                    ORDER BY fetched_at DESC LIMIT 1
                """, {"sid": symbol_id})
                row = cur.fetchone()
                if row:
                    result["earnings"] = row["data"] if isinstance(row["data"], (dict, list)) else json.loads(row["data"])

                # Recent news
                cur.execute("""
                    SELECT title, publisher, link, published_at
                    FROM financial_investments_newsitem
                    WHERE symbol_id = %(sid)s
                    ORDER BY published_at DESC LIMIT 10
                """, {"sid": symbol_id})
                result["news"] = [_ser_row(dict(r)) for r in cur.fetchall()]

                return result

    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
#  Tool: investment_upcoming_dividends
# ---------------------------------------------------------------------------

def investment_upcoming_dividends(months: int = 3) -> dict[str, Any]:
    """
    Find shares likely to pay dividends in the next N months based on
    historical payment patterns. Analyses past dividend dates to predict
    upcoming payments.

    months: Look-ahead window in months (default 3)
    """
    try:
        with psycopg2.connect(**_FINANCIALS_DSN) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                today = datetime.date.today()

                # Get all symbols with dividends in the same calendar months
                # in previous years (pattern-based prediction)
                target_months = [(today.month + i - 1) % 12 + 1 for i in range(months)]

                cur.execute("""
                    SELECT s.symbol, s.name,
                           m.share_code, m.company,
                           d.date AS last_div_date, d.amount AS last_div_amount, d.currency,
                           EXTRACT(MONTH FROM d.date)::int AS div_month
                    FROM financial_investments_dividend d
                    JOIN financial_investments_symbol s ON s.id = d.symbol_id
                    LEFT JOIN investec_investecjsesharenamemapping m
                        ON m.id = s.share_name_mapping_id
                    WHERE EXTRACT(MONTH FROM d.date)::int = ANY(%(months)s)
                      AND d.date >= (CURRENT_DATE - INTERVAL '3 years')
                    ORDER BY s.symbol, d.date DESC
                """, {"months": target_months})

                rows = [_ser_row(dict(r)) for r in cur.fetchall()]

                # Group by symbol, show most recent payment per month
                seen: dict[str, dict] = {}
                for r in rows:
                    key = f"{r['symbol']}_{r['div_month']}"
                    if key not in seen:
                        seen[key] = r

                upcoming = sorted(seen.values(), key=lambda x: x.get("div_month", 0))

                # Also get latest portfolio holdings for context
                held_codes = set()
                cur.execute("""
                    SELECT DISTINCT share_code FROM investec_investecjseportfolio
                    WHERE date = (SELECT MAX(date) FROM investec_investecjseportfolio)
                """)
                held_codes = {r["share_code"] for r in cur.fetchall()}

                for item in upcoming:
                    item["currently_held"] = item.get("share_code") in held_codes

                return {
                    "months_ahead": months,
                    "target_months": target_months,
                    "predicted_dividends": upcoming,
                    "count": len(upcoming),
                    "note": (
                        "Predictions based on historical dividend payment months. "
                        "Actual payment dates may vary."
                    ),
                }

    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
#  Tool: investment_portfolio_summary
# ---------------------------------------------------------------------------

def investment_portfolio_summary() -> dict[str, Any]:
    """
    Get a summary of the entire portfolio: total value, P&L, top holdings,
    total annual income, and overall allocation.
    """
    try:
        with psycopg2.connect(**_FINANCIALS_DSN) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("""
                    SELECT DISTINCT ON (share_code)
                        company, share_code, quantity, currency, unit_cost,
                        total_cost, price, total_value, move_percent,
                        portfolio_percent, profit_loss, annual_income_zar, date
                    FROM investec_investecjseportfolio
                    ORDER BY share_code, date DESC
                """)
                holdings = [_ser_row(dict(r)) for r in cur.fetchall()]

                if not holdings:
                    return {"error": "No portfolio holdings found"}

                total_value = sum(h.get("total_value", 0) or 0 for h in holdings)
                total_cost = sum(h.get("total_cost", 0) or 0 for h in holdings)
                total_pl = sum(h.get("profit_loss", 0) or 0 for h in holdings)
                total_income = sum(h.get("annual_income_zar", 0) or 0 for h in holdings)

                # Sort by value for top holdings
                by_value = sorted(holdings, key=lambda x: x.get("total_value", 0) or 0, reverse=True)

                return {
                    "portfolio_date": holdings[0].get("date") if holdings else None,
                    "total_value": round(total_value, 2),
                    "total_cost": round(total_cost, 2),
                    "total_profit_loss": round(total_pl, 2),
                    "total_pl_pct": round(total_pl / total_cost * 100, 2) if total_cost else 0,
                    "total_annual_income": round(total_income, 2),
                    "portfolio_yield_pct": round(total_income / total_value * 100, 2) if total_value else 0,
                    "holdings_count": len(holdings),
                    "top_10_by_value": by_value[:10],
                    "all_holdings": by_value,
                }

    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
#  Tool: investment_screen
# ---------------------------------------------------------------------------

def investment_screen(
    max_pe: float | None = None,
    min_dividend_yield: float | None = None,
    min_pe: float | None = None,
    max_dividend_yield: float | None = None,
    sector: str | None = None,
    only_held: bool = False,
) -> dict[str, Any]:
    """
    Screen/filter shares across the database by financial criteria.
    Uses company info (from yfinance SymbolInfo) for PE ratio, dividend yield, sector, etc.

    max_pe: Maximum trailing P/E ratio (e.g. 10)
    min_dividend_yield: Minimum dividend yield as percentage (e.g. 5 for 5%)
    min_pe: Minimum trailing P/E ratio
    max_dividend_yield: Maximum dividend yield as percentage
    sector: Filter by sector name (partial match)
    only_held: If True, only show shares currently in portfolio
    """
    try:
        with psycopg2.connect(**_FINANCIALS_DSN) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                # Get all symbols with their info
                cur.execute("""
                    SELECT s.id, s.symbol, s.name, s.exchange, s.category,
                           m.share_code, m.company,
                           si.data AS info_data
                    FROM financial_investments_symbol s
                    LEFT JOIN investec_investecjsesharenamemapping m
                        ON m.id = s.share_name_mapping_id
                    LEFT JOIN (
                        SELECT DISTINCT ON (symbol_id) symbol_id, data
                        FROM financial_investments_symbolinfo
                        ORDER BY symbol_id, fetched_at DESC
                    ) si ON si.symbol_id = s.id
                """)
                rows = cur.fetchall()

                # Get held share codes
                held_codes = set()
                if only_held:
                    cur.execute("""
                        SELECT DISTINCT share_code FROM investec_investecjseportfolio
                        WHERE date = (SELECT MAX(date) FROM investec_investecjseportfolio)
                    """)
                    held_codes = {r["share_code"] for r in cur.fetchall()}

                matches = []
                for row in rows:
                    row = dict(row)
                    info = row.get("info_data")
                    if not info:
                        continue
                    if isinstance(info, str):
                        info = json.loads(info)

                    trailing_pe = info.get("trailingPE")
                    div_yield_raw = info.get("dividendYield")  # yfinance stores as decimal (0.05 = 5%)
                    div_yield_pct = div_yield_raw * 100 if div_yield_raw else None
                    stock_sector = info.get("sector", "")

                    # Apply filters
                    if max_pe is not None and (trailing_pe is None or trailing_pe > max_pe):
                        continue
                    if min_pe is not None and (trailing_pe is None or trailing_pe < min_pe):
                        continue
                    if min_dividend_yield is not None and (div_yield_pct is None or div_yield_pct < min_dividend_yield):
                        continue
                    if max_dividend_yield is not None and (div_yield_pct is None or div_yield_pct > max_dividend_yield):
                        continue
                    if sector and sector.lower() not in stock_sector.lower():
                        continue
                    if only_held and row.get("share_code") not in held_codes:
                        continue

                    matches.append({
                        "symbol": row.get("symbol"),
                        "name": row.get("company") or row.get("name"),
                        "share_code": row.get("share_code"),
                        "exchange": row.get("exchange"),
                        "trailing_pe": round(trailing_pe, 2) if trailing_pe else None,
                        "dividend_yield_pct": round(div_yield_pct, 2) if div_yield_pct else None,
                        "sector": stock_sector,
                        "industry": info.get("industry", ""),
                        "market_cap": info.get("marketCap"),
                        "forward_pe": round(info["forwardPE"], 2) if info.get("forwardPE") else None,
                        "payout_ratio": round(info["payoutRatio"] * 100, 2) if info.get("payoutRatio") else None,
                        "beta": info.get("beta"),
                        "52w_high": info.get("fiftyTwoWeekHigh"),
                        "52w_low": info.get("fiftyTwoWeekLow"),
                    })

                # Sort by dividend yield descending
                matches.sort(key=lambda x: x.get("dividend_yield_pct") or 0, reverse=True)

                return {
                    "filters_applied": {
                        "max_pe": max_pe,
                        "min_dividend_yield": min_dividend_yield,
                        "min_pe": min_pe,
                        "max_dividend_yield": max_dividend_yield,
                        "sector": sector,
                        "only_held": only_held,
                    },
                    "matches": matches,
                    "count": len(matches),
                }

    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
#  Tool: investment_price_performance
# ---------------------------------------------------------------------------

def investment_price_performance(search: str) -> dict[str, Any]:
    """
    Calculate price performance for a share over multiple periods:
    1 week, 1 month, 3 months, 6 months, YTD, 1 year, 2 years.
    Also shows current price vs 52-week high/low.

    search: Share code, symbol, or company name
    """
    try:
        with psycopg2.connect(**_FINANCIALS_DSN) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                # Find symbol
                cur.execute("""
                    SELECT s.id, s.symbol, s.name,
                           m.share_code, m.company
                    FROM financial_investments_symbol s
                    LEFT JOIN investec_investecjsesharenamemapping m
                        ON m.id = s.share_name_mapping_id
                    WHERE LOWER(s.symbol) LIKE LOWER(%(q)s)
                       OR LOWER(s.name) LIKE LOWER(%(q)s)
                       OR LOWER(m.share_code) LIKE LOWER(%(q)s)
                       OR LOWER(m.company) LIKE LOWER(%(q)s)
                       OR LOWER(m.share_name) LIKE LOWER(%(q)s)
                    LIMIT 1
                """, {"q": f"%{search}%"})
                share = cur.fetchone()
                if not share:
                    return {"error": f"No share found matching '{search}'"}
                share = dict(share)
                symbol_id = share["id"]

                # Get all available prices (last 2 years)
                cur.execute("""
                    SELECT date, close FROM financial_investments_pricepoint
                    WHERE symbol_id = %(sid)s ORDER BY date DESC
                    LIMIT 600
                """, {"sid": symbol_id})
                prices = [_ser_row(dict(r)) for r in cur.fetchall()]

                if not prices:
                    return {"error": f"No price data for {share.get('symbol', search)}"}

                latest = prices[0]
                latest_close = float(latest["close"])
                latest_date = latest["date"]

                # Build lookup by date
                price_by_date = {p["date"]: float(p["close"]) for p in prices}
                all_closes = [float(p["close"]) for p in prices]

                # 52-week high/low from actual prices
                prices_1y = all_closes[:260]  # ~260 trading days
                high_52w = max(prices_1y) if prices_1y else None
                low_52w = min(prices_1y) if prices_1y else None

                # Calculate returns for various periods
                today = datetime.date.today()
                periods = {
                    "1_week": today - datetime.timedelta(days=7),
                    "1_month": today - datetime.timedelta(days=30),
                    "3_months": today - datetime.timedelta(days=91),
                    "6_months": today - datetime.timedelta(days=182),
                    "ytd": datetime.date(today.year, 1, 1),
                    "1_year": today - datetime.timedelta(days=365),
                    "2_years": today - datetime.timedelta(days=730),
                }

                returns = {}
                for label, start_date in periods.items():
                    # Find closest price on or before the start date
                    start_str = start_date.isoformat()
                    ref_price = None
                    for p in reversed(prices):
                        if p["date"] <= start_str:
                            ref_price = float(p["close"])
                            break
                    if ref_price and ref_price > 0:
                        ret = ((latest_close - ref_price) / ref_price) * 100
                        returns[label] = {
                            "return_pct": round(ret, 2),
                            "start_price": round(ref_price, 2),
                        }

                result = {
                    "symbol": share.get("symbol"),
                    "name": share.get("company") or share.get("name"),
                    "latest_price": round(latest_close, 2),
                    "latest_date": latest_date,
                    "returns": returns,
                    "data_points": len(prices),
                }

                if high_52w is not None:
                    result["52_week_high"] = round(high_52w, 2)
                    result["52_week_low"] = round(low_52w, 2)
                    result["pct_from_52w_high"] = round(
                        ((latest_close - high_52w) / high_52w) * 100, 2
                    )
                    result["pct_from_52w_low"] = round(
                        ((latest_close - low_52w) / low_52w) * 100, 2
                    ) if low_52w > 0 else None

                return result

    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
#  Tool: investment_portfolio_performance
# ---------------------------------------------------------------------------

def investment_portfolio_performance(search: str | None = None) -> dict[str, Any]:
    """
    Calculate return on investment (ROI) for portfolio holdings, including
    dividends received. If search is given, calculate for a single share;
    otherwise calculate for all holdings.

    search: Optional share code/name to filter to a single holding
    """
    try:
        with psycopg2.connect(**_FINANCIALS_DSN) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                # Get latest portfolio holdings
                where_clause = ""
                params: dict[str, Any] = {}
                if search:
                    where_clause = """
                        AND (LOWER(p.share_code) LIKE LOWER(%(q)s)
                             OR LOWER(p.company) LIKE LOWER(%(q)s))
                    """
                    params["q"] = f"%{search}%"

                cur.execute(f"""
                    SELECT DISTINCT ON (p.share_code)
                        p.company, p.share_code, p.quantity, p.currency,
                        p.unit_cost, p.total_cost, p.price, p.total_value,
                        p.profit_loss, p.annual_income_zar, p.date,
                        m.share_name
                    FROM investec_investecjseportfolio p
                    LEFT JOIN investec_investecjsesharenamemapping m
                        ON LOWER(m.share_code) = LOWER(p.share_code)
                    WHERE 1=1 {where_clause}
                    ORDER BY p.share_code, p.date DESC
                """, params)
                holdings = [_ser_row(dict(r)) for r in cur.fetchall()]

                if not holdings:
                    return {"error": f"No holdings found{f' matching {search!r}' if search else ''}"}

                results = []
                portfolio_total_cost = 0.0
                portfolio_total_value = 0.0
                portfolio_total_dividends = 0.0

                for h in holdings:
                    share_name = h.get("share_name")
                    total_cost = float(h.get("total_cost") or 0)
                    total_value = float(h.get("total_value") or 0)
                    capital_gain = total_value - total_cost

                    # Get total dividends received
                    total_divs = 0.0
                    if share_name:
                        cur.execute("""
                            SELECT COALESCE(SUM(value), 0) AS total
                            FROM investec_investecjsetransaction
                            WHERE LOWER(share_name) = LOWER(%(sn)s)
                              AND type IN ('Dividend', 'Special Dividend', 'Foreign Dividend')
                        """, {"sn": share_name})
                        row = cur.fetchone()
                        total_divs = float(row["total"]) if row else 0.0

                    total_return = capital_gain + total_divs
                    roi_pct = (total_return / total_cost * 100) if total_cost > 0 else 0.0

                    entry = {
                        "company": h.get("company"),
                        "share_code": h.get("share_code"),
                        "quantity": h.get("quantity"),
                        "cost_price": h.get("unit_cost"),
                        "total_cost": round(total_cost, 2),
                        "current_price": h.get("price"),
                        "current_value": round(total_value, 2),
                        "capital_gain": round(capital_gain, 2),
                        "dividends_received": round(total_divs, 2),
                        "total_return": round(total_return, 2),
                        "roi_pct": round(roi_pct, 2),
                        "annual_income": h.get("annual_income_zar"),
                    }
                    results.append(entry)

                    portfolio_total_cost += total_cost
                    portfolio_total_value += total_value
                    portfolio_total_dividends += total_divs

                # Sort by ROI descending
                results.sort(key=lambda x: x.get("roi_pct", 0), reverse=True)

                portfolio_capital_gain = portfolio_total_value - portfolio_total_cost
                portfolio_total_return = portfolio_capital_gain + portfolio_total_dividends
                portfolio_roi = (portfolio_total_return / portfolio_total_cost * 100) if portfolio_total_cost > 0 else 0.0

                return {
                    "portfolio_summary": {
                        "total_cost": round(portfolio_total_cost, 2),
                        "total_value": round(portfolio_total_value, 2),
                        "capital_gain": round(portfolio_capital_gain, 2),
                        "dividends_received": round(portfolio_total_dividends, 2),
                        "total_return": round(portfolio_total_return, 2),
                        "roi_pct": round(portfolio_roi, 2),
                    },
                    "holdings": results,
                    "count": len(results),
                }

    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
#  Tool: investment_compare
# ---------------------------------------------------------------------------

def investment_compare(symbols: list[str]) -> dict[str, Any]:
    """
    Compare 2-5 shares side by side on key metrics: P/E, dividend yield,
    market cap, sector, beta, 52-week range, latest price, and ROE.

    symbols: List of share codes/symbols/company names to compare
    """
    if not symbols or len(symbols) < 2:
        return {"error": "Provide at least 2 symbols to compare"}
    if len(symbols) > 5:
        symbols = symbols[:5]

    try:
        with psycopg2.connect(**_FINANCIALS_DSN) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                comparisons = []

                for search in symbols:
                    cur.execute("""
                        SELECT s.id, s.symbol, s.name, s.exchange,
                               m.share_code, m.company,
                               si.data AS info_data
                        FROM financial_investments_symbol s
                        LEFT JOIN investec_investecjsesharenamemapping m
                            ON m.id = s.share_name_mapping_id
                        LEFT JOIN (
                            SELECT DISTINCT ON (symbol_id) symbol_id, data
                            FROM financial_investments_symbolinfo
                            ORDER BY symbol_id, fetched_at DESC
                        ) si ON si.symbol_id = s.id
                        WHERE LOWER(s.symbol) LIKE LOWER(%(q)s)
                           OR LOWER(s.name) LIKE LOWER(%(q)s)
                           OR LOWER(m.share_code) LIKE LOWER(%(q)s)
                           OR LOWER(m.company) LIKE LOWER(%(q)s)
                        LIMIT 1
                    """, {"q": f"%{search}%"})
                    row = cur.fetchone()
                    if not row:
                        comparisons.append({"search": search, "error": "Not found"})
                        continue

                    row = dict(row)
                    info = row.get("info_data") or {}
                    if isinstance(info, str):
                        info = json.loads(info)

                    # Get latest price
                    cur.execute("""
                        SELECT close, date FROM financial_investments_pricepoint
                        WHERE symbol_id = %(sid)s ORDER BY date DESC LIMIT 1
                    """, {"sid": row["id"]})
                    price_row = cur.fetchone()
                    latest_price = float(price_row["close"]) if price_row else None
                    price_date = _serialize(price_row["date"]) if price_row else None

                    div_yield_raw = info.get("dividendYield")
                    div_yield_pct = round(div_yield_raw * 100, 2) if div_yield_raw else None

                    comparisons.append({
                        "symbol": row.get("symbol"),
                        "name": row.get("company") or row.get("name"),
                        "share_code": row.get("share_code"),
                        "sector": info.get("sector", ""),
                        "industry": info.get("industry", ""),
                        "latest_price": round(latest_price, 2) if latest_price else None,
                        "price_date": price_date,
                        "market_cap": info.get("marketCap"),
                        "trailing_pe": round(info["trailingPE"], 2) if info.get("trailingPE") else None,
                        "forward_pe": round(info["forwardPE"], 2) if info.get("forwardPE") else None,
                        "dividend_yield_pct": div_yield_pct,
                        "payout_ratio_pct": round(info["payoutRatio"] * 100, 2) if info.get("payoutRatio") else None,
                        "beta": info.get("beta"),
                        "roe": round(info["returnOnEquity"] * 100, 2) if info.get("returnOnEquity") else None,
                        "profit_margin_pct": round(info["profitMargin"] * 100, 2) if info.get("profitMargin") else None,
                        "52w_high": info.get("fiftyTwoWeekHigh"),
                        "52w_low": info.get("fiftyTwoWeekLow"),
                        "book_value": info.get("bookValue"),
                        "price_to_book": round(info["priceToBook"], 2) if info.get("priceToBook") else None,
                    })

                return {"comparisons": comparisons, "count": len(comparisons)}

    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
#  Tool: investment_dividend_analysis
# ---------------------------------------------------------------------------

def investment_dividend_analysis(search: str, years: int = 5) -> dict[str, Any]:
    """
    Analyse dividend consistency and growth for a share:
    - Annual dividend totals and year-over-year growth
    - Dividend growth rate (CAGR)
    - Payment consistency (how many years dividends were paid)
    - Yield on cost (if held in portfolio)
    - Current TTM yield

    search: Share code, symbol, or company name
    years: Years of history to analyse (default 5)
    """
    try:
        with psycopg2.connect(**_FINANCIALS_DSN) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                # Find share
                cur.execute("""
                    SELECT s.id, s.symbol, s.name,
                           m.share_name, m.share_code, m.company
                    FROM financial_investments_symbol s
                    LEFT JOIN investec_investecjsesharenamemapping m
                        ON m.id = s.share_name_mapping_id
                    WHERE LOWER(s.symbol) LIKE LOWER(%(q)s)
                       OR LOWER(s.name) LIKE LOWER(%(q)s)
                       OR LOWER(m.share_code) LIKE LOWER(%(q)s)
                       OR LOWER(m.company) LIKE LOWER(%(q)s)
                       OR LOWER(m.share_name) LIKE LOWER(%(q)s)
                    LIMIT 1
                """, {"q": f"%{search}%"})
                share = cur.fetchone()
                if not share:
                    return {"error": f"No share found matching '{search}'"}
                share = dict(share)

                cutoff = datetime.date.today() - datetime.timedelta(days=years * 365)
                result: dict[str, Any] = {
                    "symbol": share.get("symbol"),
                    "name": share.get("company") or share.get("name"),
                }

                # Annual dividends from yfinance data
                if share.get("id"):
                    cur.execute("""
                        SELECT EXTRACT(YEAR FROM date)::int AS year,
                               SUM(amount) AS total,
                               COUNT(*) AS payments
                        FROM financial_investments_dividend
                        WHERE symbol_id = %(sid)s AND date >= %(cutoff)s
                        GROUP BY EXTRACT(YEAR FROM date)::int
                        ORDER BY year
                    """, {"sid": share["id"], "cutoff": cutoff})
                    annual = [_ser_row(dict(r)) for r in cur.fetchall()]

                    # Calculate YoY growth
                    for i in range(1, len(annual)):
                        prev = float(annual[i - 1]["total"])
                        curr = float(annual[i]["total"])
                        if prev > 0:
                            annual[i]["yoy_growth_pct"] = round(
                                ((curr - prev) / prev) * 100, 2
                            )

                    result["annual_dividends"] = annual
                    result["years_with_dividends"] = len(annual)
                    result["years_analysed"] = years

                    # CAGR
                    if len(annual) >= 2:
                        first_total = float(annual[0]["total"])
                        last_total = float(annual[-1]["total"])
                        n_years = len(annual) - 1
                        if first_total > 0 and last_total > 0 and n_years > 0:
                            cagr = ((last_total / first_total) ** (1 / n_years) - 1) * 100
                            result["dividend_cagr_pct"] = round(cagr, 2)

                    # Consistency score
                    if years > 0:
                        result["consistency_pct"] = round(
                            len(annual) / years * 100, 1
                        )

                # Dividends actually received (from Investec transactions)
                sn = share.get("share_name")
                if sn:
                    cur.execute("""
                        SELECT EXTRACT(YEAR FROM date)::int AS year,
                               SUM(value) AS total_received,
                               COUNT(*) AS payments
                        FROM investec_investecjsetransaction
                        WHERE LOWER(share_name) = LOWER(%(sn)s)
                          AND type IN ('Dividend', 'Special Dividend', 'Foreign Dividend')
                          AND date >= %(cutoff)s
                        GROUP BY EXTRACT(YEAR FROM date)::int
                        ORDER BY year
                    """, {"sn": sn, "cutoff": cutoff})
                    result["dividends_received_by_year"] = [
                        _ser_row(dict(r)) for r in cur.fetchall()
                    ]

                    # Yield on cost (from portfolio)
                    sc = share.get("share_code")
                    if sc:
                        cur.execute("""
                            SELECT unit_cost, total_cost, annual_income_zar, quantity
                            FROM investec_investecjseportfolio
                            WHERE LOWER(share_code) = LOWER(%(sc)s)
                            ORDER BY date DESC LIMIT 1
                        """, {"sc": sc})
                        holding = cur.fetchone()
                        if holding:
                            holding = dict(holding)
                            unit_cost = float(holding.get("unit_cost") or 0)
                            annual_income = float(holding.get("annual_income_zar") or 0)
                            if unit_cost > 0 and annual_income > 0:
                                result["yield_on_cost_pct"] = round(
                                    annual_income / float(holding.get("total_cost") or 1) * 100, 2
                                )
                            result["current_holding"] = {
                                "quantity": holding.get("quantity"),
                                "unit_cost": unit_cost,
                                "annual_income_zar": annual_income,
                            }

                # Current TTM yield from company info
                if share.get("id"):
                    cur.execute("""
                        SELECT data FROM financial_investments_symbolinfo
                        WHERE symbol_id = %(sid)s
                        ORDER BY fetched_at DESC LIMIT 1
                    """, {"sid": share["id"]})
                    info_row = cur.fetchone()
                    if info_row:
                        info = info_row["data"] if isinstance(info_row["data"], dict) else json.loads(info_row["data"])
                        dy = info.get("dividendYield")
                        if dy:
                            result["current_dividend_yield_pct"] = round(float(dy) * 100, 2)
                        if info.get("dividendShare"):
                            result["current_dividend_per_share"] = info["dividendShare"]

                return result

    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
#  Tool schemas
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "name": "investment_lookup",
        "description": (
            "Look up a share/stock by code, symbol, or company name. "
            "Searches Investec JSE Share Name Mappings, Financial Investments Symbols, "
            "Investec JSE Portfolios, price data, Investec JSE Share Monthly Performances, "
            "and recent Investec JSE Transactions. Returns all available data for the share."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "search": {
                    "type": "string",
                    "description": "Share code, symbol, or company name (e.g. 'SHP', 'Capitec', 'NED.JO')",
                },
            },
            "required": ["search"],
        },
    },
    {
        "name": "investment_dividends",
        "description": (
            "Get dividend history for a share from yfinance data and Investec JSE Transactions. "
            "Shows dividend payments, TTM yield from Investec JSE Share Monthly Performances, "
            "and dividends actually received."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "search": {
                    "type": "string",
                    "description": "Share code, symbol, or company name",
                },
                "years": {
                    "type": "integer",
                    "description": "Years of dividend history (default 5)",
                },
            },
            "required": ["search"],
        },
    },
    {
        "name": "investment_analyst_data",
        "description": (
            "Get analyst recommendations, price targets, company info, earnings, "
            "and recent news for a share from the Financial Investments database."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "search": {
                    "type": "string",
                    "description": "Share code, symbol, or company name",
                },
            },
            "required": ["search"],
        },
    },
    {
        "name": "investment_upcoming_dividends",
        "description": (
            "Predict which shares will pay dividends in the next N months "
            "based on historical payment patterns. Shows whether the share is "
            "currently held in the Investec JSE Portfolio."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "months": {
                    "type": "integer",
                    "description": "Look-ahead window in months (default 3)",
                },
            },
        },
    },
    {
        "name": "investment_portfolio_summary",
        "description": (
            "Get a full summary of the Investec JSE Portfolio: total value, cost, "
            "P&L, annual income, yield, and all holdings sorted by value."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "investment_price_performance",
        "description": (
            "Calculate price performance and returns for a share over multiple periods: "
            "1 week, 1 month, 3 months, 6 months, YTD, 1 year, 2 years. "
            "Shows current price vs 52-week high/low. Use for 'how has X performed?' questions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "search": {
                    "type": "string",
                    "description": "Share code, symbol, or company name",
                },
            },
            "required": ["search"],
        },
    },
    {
        "name": "investment_portfolio_performance",
        "description": (
            "Calculate ROI (return on investment) for portfolio holdings including "
            "capital gains AND dividends received. Shows total return per holding. "
            "If search is given, filter to one share; otherwise show all holdings."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "search": {
                    "type": "string",
                    "description": "Optional: share code or company name to filter to one holding",
                },
            },
        },
    },
    {
        "name": "investment_compare",
        "description": (
            "Compare 2-5 shares side by side on key metrics: P/E ratio, dividend yield, "
            "market cap, sector, beta, ROE, profit margin, 52-week range, price-to-book. "
            "Use when the user asks to compare stocks."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of 2-5 share codes, symbols, or company names to compare",
                },
            },
            "required": ["symbols"],
        },
    },
    {
        "name": "investment_dividend_analysis",
        "description": (
            "Analyse dividend consistency and growth for a share: annual totals, "
            "year-over-year growth, CAGR, consistency score, dividends received, "
            "and yield on cost. Use for 'how are X dividends growing?' questions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "search": {
                    "type": "string",
                    "description": "Share code, symbol, or company name",
                },
                "years": {
                    "type": "integer",
                    "description": "Years of history to analyse (default 5)",
                },
            },
            "required": ["search"],
        },
    },
    {
        "name": "investment_screen",
        "description": (
            "Screen and filter shares by financial criteria: P/E ratio, dividend yield, "
            "sector, and whether currently held. Use to find stocks matching specific "
            "criteria like 'P/E below 10 and dividend yield above 5%'. "
            "Scans all tracked symbols with company info from yfinance."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "max_pe": {
                    "type": "number",
                    "description": "Maximum trailing P/E ratio (e.g. 10)",
                },
                "min_dividend_yield": {
                    "type": "number",
                    "description": "Minimum dividend yield as percentage (e.g. 5 for 5%)",
                },
                "min_pe": {
                    "type": "number",
                    "description": "Minimum trailing P/E ratio",
                },
                "max_dividend_yield": {
                    "type": "number",
                    "description": "Maximum dividend yield as percentage",
                },
                "sector": {
                    "type": "string",
                    "description": "Filter by sector name (partial match, e.g. 'Financial')",
                },
                "only_held": {
                    "type": "boolean",
                    "description": "If true, only show shares currently held in portfolio",
                },
            },
        },
    },
]

TOOL_FUNCTIONS = {
    "investment_lookup": investment_lookup,
    "investment_dividends": investment_dividends,
    "investment_analyst_data": investment_analyst_data,
    "investment_upcoming_dividends": investment_upcoming_dividends,
    "investment_portfolio_summary": investment_portfolio_summary,
    "investment_screen": investment_screen,
    "investment_price_performance": investment_price_performance,
    "investment_portfolio_performance": investment_portfolio_performance,
    "investment_compare": investment_compare,
    "investment_dividend_analysis": investment_dividend_analysis,
}
