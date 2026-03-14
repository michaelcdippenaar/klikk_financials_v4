"""
Skill: Financials PostgreSQL & vectorized data — Klikk Financials V4.

Access to:
- How data fits together: Xero → journals → trail balance → cube (PA SQL, APIs).
- Vectorized knowledge: semantic search over Klikk Financials AI-agent corpora (RAG).

Data model (from studying PA SQL and APIs in Klikk Financials V4):
- xero_data: XeroTransactionSource (raw), XeroJournalsSource, XeroJournals (parsed lines).
- xero_cube: XeroTrailBalance (aggregated by org/account/year/month/contact/tracking);
  XeroBalanceSheet, XeroPnlByTracking.
- Consolidation: single INSERT...SELECT from xero_data_xerojournals + xero_data_xerotransactionsource
  into xero_cube_xerotrailbalance (fiscal year/period from tenant).

APIs (prefix under auth_api_url): xero/data, xero/cube (trail-balance, line-items, pnl-summary),
xero/validation, api/ai-agent (corpora, vectorize, search).
"""
from __future__ import annotations

import os
import sys
from typing import Any

import requests

import logging

from apps.ai_agent.agent.config import settings

log = logging.getLogger('ai_agent')

# ---------------------------------------------------------------------------
#  Data guide (from PA SQL and Financials APIs)
# ---------------------------------------------------------------------------

FINANCIALS_DATA_GUIDE = {
    "data_flow": (
        "Xero API → transaction/journal sources (xero_data) → XeroJournals (parsed) → "
        "consolidate_journals() (single INSERT...SELECT) → XeroTrailBalance → "
        "Balance sheet and P&L views."
    ),
    "key_tables": {
        "xero_core": "XeroTenant (organisation).",
        "xero_metadata": "XeroAccount, XeroContacts, XeroTracking (accounts, contacts, tracking options).",
        "xero_data": (
            "XeroTransactionSource (invoices, bank txns; raw JSON), "
            "XeroJournalsSource (raw journal payloads), "
            "XeroJournals (parsed lines: account_id, date, amount, contact_id, tracking1_id, tracking2_id, transaction_source_id), "
            "XeroDocument (attachments)."
        ),
        "xero_cube": (
            "XeroTrailBalance (organisation_id, account_id, date, year, month, fin_year, fin_period, "
            "contact_id, tracking1_id, tracking2_id, amount, debit, credit, tax_amount, balance_to_date). "
            "XeroBalanceSheet (derived from trail balance). "
            "XeroPnlByTracking (P&L by tracking option, per org/tracking/account/year/month)."
        ),
    },
    "pa_sql_pattern": (
        "Consolidate journals (XeroTrailBalanceManager.consolidate_journals): "
        "INSERT INTO xero_cube_xerotrailbalance (...) SELECT j.organisation_id, j.account_id, "
        "make_date(EXTRACT(YEAR FROM j.date)::int, EXTRACT(MONTH FROM j.date)::int, 1), "
        "EXTRACT(YEAR/MONTH FROM j.date), fin_year/fin_period from tenant fiscal start, "
        "COALESCE(j.contact_id, ts.contact_id), j.tracking1_id, j.tracking2_id, "
        "SUM(j.amount), SUM(CASE WHEN j.amount>0 THEN j.amount ELSE 0 END), "
        "SUM(CASE WHEN j.amount<0 THEN j.amount ELSE 0 END), SUM(j.tax_amount), NULL "
        "FROM xero_data_xerojournals j "
        "LEFT JOIN xero_data_xerotransactionsource ts ON j.transaction_source_id = ts.transactions_id "
        "WHERE j.organisation_id = %s [and optional period filter] "
        "GROUP BY org, account, year, month, contact, tracking1, tracking2 HAVING SUM(j.amount) != 0."
    ),
    "apis_summary": {
        "xero/data": "POST update/journals/, process/journals/, sync/documents/; GET documents/by-transaction/<id>/.",
        "xero/cube": "POST process/ (trail balance rebuild); GET summary/, trail-balance/, line-items/, pnl-summary/, account-summary/; POST import-pnl-by-tracking/.",
        "xero/validation": "POST balance-sheet/, reconcile/, import-profit-loss/, compare-profit-loss/, export-trail-balance/, export-profit-loss/.",
        "xero/metadata": "POST update/; GET accounts/search/.",
        "xero/core": "GET tenants/.",
        "api/ai-agent": "GET/POST corpora/; POST corpora/<id>/vectorize/, corpora/<id>/search/ (semantic search over vectorized docs).",
        "api/planning-analytics": "POST pipeline/run/, tm1/execute/; GET tm1/processes/.",
    },
    "postgres_databases": {
        "financials": "klikk_financials_v4 — Xero GL, trail balance, cube tables, financial investments (shares/prices), Investec portfolio/transactions. Use pg_query_financials for SELECT.",
        "bi": "klikk_bi_etl — BI ETL metrics, RAG vector store. Use pg_query_bi for SELECT.",
    },
    "share_tables": {
        "financial_investments_symbol": "Ticker reference (symbol, name, exchange, category). Links to Investec via share_name_mapping_id.",
        "financial_investments_pricepoint": "Daily OHLCV prices (symbol_id FK, date, open, high, low, close, volume).",
        "financial_investments_dividend": "Dividend payments per symbol/date.",
        "financial_investments_split": "Stock split events per symbol/date.",
        "financial_investments_symbolinfo": "Company info JSONB from yfinance (1:1 with symbol).",
        "financial_investments_financialstatement": "Income stmt, balance sheet, cashflow JSONB (symbol_id, statement_type, freq).",
        "financial_investments_earningsreport": "Earnings reports JSONB.",
        "financial_investments_earningsestimate": "Analyst earnings estimates JSONB (1:1).",
        "financial_investments_analystrecommendation": "Buy/Hold/Sell recommendations JSONB (1:1).",
        "financial_investments_analystpricetarget": "Analyst price targets JSONB (1:1).",
        "financial_investments_ownershipsnapshot": "Institutional/insider holders JSONB.",
        "financial_investments_newsitem": "News articles per symbol.",
        "investec_investecjseportfolio": "Investec holdings export (point-in-time snapshots: share_code, company, date, quantity, currency, unit_cost, total_cost, price, total_value, exchange_rate, profit_loss, portfolio_percent, annual_income_zar).",
        "investec_investecjsetransaction": "Investec transaction export (share_name, date, account_number, description, type=[Buy|Sell|Dividend|Fee|Broker Fee|Special Dividend|Foreign Dividend|Dividend Tax], quantity, value, value_per_share, value_calculated, dividend_ttm).",
        "investec_investecjsesharenamemapping": "Maps share_name/share_name2/share_name3 <-> share_code <-> company. Auto-created on portfolio import. Links to financial_investments_symbol.",
        "investec_investecjsesharemonthlyperformance": "Monthly performance metrics per share (share_name, date, dividend_type, investec_account, dividend_ttm, closing_price, quantity, total_market_value, dividend_yield).",
    },
    "column_dimension_map": {
        "xero_cube_xerotrailbalance -> gl_src_trial_balance": {
            "year": "year dimension (2014-2030)",
            "month": "month dimension (Jul-Jun fiscal + consolidators)",
            "organisation_id": "entity dimension (Xero GUIDs, NOT names)",
            "account_id": "account dimension (382 elements, code/name/type attrs)",
            "contact_id": "contact dimension (1,152 elements)",
            "tracking1_id": "tracking_1 dimension (business segment: property, event equipment, financial investments)",
            "tracking2_id": "tracking_2 dimension (secondary tracking)",
            "amount/debit/credit": "measure_gl_src_trial_balance measures",
        },
    },
    "tracking_meaning": (
        "tracking_1 = business segment from Xero tracking categories: "
        "Property, Event Equipment, Financial Investments. "
        "tracking_2 = secondary tracking category. "
        "Resolved via XeroTenant.get_tracking_slot(TrackingCategoryID) -> slot 1 or 2."
    ),
}


def _financials_request(method: str, path: str, json_body: dict | None = None, timeout: int = 30) -> dict[str, Any]:
    """Call Klikk Financials API. path is relative to base, e.g. 'api/ai-agent/corpora/'."""
    base = (settings.auth_api_url or "").rstrip("/")
    if not base:
        return {"error": "auth_api_url (Klikk Financials base URL) is not configured."}
    token = (settings.financials_api_token or "").strip()
    if not token:
        return {"error": "financials_api_token is not set. Set FINANCIALS_API_TOKEN in .env to use vectorized search."}
    url = f"{base}/{path.lstrip('/')}"
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    try:
        resp = requests.request(method, url, headers=headers, json=json_body, timeout=timeout, verify=False)
        if resp.status_code >= 400:
            return {"error": f"Financials API {resp.status_code}: {resp.text[:500]}"}
        return resp.json() if resp.content else {}
    except requests.exceptions.RequestException as e:
        log.warning("Financials API request failed: %s", e, extra={"tool": "financials_data"})
        return {"error": f"Request failed: {e}"}


def financials_data_guide() -> dict[str, Any]:
    """
    Return how PostgreSQL and API data fit together in Klikk Financials (PA SQL, tables, APIs).
    Use this before writing SQL or calling APIs so the agent understands the data model.
    """
    return {
        "guide": FINANCIALS_DATA_GUIDE,
        "note": "Use pg_list_tables(database='financials'|'bi'), pg_describe_table(), pg_query_financials(sql) for live queries. Use financials_vector_search() for semantic search over vectorized docs.",
    }


def financials_list_corpora() -> dict[str, Any]:
    """
    List vectorized knowledge corpora in Klikk Financials AI-agent.
    Requires financials_api_token. Use financials_vector_search(corpus_id, query) to search a corpus.
    """
    out = _financials_request("GET", "api/ai-agent/corpora/")
    if "error" in out:
        return out
    if isinstance(out, list):
        return {"corpora": out, "count": len(out)}
    if isinstance(out, dict) and "corpora" not in out:
        return {"corpora": [], "count": 0, "raw": out}
    return out


def financials_vector_search(corpus_id: int, query: str, top_k: int = 6) -> dict[str, Any]:
    """
    Semantic search over a vectorized corpus in Klikk Financials (RAG).
    Corpora are built from system documents and vectorized via api/ai-agent/corpora/<id>/vectorize/.
    Requires financials_api_token. Use financials_list_corpora() to get corpus_id.
    """
    query = (query or "").strip()
    if not query:
        return {"error": "query is required."}
    top_k = max(1, min(int(top_k), 20))
    out = _financials_request(
        "POST",
        f"api/ai-agent/corpora/{corpus_id}/search/",
        json_body={"query": query, "top_k": top_k},
    )
    if "error" in out:
        return out
    return {"corpus_id": corpus_id, "query": query, **out}


# ---------------------------------------------------------------------------
#  Tool schemas (loaded by tool_registry)
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "name": "financials_data_guide",
        "description": (
            "Return how PostgreSQL and API data fit together in Klikk Financials: "
            "Xero → journals → trail balance → cube; key tables (xero_data, xero_cube); "
            "PA consolidate_journals SQL pattern; main API endpoints. Use before writing SQL or calling APIs."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "financials_list_corpora",
        "description": (
            "List vectorized knowledge corpora in Klikk Financials AI-agent. "
            "Requires FINANCIALS_API_TOKEN. Use financials_vector_search(corpus_id, query) to search."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "financials_vector_search",
        "description": (
            "Semantic search over a vectorized corpus (RAG) in Klikk Financials. "
            "Use financials_list_corpora() to get corpus_id. Requires FINANCIALS_API_TOKEN."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "corpus_id": {"type": "integer", "description": "Corpus ID from financials_list_corpora."},
                "query": {"type": "string", "description": "Natural language or keyword query."},
                "top_k": {"type": "integer", "description": "Max hits to return (default 6, max 20)."},
            },
            "required": ["corpus_id", "query"],
        },
    },
]

TOOL_FUNCTIONS = {
    "financials_data_guide": financials_data_guide,
    "financials_list_corpora": financials_list_corpora,
    "financials_vector_search": financials_vector_search,
}
