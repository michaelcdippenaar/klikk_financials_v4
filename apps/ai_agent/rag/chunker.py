"""
Document chunking strategies for RAG indexing.

Rules per document type:
- Markdown instructions: split on ## headings (~800 tokens max per chunk)
- current_model_state.md: one chunk per TM1 object (dim/cube/process section)
- TM1 dimension (from live API): one chunk per dimension
- PostgreSQL table schema: one chunk per table with columns, FKs, sample rows
- Data context: hardcoded chunks for relationships, pipelines, dimension mapping
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Generator


@dataclass
class Chunk:
    doc_id: str
    source_path: str
    doc_type: str       # instruction | model_state | dimension_structure
    title: str
    content: str
    metadata: dict = field(default_factory=dict)


def chunk_markdown(relative_path: str, text: str) -> Generator[Chunk, None, None]:
    """
    Split a markdown file on ## headings.
    Each ## section becomes one chunk, truncated to 3000 chars.
    """
    sections = re.split(r'\n(?=## )', text)
    for i, section in enumerate(sections):
        section = section.strip()
        if not section:
            continue
        first_line = section.split('\n')[0].lstrip('#').strip()
        title = first_line or f"Section {i}"
        content = section[:3000]
        doc_id = f"{relative_path}#s{i}_{_slugify(title)}"
        yield Chunk(
            doc_id=doc_id,
            source_path=relative_path,
            doc_type="instruction",
            title=title,
            content=content,
            metadata={"section_index": i, "file": relative_path},
        )


def chunk_model_state(relative_path: str, text: str) -> Generator[Chunk, None, None]:
    """
    Split current_model_state.md per TM1 object.
    Sections are ### <name> blocks under ## Dimensions, ## Cubes, ## Processes.
    """
    section_type = "model_state"
    current_name = ""
    current_lines: list[str] = []

    for line in text.split('\n'):
        if line.startswith('## Dimension'):
            section_type = 'dimension_structure'
        elif line.startswith('## Cube'):
            section_type = 'cube_rule'
        elif line.startswith('## Process'):
            section_type = 'process_code'
        elif line.startswith('### '):
            if current_lines and current_name:
                yield _flush_chunk(relative_path, section_type, current_name, current_lines)
            current_name = line.lstrip('#').strip()
            current_lines = [line]
        else:
            current_lines.append(line)

    if current_lines and current_name:
        yield _flush_chunk(relative_path, section_type, current_name, current_lines)


def _flush_chunk(path: str, doc_type: str, name: str, lines: list[str]) -> Chunk:
    content = '\n'.join(lines).strip()[:3000]
    return Chunk(
        doc_id=f"model_state::{doc_type}::{name}",
        source_path=path,
        doc_type=doc_type,
        title=name,
        content=content,
        metadata={"object_name": name, "object_type": doc_type},
    )


def chunk_tm1_dimension(
    dim_name: str,
    elements: list[dict],
    attributes: list[str],
    attribute_values: dict[str, dict[str, str]] | None = None,
    hierarchy_edges: dict[str, list[str]] | None = None,
) -> Chunk:
    """
    Create a single chunk summarising a TM1 dimension's structure.

    Args:
        dim_name: Dimension name.
        elements: List of {"name": ..., "element_type": ...} dicts.
        attributes: List of attribute names.
        attribute_values: Optional dict of {element_name: {attr_name: value}}.
        hierarchy_edges: Optional dict of {parent: [child, ...]} consolidation edges.
    """
    leaf_els = [e for e in elements if e.get("element_type") == "Numeric"]
    consol_els = [e for e in elements if e.get("element_type") == "Consolidated"]
    string_els = [e for e in elements if e.get("element_type") == "String"]
    lines = [
        f"Dimension: {dim_name}",
        f"Total elements: {len(elements)}  ({len(leaf_els)} Numeric, {len(consol_els)} Consolidated, {len(string_els)} String)",
        f"Attributes: {', '.join(attributes) if attributes else 'none'}",
    ]

    # Hierarchy summary — show top-level consolidations and their children
    if hierarchy_edges:
        # Find root consolidations (parents that aren't children of anyone)
        all_children = set()
        for children in hierarchy_edges.values():
            all_children.update(children)
        roots = [p for p in hierarchy_edges if p not in all_children]
        if roots:
            lines.append(f"\nHierarchy roots: {', '.join(roots[:10])}")
            for root in roots[:5]:
                children = hierarchy_edges.get(root, [])
                if children:
                    lines.append(f"  {root} -> {', '.join(children[:15])}")
                    if len(children) > 15:
                        lines.append(f"    ... and {len(children) - 15} more children")

    # Element listing with attribute values for richer context
    if attribute_values and leaf_els:
        lines.append(f"\nLeaf elements with attributes (first 40):")
        for el in leaf_els[:40]:
            el_name = el["name"]
            attrs = attribute_values.get(el_name, {})
            if attrs:
                attr_strs = [f"{k}={v}" for k, v in attrs.items() if v and str(v).strip()]
                if attr_strs:
                    lines.append(f"  {el_name}: {'; '.join(attr_strs[:6])}")
                else:
                    lines.append(f"  {el_name}")
            else:
                lines.append(f"  {el_name}")
        if len(leaf_els) > 40:
            lines.append(f"  ... and {len(leaf_els) - 40} more")
    else:
        lines.append("Sample leaf elements (first 30):")
        for el in leaf_els[:30]:
            lines.append(f"  {el['name']}")
        if len(leaf_els) > 30:
            lines.append(f"  ... and {len(leaf_els) - 30} more")

    # Also list consolidations
    if consol_els:
        lines.append(f"\nConsolidated elements ({len(consol_els)}):")
        for el in consol_els[:20]:
            child_count = len(hierarchy_edges.get(el["name"], [])) if hierarchy_edges else 0
            if child_count:
                lines.append(f"  {el['name']} ({child_count} children)")
            else:
                lines.append(f"  {el['name']}")
        if len(consol_els) > 20:
            lines.append(f"  ... and {len(consol_els) - 20} more")

    content = '\n'.join(lines)
    # Truncate to stay within embedding limits (but allow more than before)
    content = content[:4000]

    return Chunk(
        doc_id=f"tm1_api::dimension::{dim_name}",
        source_path=f"tm1_api::dimension::{dim_name}",
        doc_type="dimension_structure",
        title=f"Dimension: {dim_name}",
        content=content,
        metadata={
            "dim_name": dim_name,
            "total_count": len(elements),
            "leaf_count": len(leaf_els),
            "consol_count": len(consol_els),
            "string_count": len(string_els),
            "attributes": attributes,
        },
    )


def chunk_plain_text(
    source_path: str,
    text: str,
    chunk_size: int = 2500,
) -> Generator[Chunk, None, None]:
    """
    Chunk plain text by paragraph boundaries.
    Used for Google Drive documents, PDFs, and other non-markdown files.
    """
    paragraphs = re.split(r'\n{2,}', text)
    current_chunk: list[str] = []
    current_len = 0
    chunk_idx = 0

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if current_len + len(para) > chunk_size and current_chunk:
            content = "\n\n".join(current_chunk)
            # Use first line as title (truncated)
            title = current_chunk[0][:80] if current_chunk else f"Chunk {chunk_idx}"
            yield Chunk(
                doc_id=f"{source_path}#p{chunk_idx}",
                source_path=source_path,
                doc_type="plain_text",
                title=title,
                content=content[:3000],
                metadata={"chunk_index": chunk_idx, "source": source_path},
            )
            chunk_idx += 1
            current_chunk = []
            current_len = 0
        current_chunk.append(para)
        current_len += len(para)

    # Flush remaining
    if current_chunk:
        content = "\n\n".join(current_chunk)
        title = current_chunk[0][:80] if current_chunk else f"Chunk {chunk_idx}"
        yield Chunk(
            doc_id=f"{source_path}#p{chunk_idx}",
            source_path=source_path,
            doc_type="plain_text",
            title=title,
            content=content[:3000],
            metadata={"chunk_index": chunk_idx, "source": source_path},
        )


def _slugify(text: str) -> str:
    return re.sub(r'[^a-z0-9_]', '_', text.lower())[:40]


# ---------------------------------------------------------------------------
#  PostgreSQL schema chunks
# ---------------------------------------------------------------------------

def chunk_pg_table_schema(
    table_name: str,
    database: str,
    columns: list[dict],
    foreign_keys: list[dict],
    row_count: int,
    sample_rows: list[dict] | None = None,
) -> Chunk:
    """
    Create a chunk describing a single PostgreSQL table's schema.

    Args:
        table_name: Fully qualified table name (e.g. 'financial_investments_symbol').
        database: Database name (e.g. 'klikk_financials_v4').
        columns: List of {column_name, data_type, is_nullable, column_default}.
        foreign_keys: List of {column, references_table, references_column}.
        row_count: Approximate row count.
        sample_rows: Optional list of sample row dicts.
    """
    lines = [
        f"Table: {table_name} ({database})",
        f"Approx rows: {row_count:,}",
        "",
        "Columns:",
    ]
    for col in columns:
        nullable = ", nullable" if col.get("is_nullable") == "YES" else ""
        default = f", default={col['column_default']}" if col.get("column_default") else ""
        lines.append(f"  {col['column_name']} ({col['data_type']}{nullable}{default})")

    if foreign_keys:
        lines.append("")
        lines.append("Foreign Keys:")
        for fk in foreign_keys:
            lines.append(f"  {fk['column']} -> {fk['references_table']}.{fk['references_column']}")

    if sample_rows:
        lines.append("")
        lines.append("Sample data:")
        for row in sample_rows[:3]:
            # Truncate long values
            parts = [f"{k}: {str(v)[:60]}" for k, v in row.items()]
            lines.append(f"  {{{', '.join(parts)}}}")

    content = "\n".join(lines)[:4000]

    # Determine app prefix for grouping
    app_prefix = table_name.split("_")[0] if "_" in table_name else table_name

    return Chunk(
        doc_id=f"pg_schema::{database}::{table_name}",
        source_path=f"pg_schema::{database}::{table_name}",
        doc_type="pg_table_schema",
        title=f"PostgreSQL Table: {table_name}",
        content=content,
        metadata={
            "database": database,
            "table_name": table_name,
            "app_prefix": app_prefix,
            "column_count": len(columns),
            "row_count": row_count,
            "has_foreign_keys": bool(foreign_keys),
        },
    )


# ---------------------------------------------------------------------------
#  Data context chunks (hardcoded relational knowledge)
# ---------------------------------------------------------------------------

def chunk_share_data_relationships() -> Chunk:
    """Relationship map for share & investment data across PostgreSQL and TM1."""
    content = """\
## Share & Investment Data Model — How It Fits Together

### Data Sources
1. Investec Private Banking API → InvestecBankAccount, InvestecBankTransaction
2. Investec JSE Exports → InvestecJseTransaction (buy/sell/dividend activity), InvestecJsePortfolio (holdings snapshots)
3. yfinance API → Symbol, PricePoint, Dividend, Split, SymbolInfo, FinancialStatement, etc.

### Investec Export Data
- **Portfolio** (investec_investecjseportfolio) = Investec holdings export. Point-in-time snapshots showing what you hold, cost basis, current value, P&L. Fields: share_code, company, date, quantity, currency, unit_cost, total_cost, price, total_value, exchange_rate, profit_loss, portfolio_percent, annual_income_zar.
- **Transaction** (investec_investecjsetransaction) = Investec transaction export. Activity history showing buys, sells, dividends received, fees paid. Fields: share_name, date, account_number, description, type (Buy/Sell/Dividend/Special Dividend/Foreign Dividend/Dividend Tax/Fee/Broker Fee), quantity, value, value_per_share, value_calculated, dividend_ttm.
- **ShareNameMapping** (investec_investecjsesharenamemapping) = Maps between share_name (from transactions) and share_code (from portfolio). Has share_name, share_name2, share_name3 (alternative names), company, share_code. Auto-created on portfolio import.
- **MonthlyPerformance** (investec_investecjsesharemonthlyperformance) = Calculated monthly metrics: share_name, date, dividend_type, investec_account, dividend_ttm, closing_price, quantity, total_market_value, dividend_yield.

### Core Relationship Chain
InvestecJsePortfolio.save() auto-creates InvestecJseShareNameMapping (share_name <-> share_code <-> company)
  |
sync_investec_symbols command links InvestecJseShareNameMapping -> financial_investments_symbol (adds .JO suffix for JSE)
  |
refresh_all_symbols fetches yfinance data -> PricePoint (daily OHLCV), Dividend, Split
refresh_extra_financial_data -> SymbolInfo, FinancialStatement, EarningsReport, AnalystRecommendation, etc.

### Key Joins (PostgreSQL)
- financial_investments_symbol.share_name_mapping_id -> investec_investecjsesharenamemapping.id
- financial_investments_pricepoint.symbol_id -> financial_investments_symbol.id
- financial_investments_dividend.symbol_id -> financial_investments_symbol.id
- financial_investments_symbolinfo.symbol_id -> financial_investments_symbol.id (1:1)
- financial_investments_financialstatement.symbol_id -> financial_investments_symbol.id
- financial_investments_earningsreport.symbol_id -> financial_investments_symbol.id
- financial_investments_earningsestimate.symbol_id -> financial_investments_symbol.id (1:1)
- financial_investments_analystrecommendation.symbol_id -> financial_investments_symbol.id (1:1)
- financial_investments_analystpricetarget.symbol_id -> financial_investments_symbol.id (1:1)
- financial_investments_ownershipsnapshot.symbol_id -> financial_investments_symbol.id
- financial_investments_newsitem.symbol_id -> financial_investments_symbol.id
- investec_investecjseportfolio.share_code = investec_investecjsesharenamemapping.share_code
- investec_investecjsetransaction.share_name = investec_investecjsesharenamemapping.share_name
- investec_investecjsesharemonthlyperformance.share_name = investec_investecjsesharenamemapping.share_name

### TM1 <-> PostgreSQL Overlap
- TM1 listed_share dimension (71 elements) = same securities as financial_investments_symbol
- TM1 listed_share_src_holdings = Investec positions (planned/forecast layer)
- PostgreSQL investec_investecjseportfolio = actual Investec portfolio snapshots (holdings export)
- PostgreSQL investec_investecjsetransaction = actual buy/sell/dividend activity (transaction export)
- PostgreSQL financial_investments_pricepoint = daily market prices from yfinance
- Use TM1 for planning/budgeting. Use PostgreSQL for actual holdings, transaction history, market data, fundamentals.

### Common Queries
- Holdings history: SELECT from investec_investecjseportfolio WHERE share_code = X ORDER BY date
- Buy/sell/dividend activity: SELECT from investec_investecjsetransaction WHERE share_name = X AND type IN ('Buy','Sell','Dividend')
- Price history: SELECT from financial_investments_pricepoint WHERE symbol_id = X ORDER BY date
- Dividend yield over time: SELECT from investec_investecjsesharemonthlyperformance WHERE share_name = X
- Fundamentals: SELECT from financial_investments_financialstatement WHERE symbol_id = X"""

    return Chunk(
        doc_id="data_context::share_data_relationships",
        source_path="data_context::share_data_relationships",
        doc_type="data_relationship",
        title="Share & Investment Data Model — Relationships",
        content=content[:4000],
        metadata={"topic": "share_data", "scope": "cross_system"},
    )


def chunk_gl_data_relationships() -> Chunk:
    """Relationship map for GL data across Xero, PostgreSQL, and TM1."""
    content = """\
## GL Data Model — How It Fits Together

### Data Flow: Xero -> PostgreSQL -> TM1
Xero API (Journals, Invoices, Bank Txns)
  -> xero_data_xerotransactionsource (raw JSON per type)
  -> xero_data_xerojournalssource (raw journal payloads)
  -> xero_data_xerojournals (parsed: one row per journal line)
  -> xero_cube_xerotrailbalance (aggregated by org/account/year/month/contact/tracking)
  -> TM1 gl_src_trial_balance (via TI import process)

### Key PostgreSQL Tables
xero_core_xerotenant (organisation_id, name, fiscal_year_start_month, tracking_category_1_id, tracking_category_2_id)
xero_metadata_xeroaccount (account_id, code, name, type, class — from Xero chart of accounts)
xero_metadata_xerocontacts (contact_id, name, type — customers/suppliers)
xero_metadata_xerotracking (option_id, name, option, tracking_category_id — tracking categories)
xero_data_xerotransactionsource (transactions_id, type, contact_id, collection JSON)
xero_data_xerojournals (journal_id, account_id, date, amount, contact_id, tracking1_id, tracking2_id)
xero_cube_xerotrailbalance (organisation_id, account_id, year, month, fin_year, fin_period,
  contact_id, tracking1_id, tracking2_id, amount, debit, credit, tax_amount, balance_to_date)

### Key Joins
- xero_data_xerojournals.account_id -> xero_metadata_xeroaccount.id
- xero_data_xerojournals.contact_id -> xero_metadata_xerocontacts.id
- xero_data_xerojournals.tracking1_id -> xero_metadata_xerotracking.id
- xero_data_xerojournals.tracking2_id -> xero_metadata_xerotracking.id
- xero_data_xerojournals.transaction_source_id -> xero_data_xerotransactionsource.transactions_id
- xero_cube_xerotrailbalance.organisation_id -> xero_core_xerotenant.organisation_id
- xero_cube_xerotrailbalance.account_id -> xero_metadata_xeroaccount.id

### TM1 Cube: gl_src_trial_balance
Dimensions: year, month, version, entity, account, contact, tracking_1, tracking_2, measure_gl_src_trial_balance
Measures: amount, tax_amount, balance, debit, credit
- BS accounts: balance accumulates YTD. P&L accounts: balance = amount.
- Populated by TI import from xero_cube_xerotrailbalance.
- Reconcile with reconcile_gl_totals() tool."""

    return Chunk(
        doc_id="data_context::gl_data_relationships",
        source_path="data_context::gl_data_relationships",
        doc_type="data_relationship",
        title="GL Data Model — Xero to TM1 Relationships",
        content=content[:4000],
        metadata={"topic": "gl_data", "scope": "cross_system"},
    )


def chunk_column_dimension_map() -> Chunk:
    """Maps PostgreSQL GL columns to TM1 cube dimensions with business meaning."""
    content = """\
## GL Column -> TM1 Dimension Mapping

XeroTrailBalance (PostgreSQL) -> gl_src_trial_balance (TM1 cube)

| PG Column        | TM1 Dimension                    | Business Meaning                                |
|------------------|----------------------------------|-------------------------------------------------|
| year             | year                             | Calendar year (2014-2030)                       |
| month            | month                            | Jul-Jun fiscal months + consolidators H1/H2/Q1-Q4/YTD |
| organisation_id  | entity                           | Xero org GUIDs (Klikk, Tremly, Dippenaar Family, Space Invaders) |
| account_id       | account                          | Chart of accounts (382 elements), attrs: code, name, type, account_type |
| contact_id       | contact                          | Customers/suppliers/employees (1,152 elements)  |
| tracking1_id     | tracking_1                       | Business segment: property, event equipment, financial investments |
| tracking2_id     | tracking_2                       | Secondary tracking category                     |
| amount/debit/credit/tax_amount | measure_gl_src_trial_balance | Measures: amount, tax_amount, balance, debit, credit |
| (always actual)  | version                          | actual, budget, forecast, prior_year            |

### Entity GUID Mapping (entity dimension uses Xero GUIDs, NOT names)
41ebfa0e-012e-4ff1-82ba-a9a7585c536c = Klikk (Pty) Ltd (code: kl)
0415e61e-f78c-4216-ac54-7933a6f63a5d = Tremly (Pty) Ltd (code: tr)
27806be4-62dd-4c50-9eb9-c8b79231f6a1 = Dippenaar Family (code: df)
3472e131-f248-41d1-9085-228112825f25 = Space Invaders (code: si)
All_Entity = consolidated total
Use tm1_get_element_attributes_bulk("entity") to get aliases.

### Tracking Dimensions (from Xero tracking categories)
tracking_1 elements: Property, Event Equipment, Financial Investments (business segments)
tracking_2 elements: secondary classification
Tracking resolved via XeroTenant.get_tracking_slot(TrackingCategoryID) -> slot 1 or 2.
Xero allows max 2 active tracking categories per organisation.

### Month Dimension
Fiscal year starts July (configurable per tenant via fiscal_year_start_month).
Leaf elements: Jul, Aug, Sep, Oct, Nov, Dec, Jan, Feb, Mar, Apr, May, Jun
Consolidators: H1 (Jul-Dec), H2 (Jan-Jun), Q1-Q4, YTD

### Account Dimension
382 leaf elements with attributes: code, name, type (REVENUE/EXPENSE/ASSET/LIABILITY/EQUITY),
account_type (sub-classification), cashflow_activity (maps to cashflow cube).
Use tm1_get_element_attributes_bulk("account") for full attribute list."""

    return Chunk(
        doc_id="data_context::column_dimension_map",
        source_path="data_context::column_dimension_map",
        doc_type="column_dimension_map",
        title="GL Column to TM1 Dimension Mapping",
        content=content[:4000],
        metadata={"topic": "column_dimension_mapping", "scope": "cross_system"},
    )


def chunk_transaction_processing() -> Chunk:
    """How transactions flow from Xero API through processing to TM1."""
    content = """\
## Transaction Processing Pipeline

### Step 1: Xero API Sync
Trigger: ProcessTree (scheduled or manual via xero_sync/services.py -> update_xero_models)
Process tree: fetch_metadata -> (fetch_journals + fetch_manual_journals) -> process_data -> process_pnl
Fetches: Accounts, Contacts, Tracking Categories, Bank Transactions, Invoices,
  Payments, Credit Notes, Prepayments, Overpayments, Manual Journals

### Step 2: Store Raw Data
XeroTransactionSource — raw transaction JSON per type (bulk create/update, 5000-record batches)
XeroJournalsSource — raw journal API responses
XeroDocument — file attachments linked to transactions

### Step 3: Parse Journals (XeroJournalsSourceManager.create_journals_from_xero)
XeroJournalsSource -> XeroJournals (one row per journal line)
Key processing:
- Resolves tracking slots: TrackingCategoryID -> XeroTenant.get_tracking_slot() -> slot 1 or 2
- Priority: journal line tracking > transaction source tracking > account code match
- Inherits contact from transaction source if journal line has none
- Stores debit (positive amounts) and credit (negative amounts) separately
- All creates wrapped in transaction.atomic() for consistency

### Step 4: Consolidate to Trial Balance
XeroTrailBalanceManager.consolidate_journals():
  INSERT INTO xero_cube_xerotrailbalance
  SELECT organisation_id, account_id, year, month, fin_year, fin_period,
         COALESCE(journal.contact_id, source.contact_id), tracking1_id, tracking2_id,
         SUM(amount), SUM(debit), SUM(credit), SUM(tax_amount)
  FROM xero_data_xerojournals JOIN xero_data_xerotransactionsource
  GROUP BY org, account, year, month, contact, tracking1, tracking2
  HAVING SUM(amount) != 0
Fiscal year: calculated from tenant.fiscal_year_start_month (default: July = 7)
Modes: full rebuild (delete all, reinsert) or incremental (only affected periods)

### Step 5: Import to TM1
TI process imports xero_cube_xerotrailbalance -> gl_src_trial_balance cube
Column mapping:
  organisation_id -> entity (Xero GUID)
  account_code -> account
  year -> year
  month -> month (calendar month name: Jan, Feb, etc.)
  contact_id -> contact
  tracking1_id -> tracking_1
  tracking2_id -> tracking_2
  amount -> measure_gl_src_trial_balance.amount

### Step 6: TM1 Calculation Layers
gl_src_trial_balance (source, read-only — imported data)
  -> gl_pln_forecast (planning: actual = live lookup from src; budget/forecast = user-entered)
  -> gl_rpt_trial_balance (reporting: FY-translated view, populated by TI process)
  -> cashflow_cal_metrics (cashflow: derived from GL via account->cashflow_activity mapping)

### Reconciliation
reconcile_gl_totals() tool compares TM1 gl_src total vs PostgreSQL xero_cube total for a year/month.
Tolerance: < 1.0 difference considered reconciled.

### Dimension Storage
TM1 is authoritative for current dimension structure (elements, attributes, hierarchies).
PostgreSQL metadata tables (xero_metadata_xeroaccount, xero_metadata_xerocontacts, xero_metadata_xerotracking)
  mirror Xero API data and are updated during sync. May lag behind manual TM1 changes.
For current elements: query TM1 (tm1_get_dimension_elements).
For Xero-specific metadata (account class, contact type): query PostgreSQL (pg_query_financials)."""

    return Chunk(
        doc_id="data_context::transaction_processing",
        source_path="data_context::transaction_processing",
        doc_type="data_flow",
        title="Transaction Processing Pipeline — Xero to TM1",
        content=content[:4000],
        metadata={"topic": "transaction_processing", "scope": "full_pipeline"},
    )
