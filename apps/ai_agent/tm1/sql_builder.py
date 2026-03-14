"""
SQL Query Builder — translates natural language into SQL queries
against klikk_financials_v4 and executes them.

Knows the database schema, table relationships, and common query patterns.
Builds safe, read-only SELECT queries.
"""
from __future__ import annotations

import re
import logging
from typing import Any

from apps.ai_agent.tm1.pg_tools import pg_query_financials

log = logging.getLogger("mcp_tm1")

# ---------------------------------------------------------------------------
#  Schema knowledge — tables, columns, relationships
# ---------------------------------------------------------------------------

SCHEMA = {
    # ── Xero ──
    "v_xero_journal_drill": {
        "description": "Pre-joined view: journals + accounts + contacts + tracking with fiscal year. BEST for Xero transaction queries.",
        "columns": {
            "tenant_id": "UUID — Xero tenant/org",
            "account_id": "UUID — account",
            "account_code": "str — Xero account code",
            "year": "int — calendar year",
            "month": "int — calendar month 1-12",
            "fin_year": "int — fiscal year",
            "fin_period": "int — fiscal period 1-12",
            "fiscal_year_start_month": "int — org's FY start month",
            "contact_id": "UUID",
            "contact_name": "str — supplier/customer name",
            "tracking1_id": "UUID",
            "tracking1_option": "str — tracking category 1 value",
            "tracking2_id": "UUID",
            "tracking2_option": "str — tracking category 2 value",
            "id": "UUID — journal line ID",
            "journal_id": "UUID",
            "journal_number": "int",
            "journal_type": "str — ACCREC/ACCPAY/CASHPAID/etc",
            "date": "date",
            "description": "str — line description",
            "reference": "str — document reference",
            "amount": "decimal — signed amount",
            "debit": "decimal",
            "credit": "decimal",
            "tax_amount": "decimal",
            "transaction_source_type": "str",
        },
        "keywords": ["journal", "transaction", "xero", "expense", "income", "revenue",
                      "supplier", "customer", "contact", "tracking", "debit", "credit",
                      "account code", "journal type", "invoice", "payment"],
    },
    "xero_cube_xerotrailbalance": {
        "description": "Xero trial balance cube — monthly balances by account/contact/tracking.",
        "columns": {
            "year": "int", "month": "int", "fin_year": "int", "fin_period": "int",
            "fiscal_year_start_month": "int",
            "account_code": "str", "account_name": "str", "account_type": "str",
            "reporting_code": "str",
            "contact_name": "str",
            "tracking_option_1": "str", "tracking_option_2": "str",
            "amount": "decimal", "debit": "decimal", "credit": "decimal",
            "tax_amount": "decimal", "balance_to_date": "decimal",
            "organisation_id": "UUID",
        },
        "keywords": ["trial balance", "tb", "balance", "account balance", "monthly balance",
                      "balance to date", "reporting code"],
    },
    "xero_metadata_xeroaccount": {
        "description": "Chart of accounts — account codes, names, types, reporting codes.",
        "columns": {
            "account_id": "UUID PK", "code": "str", "name": "str",
            "type": "str — REVENUE/EXPENSE/ASSET/LIABILITY/EQUITY",
            "reporting_code": "str", "tax_type": "str",
            "description": "str", "status": "str",
        },
        "keywords": ["chart of accounts", "account list", "account type", "reporting code"],
    },
    "xero_metadata_xerocontacts": {
        "description": "Xero contacts (customers/suppliers).",
        "columns": {
            "contacts_id": "UUID PK", "name": "str",
            "first_name": "str", "last_name": "str",
            "email_address": "str", "is_supplier": "bool", "is_customer": "bool",
        },
        "keywords": ["contact", "supplier", "customer", "vendor"],
    },
    "xero_core_xerotenant": {
        "description": "Xero tenants/organisations.",
        "columns": {
            "tenant_id": "UUID PK", "tenant_name": "str",
            "fiscal_year_start_month": "int",
        },
        "keywords": ["tenant", "organisation", "org"],
    },

    # ── Investec JSE ──
    "investec_investecjseportfolio": {
        "description": "Point-in-time portfolio snapshots from Investec.",
        "columns": {
            "date": "date", "company": "str", "share_code": "str",
            "quantity": "decimal", "currency": "str",
            "unit_cost": "decimal", "total_cost": "decimal",
            "price": "decimal", "total_value": "decimal",
            "exchange_rate": "decimal", "profit_loss": "decimal",
            "portfolio_percent": "decimal", "annual_income_zar": "decimal",
        },
        "keywords": ["portfolio", "holdings", "position", "profit loss", "allocation"],
    },
    "investec_investecjsetransaction": {
        "description": "JSE buy/sell/dividend transactions from Investec.",
        "columns": {
            "date": "date", "share_name": "str", "account_number": "str",
            "description": "str", "type": "str — BUY/SELL/DIVIDEND/FEE",
            "quantity": "decimal", "value": "decimal",
            "value_per_share": "decimal", "value_calculated": "decimal",
            "dividend_ttm": "decimal",
        },
        "keywords": ["buy", "sell", "jse transaction", "share transaction",
                      "dividend received", "fee"],
    },
    "investec_investecjsesharemonthlyperformance": {
        "description": "Monthly share performance — TTM dividends, yields, market value.",
        "columns": {
            "share_name": "str", "date": "date",
            "dividend_type": "str", "investec_account": "str",
            "dividend_ttm": "decimal", "closing_price": "decimal",
            "quantity": "decimal", "total_market_value": "decimal",
            "dividend_yield": "decimal",
        },
        "keywords": ["monthly performance", "dividend yield", "market value", "ttm"],
    },
    "investec_investecjsesharenamemapping": {
        "description": "Maps Investec share names to symbols and companies.",
        "columns": {
            "id": "int PK", "share_name": "str", "share_name2": "str",
            "share_name3": "str", "company": "str", "share_code": "str",
        },
        "keywords": ["share mapping", "share name", "symbol mapping"],
    },

    # ── Financial Investments (yfinance market data) ──
    "financial_investments_symbol": {
        "description": "Tracked market symbols with metadata.",
        "columns": {
            "id": "int PK", "symbol": "str — e.g. ABG.JO",
            "name": "str", "exchange": "str", "category": "str",
            "share_name_mapping_id": "FK → investec_investecjsesharenamemapping",
        },
        "keywords": ["symbol", "ticker", "exchange", "market"],
    },
    "financial_investments_pricepoint": {
        "description": "Daily OHLCV price history from yfinance.",
        "columns": {
            "symbol_id": "FK → financial_investments_symbol",
            "date": "date", "open": "decimal", "high": "decimal",
            "low": "decimal", "close": "decimal", "volume": "bigint",
        },
        "keywords": ["price", "close", "open", "high", "low", "volume", "ohlc", "price history"],
    },
    "financial_investments_dividend": {
        "description": "Dividend history from yfinance.",
        "columns": {
            "symbol_id": "FK → financial_investments_symbol",
            "date": "date", "amount": "decimal", "currency": "str",
        },
        "keywords": ["dividend history", "dividend amount", "dps"],
    },

    # ── Investec Bank ──
    "investec_investecbankaccount": {
        "description": "Investec bank accounts.",
        "columns": {
            "account_id": "str PK", "account_number": "str",
            "account_name": "str", "product_name": "str",
        },
        "keywords": ["bank account", "investec account"],
    },
    "investec_investecbanktransaction": {
        "description": "Investec bank transactions.",
        "columns": {
            "type": "str", "transaction_type": "str",
            "description": "str", "amount": "decimal",
            "posting_date": "date", "transaction_date": "date",
            "running_balance": "decimal",
        },
        "keywords": ["bank transaction", "bank payment", "bank transfer", "running balance"],
    },
}

# ---------------------------------------------------------------------------
#  Table selection — match query to relevant tables
# ---------------------------------------------------------------------------

def _match_tables(query: str) -> list[str]:
    """Score tables against the query and return top matches."""
    query_lower = query.lower()
    scored = []
    for table, info in SCHEMA.items():
        score = 0
        for kw in info["keywords"]:
            if kw in query_lower:
                score += 10
        # Bonus for exact table name mention
        if table in query_lower:
            score += 20
        # Check column names
        for col in info["columns"]:
            if col in query_lower:
                score += 3
        if score > 0:
            scored.append((table, score))
    scored.sort(key=lambda x: -x[1])
    return [t[0] for t in scored[:3]]


def _get_schema_context(tables: list[str]) -> str:
    """Build schema context string for the matched tables."""
    parts = []
    for table in tables:
        info = SCHEMA[table]
        cols = ", ".join(f"{c} ({t})" for c, t in info["columns"].items())
        parts.append(f"-- {table}: {info['description']}\n-- Columns: {cols}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
#  Query patterns — common queries with templates
# ---------------------------------------------------------------------------

QUERY_PATTERNS = [
    {
        "keywords": ["top", "largest", "biggest", "highest"],
        "pattern": "ranking/top-N query",
        "hint": "Use ORDER BY ... DESC LIMIT N",
    },
    {
        "keywords": ["total", "sum", "aggregate", "by month", "by year", "group"],
        "pattern": "aggregation query",
        "hint": "Use SUM/COUNT/AVG with GROUP BY",
    },
    {
        "keywords": ["trend", "over time", "monthly", "yearly", "history"],
        "pattern": "time series query",
        "hint": "GROUP BY date/month/year, ORDER BY date",
    },
    {
        "keywords": ["compare", "vs", "versus", "difference", "change"],
        "pattern": "comparison query",
        "hint": "Use subqueries or CASE WHEN for period comparison",
    },
]


# ---------------------------------------------------------------------------
#  Tool functions
# ---------------------------------------------------------------------------

def sql_build_query(
    question: str,
    tables: list[str] | None = None,
    execute: bool = True,
    limit: int = 100,
) -> dict[str, Any]:
    """
    Build a SQL query from a natural language question.

    Analyses the question, identifies relevant tables, builds an appropriate
    SELECT query, and optionally executes it.

    question: Natural language, e.g. "total expenses by account for 2025"
    tables: Override auto-detected tables (optional)
    execute: If True (default), also run the query and return results
    limit: Max rows (default 100)
    """
    question_lower = question.lower()

    # 1. Match tables
    matched_tables = tables if tables else _match_tables(question)
    if not matched_tables:
        # Default to the journal drill view for general financial queries
        matched_tables = ["v_xero_journal_drill"]

    schema_context = _get_schema_context(matched_tables)

    # 2. Detect query pattern
    detected_patterns = []
    for p in QUERY_PATTERNS:
        if any(kw in question_lower for kw in p["keywords"]):
            detected_patterns.append(p["pattern"])

    # 3. Build SQL based on common patterns
    sql = _build_sql(question_lower, matched_tables, limit)

    result: dict[str, Any] = {
        "question": question,
        "matched_tables": matched_tables,
        "schema": schema_context,
        "sql": sql,
        "patterns_detected": detected_patterns or ["general query"],
    }

    # 4. Execute if requested
    if execute and sql:
        query_result = pg_query_financials(sql, limit=limit)
        result["result"] = query_result
        if "error" in query_result:
            result["hint"] = (
                "Query failed. Common fixes:\n"
                "- Check column names with pg_describe_table\n"
                "- Use the schema info above to fix column references\n"
                "- Try a simpler query first"
            )

    return result


def _build_sql(question: str, tables: list[str], limit: int) -> str:
    """Build SQL from question and matched tables. Returns best-effort SQL."""
    primary = tables[0]
    cols = list(SCHEMA[primary]["columns"].keys())

    # ── Xero journal queries ──
    if primary == "v_xero_journal_drill":
        return _build_journal_query(question, limit)

    # ── Trial balance queries ──
    if primary == "xero_cube_xerotrailbalance":
        return _build_tb_query(question, limit)

    # ── Portfolio queries ──
    if primary == "investec_investecjseportfolio":
        return _build_portfolio_query(question, limit)

    # ── Transaction queries ──
    if primary == "investec_investecjsetransaction":
        return _build_jse_transaction_query(question, limit)

    # ── Price queries ──
    if primary == "financial_investments_pricepoint":
        return _build_price_query(question, limit)

    # ── Dividend queries ──
    if primary == "financial_investments_dividend":
        return _build_dividend_query(question, limit)

    # ── Bank transaction queries ──
    if primary == "investec_investecbanktransaction":
        return _build_bank_query(question, limit)

    # ── Default: select all columns ──
    return f"SELECT * FROM {primary} LIMIT {limit}"


def _extract_year(question: str) -> str | None:
    m = re.search(r'\b(20\d{2})\b', question)
    return m.group(1) if m else None


def _extract_month(question: str) -> int | None:
    months = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
        "january": 1, "february": 2, "march": 3, "april": 4,
        "june": 6, "july": 7, "august": 8, "september": 9,
        "october": 10, "november": 11, "december": 12,
    }
    for name, num in months.items():
        if name in question:
            return num
    return None


def _extract_limit(question: str, default: int) -> int:
    m = re.search(r'top\s+(\d+)', question)
    if m:
        return int(m.group(1))
    return default


def _extract_entity_name(question: str) -> str | None:
    """Extract a contact/entity name from patterns like 'for X', 'related to X', 'from X'."""
    # Remove known keywords to isolate the entity name
    patterns = [
        r'(?:related to|transactions? (?:for|from|with|to)|paid to|received from|invoices? (?:from|for)|payments? to)\s+["\']?(.+?)["\']?\s*(?:in |for |during |\d{4}|$)',
        r'(?:for|from|with|to)\s+["\']?([A-Z][A-Za-z\s&\'-]+?)(?:\s+in\b|\s+for\b|\s+during\b|\s+\d{4}|\s*$)',
    ]
    for pattern in patterns:
        m = re.search(pattern, question, re.IGNORECASE)
        if m:
            name = m.group(1).strip().rstrip('.,;')
            # Skip generic words that aren't entity names
            skip = {"each", "all", "the", "this", "that", "every", "any",
                    "account", "month", "year", "period", "tracking"}
            if name.lower() not in skip and len(name) > 2:
                return name
    return None


def _build_journal_query(question: str, limit: int) -> str:
    year = _extract_year(question)
    month = _extract_month(question)
    n = _extract_limit(question, limit)
    entity = _extract_entity_name(question)

    where = []
    if year:
        where.append(f"year = {year}")
    if month:
        where.append(f"month = {month}")
    if entity:
        # Use ILIKE for case-insensitive fuzzy match on contact_name
        safe_entity = entity.replace("'", "''")
        where.append(f"contact_name ILIKE '%{safe_entity}%'")

    where_clause = f"WHERE {' AND '.join(where)}" if where else ""

    # If we have an entity filter, default to showing all their transactions
    if entity and not any(kw in question for kw in [
        "total", "sum", "by account", "by contact", "by month", "monthly",
        "by tracking", "largest", "biggest", "top", "highest"
    ]):
        return (
            f"SELECT date, account_code, contact_name, description, "
            f"amount, debit, credit, journal_type, reference "
            f"FROM v_xero_journal_drill {where_clause} "
            f"ORDER BY date DESC LIMIT {n}"
        )

    # Detect aggregation patterns
    if any(kw in question for kw in ["total", "sum", "by account", "per account"]):
        return (
            f"SELECT account_code, SUM(amount) AS total_amount, "
            f"SUM(debit) AS total_debit, SUM(credit) AS total_credit, "
            f"COUNT(*) AS transaction_count "
            f"FROM v_xero_journal_drill {where_clause} "
            f"GROUP BY account_code ORDER BY ABS(SUM(amount)) DESC LIMIT {n}"
        )

    if any(kw in question for kw in ["by contact", "per contact", "by supplier", "by customer"]):
        return (
            f"SELECT contact_name, SUM(amount) AS total_amount, "
            f"COUNT(*) AS transaction_count "
            f"FROM v_xero_journal_drill {where_clause} "
            f"GROUP BY contact_name ORDER BY ABS(SUM(amount)) DESC LIMIT {n}"
        )

    if any(kw in question for kw in ["by month", "monthly", "per month"]):
        return (
            f"SELECT year, month, SUM(amount) AS total_amount, "
            f"SUM(debit) AS total_debit, SUM(credit) AS total_credit "
            f"FROM v_xero_journal_drill {where_clause} "
            f"GROUP BY year, month ORDER BY year, month LIMIT {n}"
        )

    if any(kw in question for kw in ["by tracking", "tracking category", "department", "cost centre"]):
        return (
            f"SELECT tracking1_option, SUM(amount) AS total_amount, "
            f"COUNT(*) AS transaction_count "
            f"FROM v_xero_journal_drill {where_clause} "
            f"AND tracking1_option IS NOT NULL "
            f"GROUP BY tracking1_option ORDER BY ABS(SUM(amount)) DESC LIMIT {n}"
        )

    if any(kw in question for kw in ["largest", "biggest", "top", "highest"]):
        return (
            f"SELECT date, account_code, contact_name, description, amount, "
            f"journal_type, reference "
            f"FROM v_xero_journal_drill {where_clause} "
            f"ORDER BY ABS(amount) DESC LIMIT {n}"
        )

    # Default: recent journals
    order = "ORDER BY date DESC" if not where else "ORDER BY date DESC"
    return (
        f"SELECT date, account_code, contact_name, description, "
        f"amount, debit, credit, journal_type "
        f"FROM v_xero_journal_drill {where_clause} {order} LIMIT {n}"
    )


def _build_tb_query(question: str, limit: int) -> str:
    year = _extract_year(question)
    month = _extract_month(question)
    n = _extract_limit(question, limit)
    entity = _extract_entity_name(question)

    where = []
    if year:
        where.append(f"year = {year}")
    if entity:
        safe_entity = entity.replace("'", "''")
        where.append(f"contact_name ILIKE '%{safe_entity}%'")
    if month:
        where.append(f"month = {month}")
    where_clause = f"WHERE {' AND '.join(where)}" if where else ""

    if any(kw in question for kw in ["by account", "per account", "account summary"]):
        return (
            f"SELECT account_code, account_name, account_type, "
            f"SUM(amount) AS total_amount, SUM(balance_to_date) AS balance "
            f"FROM xero_cube_xerotrailbalance {where_clause} "
            f"GROUP BY account_code, account_name, account_type "
            f"ORDER BY ABS(SUM(amount)) DESC LIMIT {n}"
        )

    return (
        f"SELECT year, month, account_code, account_name, account_type, "
        f"amount, balance_to_date "
        f"FROM xero_cube_xerotrailbalance {where_clause} "
        f"ORDER BY year DESC, month DESC LIMIT {n}"
    )


def _build_portfolio_query(question: str, limit: int) -> str:
    n = _extract_limit(question, limit)

    if any(kw in question for kw in ["latest", "current", "now"]):
        return (
            f"SELECT company, share_code, quantity, price, total_value, "
            f"profit_loss, portfolio_percent, annual_income_zar, date "
            f"FROM investec_investecjseportfolio "
            f"WHERE date = (SELECT MAX(date) FROM investec_investecjseportfolio) "
            f"ORDER BY total_value DESC LIMIT {n}"
        )

    if any(kw in question for kw in ["total", "sum", "value"]):
        return (
            f"SELECT date, SUM(total_value) AS total_portfolio_value, "
            f"SUM(total_cost) AS total_cost, SUM(profit_loss) AS total_pnl, "
            f"SUM(annual_income_zar) AS total_annual_income, "
            f"COUNT(*) AS positions "
            f"FROM investec_investecjseportfolio "
            f"GROUP BY date ORDER BY date DESC LIMIT {n}"
        )

    return (
        f"SELECT date, company, share_code, quantity, price, total_value, "
        f"profit_loss, portfolio_percent "
        f"FROM investec_investecjseportfolio "
        f"ORDER BY date DESC, total_value DESC LIMIT {n}"
    )


def _build_jse_transaction_query(question: str, limit: int) -> str:
    n = _extract_limit(question, limit)
    year = _extract_year(question)

    where = []
    if year:
        where.append(f"EXTRACT(YEAR FROM date) = {year}")

    if any(kw in question for kw in ["dividend"]):
        where.append("type ILIKE '%dividend%'")
    elif any(kw in question for kw in ["buy", "purchase"]):
        where.append("type ILIKE '%buy%'")
    elif any(kw in question for kw in ["sell", "sold"]):
        where.append("type ILIKE '%sell%'")

    where_clause = f"WHERE {' AND '.join(where)}" if where else ""

    if any(kw in question for kw in ["total", "sum", "by share"]):
        return (
            f"SELECT share_name, type, COUNT(*) AS count, "
            f"SUM(value) AS total_value, SUM(quantity) AS total_quantity "
            f"FROM investec_investecjsetransaction {where_clause} "
            f"GROUP BY share_name, type ORDER BY ABS(SUM(value)) DESC LIMIT {n}"
        )

    return (
        f"SELECT date, share_name, type, quantity, value, "
        f"value_per_share, description "
        f"FROM investec_investecjsetransaction {where_clause} "
        f"ORDER BY date DESC LIMIT {n}"
    )


def _build_price_query(question: str, limit: int) -> str:
    n = _extract_limit(question, limit)
    return (
        f"SELECT s.symbol, s.name, p.date, p.open, p.high, p.low, p.close, p.volume "
        f"FROM financial_investments_pricepoint p "
        f"JOIN financial_investments_symbol s ON s.id = p.symbol_id "
        f"ORDER BY p.date DESC LIMIT {n}"
    )


def _build_dividend_query(question: str, limit: int) -> str:
    n = _extract_limit(question, limit)
    year = _extract_year(question)

    where = f"WHERE EXTRACT(YEAR FROM d.date) = {year}" if year else ""

    if any(kw in question for kw in ["total", "sum", "by share", "per share"]):
        return (
            f"SELECT s.symbol, s.name, COUNT(*) AS payments, "
            f"SUM(d.amount) AS total_dividends, MIN(d.date) AS first, MAX(d.date) AS last "
            f"FROM financial_investments_dividend d "
            f"JOIN financial_investments_symbol s ON s.id = d.symbol_id {where} "
            f"GROUP BY s.symbol, s.name ORDER BY SUM(d.amount) DESC LIMIT {n}"
        )

    return (
        f"SELECT s.symbol, s.name, d.date, d.amount, d.currency "
        f"FROM financial_investments_dividend d "
        f"JOIN financial_investments_symbol s ON s.id = d.symbol_id {where} "
        f"ORDER BY d.date DESC LIMIT {n}"
    )


def _build_bank_query(question: str, limit: int) -> str:
    n = _extract_limit(question, limit)
    year = _extract_year(question)

    where = []
    if year:
        where.append(f"EXTRACT(YEAR FROM posting_date) = {year}")

    where_clause = f"WHERE {' AND '.join(where)}" if where else ""

    if any(kw in question for kw in ["total", "sum", "by type"]):
        return (
            f"SELECT transaction_type, COUNT(*) AS count, "
            f"SUM(amount) AS total_amount "
            f"FROM investec_investecbanktransaction {where_clause} "
            f"GROUP BY transaction_type ORDER BY ABS(SUM(amount)) DESC LIMIT {n}"
        )

    return (
        f"SELECT posting_date, transaction_type, description, amount, running_balance "
        f"FROM investec_investecbanktransaction {where_clause} "
        f"ORDER BY posting_date DESC LIMIT {n}"
    )


def sql_list_tables_schema() -> dict[str, Any]:
    """
    List all known tables with their descriptions, columns, and common query keywords.
    Use this to understand what data is available before building a query.
    """
    tables = []
    for table, info in SCHEMA.items():
        tables.append({
            "table": table,
            "description": info["description"],
            "columns": list(info["columns"].keys()),
            "keywords": info["keywords"],
        })
    return {"tables": tables, "count": len(tables)}
