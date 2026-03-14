"""
PostgreSQL tool implementations for the MCP server.

Read-only SQL access to klikk_financials_v4 (Xero GL, financial investments,
Investec portfolio) and klikk_bi_etl (BI metrics).
"""
from __future__ import annotations

import datetime
import decimal
import logging
from typing import Any

import psycopg2
import psycopg2.extras

from apps.ai_agent.agent.config import settings

log = logging.getLogger("mcp_tm1")

# ---------------------------------------------------------------------------
#  DSN configs
# ---------------------------------------------------------------------------

_FINANCIALS_DSN = dict(
    host=settings.pg_financials_host,
    port=settings.pg_financials_port,
    dbname=settings.pg_financials_db,
    user=settings.pg_financials_user,
    password=settings.pg_financials_password,
)

_BI_DSN = dict(
    host=settings.pg_bi_host,
    port=settings.pg_bi_port,
    dbname=settings.pg_bi_db,
    user=settings.pg_bi_user,
    password=settings.pg_bi_password,
)


def _serialize_value(v):
    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.isoformat()
    elif isinstance(v, decimal.Decimal):
        return float(v)
    return v


def _serialize_row(row: dict) -> dict:
    return {k: _serialize_value(v) for k, v in row.items()}


def _is_select_only(sql: str) -> bool:
    stripped = sql.strip().lstrip("(").upper()
    return stripped.startswith("SELECT") or stripped.startswith("WITH")


def _run_query(dsn: dict, sql: str, limit: int = 100) -> dict[str, Any]:
    if not _is_select_only(sql):
        return {"error": "Only SELECT queries are permitted."}
    try:
        with psycopg2.connect(**dsn) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(sql)
                rows = cur.fetchmany(limit)
                headers = [desc[0] for desc in cur.description] if cur.description else []
                return {
                    "headers": headers,
                    "rows": [[_serialize_value(v) for v in r] for r in rows],
                    "row_count": len(rows),
                    "truncated": len(rows) >= limit,
                }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
#  Tool functions
# ---------------------------------------------------------------------------

def pg_query_financials(sql: str, limit: int = 100) -> dict[str, Any]:
    """
    Run a read-only SELECT against klikk_financials_v4.
    Contains: Xero GL (xero_cube_xerotrailbalance), financial investments
    (financial_investments_symbol, _pricepoint, _dividend, etc.),
    Investec portfolio (investec_investecjseportfolio, _investecjsetransaction).
    Only SELECT statements permitted.
    """
    return _run_query(_FINANCIALS_DSN, sql, min(limit, 1000))


def pg_query_bi(sql: str, limit: int = 100) -> dict[str, Any]:
    """
    Run a read-only SELECT against klikk_bi_etl (BI ETL metrics).
    Only SELECT statements permitted.
    """
    return _run_query(_BI_DSN, sql, min(limit, 1000))


def pg_list_tables(database: str) -> dict[str, Any]:
    """
    List all tables in a PostgreSQL database with sizes and row counts.
    database: 'financials' for klikk_financials_v4, 'bi' for klikk_bi_etl.
    """
    dsn = _FINANCIALS_DSN if database == "financials" else _BI_DSN
    sql = """
        SELECT schemaname, relname AS tablename,
               pg_size_pretty(pg_total_relation_size(schemaname||'.'||relname)) AS size,
               n_live_tup AS approx_rows
        FROM pg_stat_user_tables
        ORDER BY schemaname, relname
    """
    return _run_query(dsn, sql, limit=200)


def pg_describe_table(database: str, table_name: str) -> dict[str, Any]:
    """
    Return column names and data types for a table.
    database: 'financials' or 'bi'.
    table_name: e.g. 'xero_cube_xerotrailbalance' or 'public.my_table'.
    """
    dsn = _FINANCIALS_DSN if database == "financials" else _BI_DSN
    parts = table_name.split(".", 1)
    schema = parts[0] if len(parts) == 2 else "public"
    tname = parts[1] if len(parts) == 2 else parts[0]
    sql = """
        SELECT column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
    """
    try:
        with psycopg2.connect(**dsn) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(sql, (schema, tname))
                rows = cur.fetchall()
                headers = [desc[0] for desc in cur.description]
                return {
                    "table": table_name,
                    "database": database,
                    "headers": headers,
                    "rows": [[_serialize_value(v) for v in r] for r in rows],
                    "column_count": len(rows),
                }
    except Exception as e:
        return {"error": str(e)}


def pg_get_xero_gl_sample(year: int, month: int, limit: int = 50) -> dict[str, Any]:
    """
    Fetch sample Xero GL trial balance entries for a given year/month.
    """
    sql = """
        SELECT year, month, organisation_id, account_code, account_name,
               contact_name, tracking_option_1, tracking_option_2,
               amount, balance_to_date
        FROM xero_cube_xerotrailbalance
        WHERE year = %(year)s AND month = %(month)s
        LIMIT %(limit)s
    """
    try:
        with psycopg2.connect(**_FINANCIALS_DSN) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(sql, {"year": year, "month": month, "limit": limit})
                rows = cur.fetchall()
                headers = [desc[0] for desc in cur.description]
                return {
                    "headers": headers,
                    "rows": [[_serialize_value(v) for v in r] for r in rows],
                    "row_count": len(rows),
                    "year": year,
                    "month": month,
                }
    except Exception as e:
        return {"error": str(e)}


def pg_get_share_data(symbol_search: str, include: str = "holdings,dividends,prices") -> dict[str, Any]:
    """
    Fetch detailed data for a specific share by symbol or name (fuzzy match).
    Returns holdings, dividends, prices, and/or transactions.
    """
    include_set = {s.strip().lower() for s in include.split(",")}
    result: dict[str, Any] = {}
    try:
        with psycopg2.connect(**_FINANCIALS_DSN) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("""
                    SELECT s.id, s.symbol, s.name, s.exchange, s.category,
                           m.share_name, m.share_code, m.company
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
                    LIMIT 5
                """, {"q": f"%{symbol_search}%"})
                symbols = [_serialize_row(dict(r)) for r in cur.fetchall()]
                if not symbols:
                    return {"error": f"No share found matching '{symbol_search}'"}
                result["symbols"] = symbols
                symbol_id = symbols[0]["id"]
                share_code = symbols[0].get("share_code")

                if "holdings" in include_set and share_code:
                    cur.execute("""
                        SELECT date, company, quantity, currency, unit_cost, total_cost,
                               price, total_value, exchange_rate, profit_loss,
                               portfolio_percent, annual_income_zar
                        FROM investec_investecjseportfolio
                        WHERE share_code = %(code)s ORDER BY date DESC LIMIT 12
                    """, {"code": share_code})
                    result["holdings"] = [_serialize_row(dict(r)) for r in cur.fetchall()]

                if "dividends" in include_set:
                    cur.execute("""
                        SELECT date, amount, currency
                        FROM financial_investments_dividend
                        WHERE symbol_id = %(sid)s ORDER BY date DESC LIMIT 50
                    """, {"sid": symbol_id})
                    result["dividends"] = [_serialize_row(dict(r)) for r in cur.fetchall()]

                if "prices" in include_set:
                    cur.execute("""
                        SELECT date, open, high, low, close, volume
                        FROM financial_investments_pricepoint
                        WHERE symbol_id = %(sid)s ORDER BY date DESC LIMIT 60
                    """, {"sid": symbol_id})
                    result["prices"] = [_serialize_row(dict(r)) for r in cur.fetchall()]

                if "transactions" in include_set and symbols[0].get("share_name"):
                    cur.execute("""
                        SELECT date, account_number, description, type, quantity,
                               value, value_per_share, value_calculated, dividend_ttm
                        FROM investec_investecjsetransaction
                        WHERE share_name = %(sn)s ORDER BY date DESC LIMIT 200
                    """, {"sn": symbols[0]["share_name"]})
                    result["transactions"] = [_serialize_row(dict(r)) for r in cur.fetchall()]

                if "performance" in include_set and symbols[0].get("share_name"):
                    cur.execute("""
                        SELECT date, dividend_type, investec_account, dividend_ttm,
                               closing_price, quantity, total_market_value, dividend_yield
                        FROM investec_investecjsesharemonthlyperformance
                        WHERE share_name = %(sn)s ORDER BY date DESC LIMIT 36
                    """, {"sn": symbols[0]["share_name"]})
                    result["monthly_performance"] = [_serialize_row(dict(r)) for r in cur.fetchall()]

                return result
    except Exception as e:
        return {"error": str(e)}


def pg_get_share_summary(limit: int = 50) -> dict[str, Any]:
    """Fetch summary of tracked shares with latest prices and Investec positions."""
    sql = """
        SELECT s.symbol, s.name, s.exchange, s.category,
               pp.date AS price_date, pp.close, pp.volume,
               ip.quantity, ip.total_cost, ip.total_value,
               ip.profit_loss, ip.portfolio_percent, ip.annual_income_zar
        FROM financial_investments_symbol s
        LEFT JOIN LATERAL (
            SELECT date, close, volume
            FROM financial_investments_pricepoint
            WHERE symbol_id = s.id ORDER BY date DESC LIMIT 1
        ) pp ON true
        LEFT JOIN investec_investecjsesharenamemapping m
            ON m.id = s.share_name_mapping_id
        LEFT JOIN LATERAL (
            SELECT quantity, total_cost, total_value, profit_loss,
                   portfolio_percent, annual_income_zar
            FROM investec_investecjseportfolio
            WHERE share_code = m.share_code ORDER BY date DESC LIMIT 1
        ) ip ON true
        ORDER BY s.symbol
        LIMIT %(limit)s
    """
    try:
        with psycopg2.connect(**_FINANCIALS_DSN) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(sql, {"limit": min(limit, 200)})
                rows = cur.fetchall()
                headers = [desc[0] for desc in cur.description] if cur.description else []
                return {
                    "headers": headers,
                    "rows": [[_serialize_value(v) for v in r] for r in rows],
                    "row_count": len(rows),
                }
    except Exception as e:
        return {"error": str(e)}
