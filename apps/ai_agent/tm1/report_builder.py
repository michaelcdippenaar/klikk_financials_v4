"""
Natural Language Report Builder for TM1.

Takes a plain-English report request (e.g. "show me revenue by entity for 2025")
and resolves it to TM1 cube, elements, and MDX — then executes and returns results.

Flow:
1. Match request to a cube (keyword/intent mapping)
2. Resolve natural-language element references to actual TM1 element names (alias lookup)
3. Build MDX with resolved elements
4. Execute and return formatted results
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from TM1py import TM1Service

from apps.ai_agent.agent.config import TM1_CONFIG

log = logging.getLogger("mcp_tm1")

# ---------------------------------------------------------------------------
#  Cube intent mapping — keywords -> cube name + default config
# ---------------------------------------------------------------------------

CUBE_PROFILES = {
    "gl_rpt_trial_balance": {
        "keywords": ["trial balance", "tb", "gl report", "profit and loss", "p&l", "pnl",
                      "income statement", "balance sheet", "revenue", "expenses", "cost",
                      "general ledger", "gl"],
        "dimensions": ["year", "month", "version", "entity", "account",
                       "measure_gl_rpt_trial_balance"],
        "default_measure": "amount",
        "default_rows": "account",
        "default_cols": "month",
    },
    "gl_src_trial_balance": {
        "keywords": ["source trial balance", "source tb", "source gl", "by contact",
                      "by tracking", "detailed gl", "gl source"],
        "dimensions": ["year", "month", "version", "entity", "account", "contact",
                       "tracking_1", "tracking_2", "measure_gl_src_trial_balance"],
        "default_measure": "amount",
        "default_rows": "account",
        "default_cols": "month",
    },
    "gl_pln_forecast": {
        "keywords": ["forecast", "budget vs actual", "plan", "planning",
                      "gl forecast", "gl plan"],
        "dimensions": ["year", "month", "version", "entity", "account", "cost_object",
                       "measure_gl_pln_forecast"],
        "default_measure": "amount",
        "default_rows": "account",
        "default_cols": "month",
    },
    "cashflow_rpt_summary": {
        "keywords": ["cashflow", "cash flow", "cash position", "operating cash",
                      "investing cash", "financing cash"],
        "dimensions": ["financial_year", "month", "version", "entity",
                       "cashflow_activity", "measure_cashflow_rpt_summary"],
        "default_measure": "amount",
        "default_rows": "cashflow_activity",
        "default_cols": "month",
    },
    "cashflow_cal_metrics": {
        "keywords": ["cashflow metrics", "cash balance", "cashflow calc"],
        "dimensions": ["year", "month", "version", "entity", "cashflow_activity",
                       "account", "measure_cashflow_cal_metrics"],
        "default_measure": "amount",
        "default_rows": "cashflow_activity",
        "default_cols": "month",
    },
    "listed_share_src_holdings": {
        "keywords": ["share holdings", "portfolio holdings", "share positions",
                      "share value", "market value", "portfolio value", "investment holdings"],
        "dimensions": ["year", "month", "version", "entity", "investec_account",
                       "listed_share", "measure_listed_share_src_holdings"],
        "default_measure": "market_value",
        "default_rows": "listed_share",
        "default_cols": "month",
    },
    "listed_share_src_transactions": {
        "keywords": ["share transactions", "share buys", "share sells", "dividends received",
                      "share trades", "investment transactions"],
        "dimensions": ["year", "month", "version", "entity", "investec_account",
                       "listed_share", "listed_share_transaction_type",
                       "measure_listed_share_src_transactions"],
        "default_measure": "amount",
        "default_rows": "listed_share",
        "default_cols": "month",
    },
    "listed_share_pln_forecast": {
        "keywords": ["dividend forecast", "dividend budget", "share forecast",
                      "dps forecast", "dividend plan", "pln forecast",
                      "declared_dividend"],
        "dimensions": ["year", "month", "version", "entity", "listed_share",
                       "listed_share_transaction_type", "input_type",
                       "measure_listed_share_pln_forecast"],
        "default_measure": "dividends_per_share",
        "default_rows": "listed_share",
        "default_cols": "month",
    },
    "listed_share_cal_flow_metrics": {
        "keywords": ["share performance", "share returns", "twrr", "total return",
                      "capital return", "income return", "dividend yield", "share metrics"],
        "dimensions": ["year", "month", "version", "entity", "investec_account",
                       "listed_share", "measure_listed_share_cal_flow_metrics"],
        "default_measure": "total_return",
        "default_rows": "listed_share",
        "default_cols": "month",
    },
    "prop_res_pln_forecast_revenue": {
        "keywords": ["property", "rental", "rental income", "property revenue",
                      "property forecast"],
        "dimensions": ["year", "month", "version", "entity", "property", "input_type",
                       "measure_prop_res_pln_forecast_revenue"],
        "default_measure": "rental_income",
        "default_rows": "property",
        "default_cols": "month",
    },
}

# Dimension alias attribute names (for resolving natural language to element names)
ALIAS_ATTRS = {
    "account": ["name", "code"],
    "entity": ["name", "code"],
    "listed_share": ["share_name", "company", "share_name_2", "share_name_3"],
    "contact": ["name"],
    "cashflow_activity": ["name"],
    "property": ["name"],
    "cost_object": ["name"],
}


def _get_tm1() -> TM1Service:
    """Get a TM1 connection (reuses tm1_tools singleton)."""
    from apps.ai_agent.tm1 import tm1_tools
    return tm1_tools._get_tm1()


def _match_cube(query: str) -> tuple[str, dict] | None:
    """Match a natural language query to a cube profile."""
    query_lower = query.lower()
    best_match = None
    best_score = 0

    for cube_name, profile in CUBE_PROFILES.items():
        score = 0
        for kw in profile["keywords"]:
            if kw in query_lower:
                score += len(kw)  # longer keyword matches score higher
        if score > best_score:
            best_score = score
            best_match = (cube_name, profile)

    return best_match


def _resolve_element(
    tm1: TM1Service,
    dimension: str,
    search_term: str,
) -> str | None:
    """
    Resolve a natural language term to an actual element name.
    Checks element names directly, then aliases.
    """
    search_lower = search_term.strip().lower()

    # Direct element name match
    try:
        element_names = tm1.elements.get_element_names(dimension, dimension)
        for el in element_names:
            if el.lower() == search_lower:
                return el
    except Exception:
        pass

    # Alias/attribute match
    alias_attrs = ALIAS_ATTRS.get(dimension, [])
    for attr in alias_attrs:
        try:
            aliases = tm1.elements.get_attribute_of_elements(dimension, dimension, attr)
            for el_name, alias_val in aliases.items():
                if alias_val and str(alias_val).strip().lower() == search_lower:
                    return el_name
                # Partial match for longer names
                if alias_val and search_lower in str(alias_val).strip().lower():
                    return el_name
        except Exception:
            continue

    # Substring match on element names
    try:
        for el in element_names:
            if search_lower in el.lower():
                return el
    except Exception:
        pass

    return None


def _extract_year(query: str) -> str | None:
    """Extract a year (2014-2030) from the query."""
    match = re.search(r'\b(20[12]\d)\b', query)
    return match.group(1) if match else None


def _extract_months(query: str) -> list[str]:
    """Extract month references from the query."""
    month_map = {
        "january": "Jan", "february": "Feb", "march": "Mar", "april": "Apr",
        "may": "May", "june": "Jun", "july": "Jul", "august": "Aug",
        "september": "Sep", "october": "Oct", "november": "Nov", "december": "Dec",
        "jan": "Jan", "feb": "Feb", "mar": "Mar", "apr": "Apr",
        "jun": "Jun", "jul": "Jul", "aug": "Aug", "sep": "Sep",
        "oct": "Oct", "nov": "Nov", "dec": "Dec",
        "q1": None, "q2": None, "q3": None, "q4": None,
        "ytd": None, "full year": "All_Month",
    }
    found = []
    query_lower = query.lower()
    for term, element in month_map.items():
        if term in query_lower and element:
            found.append(element)
    return found


def _extract_version(query: str) -> str:
    """Extract version from query (actual, budget, forecast)."""
    query_lower = query.lower()
    if "budget" in query_lower:
        return "budget"
    if "forecast" in query_lower:
        return "forecast"
    return "actual"


def _extract_dimension_filters(
    tm1: TM1Service,
    query: str,
    cube_dimensions: list[str],
) -> dict[str, list[str]]:
    """
    Extract dimension element references from the query.
    Returns {dimension_name: [resolved_element_names]}.
    """
    filters: dict[str, list[str]] = {}

    # Year
    if "year" in cube_dimensions or "financial_year" in cube_dimensions:
        year = _extract_year(query)
        if year:
            if "financial_year" in cube_dimensions and "year" not in cube_dimensions:
                filters["financial_year"] = [f"FY{year}"]
            elif "year" in cube_dimensions:
                filters["year"] = [year]

    # Month
    if "month" in cube_dimensions:
        months = _extract_months(query)
        if months:
            filters["month"] = months

    # Version
    if "version" in cube_dimensions:
        filters["version"] = [_extract_version(query)]

    # Entity — look for company names (exact and partial alias match)
    if "entity" in cube_dimensions:
        try:
            entity_attrs = ALIAS_ATTRS.get("entity", ["name"])

            def _fetch_entity_attr(attr):
                try:
                    return tm1.elements.get_attribute_of_elements("entity", "entity", attr)
                except Exception:
                    return {}

            query_lower = query.lower()
            with ThreadPoolExecutor(max_workers=min(len(entity_attrs), 4)) as pool:
                for aliases in pool.map(_fetch_entity_attr, entity_attrs):
                    for el_name, alias in aliases.items():
                        if not alias or not str(alias).strip():
                            continue
                        alias_lower = str(alias).strip().lower()
                        already = filters.get("entity", [])
                        if el_name in already:
                            continue
                        if alias_lower in query_lower:
                            filters.setdefault("entity", []).append(el_name)
                        else:
                            for word in alias_lower.split():
                                if len(word) >= 3 and word not in {"pty", "ltd", "inc", "the"} and word in query_lower:
                                    filters.setdefault("entity", []).append(el_name)
                                    break
        except Exception:
            pass

    # Listed share — look for share/company names
    if "listed_share" in cube_dimensions and "listed_share" not in filters:
        try:
            share_attrs = ALIAS_ATTRS.get("listed_share", ["share_name"])

            def _fetch_share_attr(attr):
                try:
                    return tm1.elements.get_attribute_of_elements("listed_share", "listed_share", attr)
                except Exception:
                    return {}

            query_lower = query.lower()
            with ThreadPoolExecutor(max_workers=min(len(share_attrs), 4)) as pool:
                for aliases in pool.map(_fetch_share_attr, share_attrs):
                    for el_name, alias in aliases.items():
                        if not alias or not str(alias).strip():
                            continue
                        alias_lower = str(alias).strip().lower()
                        if alias_lower in query_lower:
                            filters.setdefault("listed_share", []).append(el_name)
        except Exception:
            pass

    return filters


def _detect_rows_columns(
    query: str,
    cube_dimensions: list[str],
    default_rows: str,
    default_cols: str,
) -> tuple[str, str]:
    """Detect which dimension should be on rows vs columns from the query."""
    query_lower = query.lower()
    rows_dim = default_rows
    cols_dim = default_cols

    # "by entity" -> entity on rows
    by_match = re.search(r'\bby\s+(\w+)', query_lower)
    if by_match:
        by_term = by_match.group(1)
        for dim in cube_dimensions:
            if dim.startswith("measure_"):
                continue
            if by_term in dim.lower() or dim.lower() in by_term:
                rows_dim = dim
                break
        # Common aliases
        if by_term in ("company", "org", "organisation"):
            rows_dim = "entity"
        elif by_term in ("share", "shares", "stock"):
            rows_dim = "listed_share"
        elif by_term in ("activity",):
            rows_dim = "cashflow_activity"

    # "over months" / "monthly" -> month on columns
    if "over month" in query_lower or "monthly" in query_lower:
        cols_dim = "month"
    elif "over year" in query_lower or "yearly" in query_lower:
        cols_dim = "year" if "year" in cube_dimensions else "financial_year"

    return rows_dim, cols_dim


def _build_mdx(
    cube_name: str,
    cube_dimensions: list[str],
    rows_dim: str,
    cols_dim: str,
    measure_dim: str,
    measure: str,
    filters: dict[str, list[str]],
    rows_elements: list[str] | None = None,
    top_n: int = 50,
) -> str:
    """Build an MDX query from resolved parameters."""
    # COLUMNS: measure on columns, or cols_dim
    if cols_dim == rows_dim:
        # If same, put measure alone on columns
        col_set = f"{{[{measure_dim}].[{measure}]}}"
    else:
        # cols_dim elements
        if cols_dim in filters and filters[cols_dim]:
            col_members = ", ".join(f"[{cols_dim}].[{e}]" for e in filters[cols_dim])
            col_set = f"{{{col_members}}}"
        else:
            col_set = f"{{TM1SubsetAll([{cols_dim}])}}"
        # Cross with measure
        col_set = f"{col_set} * {{[{measure_dim}].[{measure}]}}"

    # ROWS: rows_dim elements
    if rows_elements:
        row_members = ", ".join(f"[{rows_dim}].[{e}]" for e in rows_elements[:top_n])
        row_set = f"{{{row_members}}}"
    elif rows_dim in filters and filters[rows_dim]:
        row_members = ", ".join(f"[{rows_dim}].[{e}]" for e in filters[rows_dim])
        row_set = f"{{{row_members}}}"
    else:
        row_set = f"{{TM1SubsetAll([{rows_dim}])}}"

    # WHERE clause: all dimensions not on rows/columns
    # Dimensions with common "All_" consolidation parents
    _ALL_ELEMENT_MAP = {
        "entity": "All_Entity",
        "account": "All_Account",
        "contact": "All_Contact",
        "tracking_1": "All_Tracking_1",
        "tracking_2": "All_Tracking_2",
        "cashflow_activity": "All_Cashflow_Activity",
        "listed_share": "All_Listed_Share",
        "cost_object": "All_Cost_Object",
        "investec_account": "All_Investec_Account",
        "property": "All_Property",
    }

    where_parts = []
    for dim in cube_dimensions:
        if dim in (rows_dim, cols_dim, measure_dim):
            continue
        if dim in filters and filters[dim]:
            # Use first element for WHERE
            where_parts.append(f"[{dim}].[{filters[dim][0]}]")
        elif dim in _ALL_ELEMENT_MAP:
            # Use the "All_" consolidation parent
            where_parts.append(f"[{dim}].[{_ALL_ELEMENT_MAP[dim]}]")

    mdx = f"SELECT {col_set} ON COLUMNS, NON EMPTY {row_set} ON ROWS FROM [{cube_name}]"
    if where_parts:
        mdx += f" WHERE ({', '.join(where_parts)})"

    return mdx


# ---------------------------------------------------------------------------
#  Main tool functions
# ---------------------------------------------------------------------------

def tm1_build_report(
    query: str,
    cube_name: str = "",
    rows_dimension: str = "",
    columns_dimension: str = "",
    measure: str = "",
    top_n: int = 50,
) -> dict[str, Any]:
    """
    Build and execute a TM1 report from a natural language description.

    Examples:
      - "Show me the trial balance by account for 2025 actual"
      - "Revenue by entity for Jan to Jun 2025"
      - "Share holdings by share for Klikk 2025"
      - "Cashflow summary by activity for 2025 actual"
      - "Forecast vs actual by account for Klikk 2025"

    query: Natural language report request.
    cube_name: Override auto-detected cube (optional).
    rows_dimension: Force this dimension on rows (optional).
    columns_dimension: Force this dimension on columns (optional).
    measure: Force this measure element (optional).
    top_n: Max rows to return (default 50).
    """
    try:
        tm1 = _get_tm1()
    except Exception as e:
        return {"error": f"Cannot connect to TM1: {e}"}

    # 1. Match cube
    if cube_name:
        profile = CUBE_PROFILES.get(cube_name, {
            "dimensions": [],
            "default_measure": "amount",
            "default_rows": "",
            "default_cols": "month",
        })
        if not profile["dimensions"]:
            # Get dimensions from TM1
            try:
                cube = tm1.cubes.get(cube_name)
                dims = cube.dimensions
                profile["dimensions"] = [
                    d.name if hasattr(d, "name") else str(d) for d in dims
                ]
            except Exception as e:
                return {"error": f"Cube '{cube_name}' not found: {e}"}
    else:
        match = _match_cube(query)
        if not match:
            return {
                "error": "Could not determine which cube to query from your description.",
                "hint": "Try specifying a cube_name, or use keywords like: trial balance, cashflow, share holdings, forecast, property.",
                "available_cubes": list(CUBE_PROFILES.keys()),
            }
        cube_name, profile = match

    cube_dims = profile["dimensions"]
    measure_dim = next((d for d in cube_dims if d.startswith("measure_")), "")

    if not measure_dim:
        return {"error": f"No measure dimension found for cube '{cube_name}'."}

    # 2. Extract filters from query
    filters = _extract_dimension_filters(tm1, query, cube_dims)

    # 3. Determine rows/columns
    default_rows = rows_dimension or profile.get("default_rows", "")
    default_cols = columns_dimension or profile.get("default_cols", "month")
    rows_dim, cols_dim = _detect_rows_columns(query, cube_dims, default_rows, default_cols)

    if rows_dimension:
        rows_dim = rows_dimension
    if columns_dimension:
        cols_dim = columns_dimension

    # 4. Determine measure
    measure_el = measure or profile.get("default_measure", "amount")

    # 5. Build MDX
    mdx = _build_mdx(
        cube_name=cube_name,
        cube_dimensions=cube_dims,
        rows_dim=rows_dim,
        cols_dim=cols_dim,
        measure_dim=measure_dim,
        measure=measure_el,
        filters=filters,
        top_n=top_n,
    )

    # 6. Execute
    try:
        df = tm1.cells.execute_mdx_dataframe(mdx)
        records = df.to_dict(orient="records")
        if len(records) > top_n:
            records = records[:top_n]
            truncated = True
        else:
            truncated = False

        return {
            "cube": cube_name,
            "mdx": mdx,
            "rows_dimension": rows_dim,
            "columns_dimension": cols_dim,
            "measure": measure_el,
            "filters_applied": filters,
            "data": records,
            "row_count": len(records),
            "truncated": truncated,
        }
    except Exception as e:
        return {
            "error": f"MDX execution failed: {e}",
            "mdx": mdx,
            "cube": cube_name,
            "filters_applied": filters,
            "hint": "The auto-generated MDX may need adjustment. Try specifying cube_name, rows_dimension, or measure explicitly.",
        }


def tm1_resolve_report_elements(
    query: str,
    dimension_name: str = "",
) -> dict[str, Any]:
    """
    Resolve natural language references to TM1 elements.
    Use this to check what elements the report builder would match before running a full report.

    query: text containing element references (e.g. "Klikk", "Absa", "revenue", "2025")
    dimension_name: limit search to this dimension (optional)
    """
    try:
        tm1 = _get_tm1()
    except Exception as e:
        return {"error": f"Cannot connect to TM1: {e}"}

    results: dict[str, Any] = {}

    # Extract structured references
    year = _extract_year(query)
    if year:
        results["year"] = year

    months = _extract_months(query)
    if months:
        results["months"] = months

    version = _extract_version(query)
    results["version"] = version

    # Cube match
    match = _match_cube(query)
    if match:
        results["matched_cube"] = match[0]

    # Try resolving words as elements in specified or common dimensions
    dims_to_check = [dimension_name] if dimension_name else list(ALIAS_ATTRS.keys())
    words = re.findall(r'\b[A-Za-z]{3,}\b', query)
    # Also check multi-word phrases
    phrases = re.findall(r'[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*', query)

    element_matches = []
    checked = set()
    skip_words = {"the", "for", "and", "show", "give", "get", "all",
                  "from", "with", "by", "jan", "feb", "mar", "apr",
                  "may", "jun", "jul", "aug", "sep", "oct", "nov",
                  "dec", "actual", "budget", "forecast", "report",
                  "trial", "balance", "revenue", "month", "year",
                  "account", "accounts", "entity", "entities",
                  "share", "shares", "summary", "total", "amount",
                  "cashflow", "cash", "flow", "property", "holdings",
                  "transactions", "contact", "tracking", "version"}
    # Prioritise entity dimension (company names) before others
    priority_dims = ["entity", "listed_share"]
    ordered_dims = [d for d in priority_dims if d in dims_to_check] + \
                   [d for d in dims_to_check if d not in priority_dims]

    for term in phrases + words:
        if term.lower() in checked or len(term) < 3:
            continue
        checked.add(term.lower())
        if term.lower() in skip_words:
            continue
        for dim in ordered_dims:
            resolved = _resolve_element(tm1, dim, term)
            if resolved:
                element_matches.append({
                    "search_term": term,
                    "dimension": dim,
                    "resolved_element": resolved,
                })
                break

    results["element_matches"] = element_matches
    return results


def tm1_list_report_cubes() -> dict[str, Any]:
    """
    List available cube profiles for natural language reporting.
    Shows which cubes can be queried, their keywords, dimensions, and default measures.
    """
    profiles = []
    for cube_name, profile in CUBE_PROFILES.items():
        profiles.append({
            "cube": cube_name,
            "keywords": profile["keywords"][:5],
            "dimensions": profile["dimensions"],
            "default_measure": profile.get("default_measure", "amount"),
            "default_rows": profile.get("default_rows", ""),
            "default_cols": profile.get("default_cols", "month"),
        })
    return {"cubes": profiles, "count": len(profiles)}
