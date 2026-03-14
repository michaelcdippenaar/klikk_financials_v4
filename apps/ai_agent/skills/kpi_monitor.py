"""
Skill: KPI Monitor
YAML-driven KPI engine. Loads definitions from kpi_definitions.yaml,
computes values from TM1 and PostgreSQL, supports thresholds and alerting.
"""
from __future__ import annotations

import sys
import os
from pathlib import Path
from typing import Any

import yaml

from apps.ai_agent.agent.config import TM1_CONFIG, settings
from TM1py import TM1Service

AGENT_ROOT = Path(__file__).parent.parent.parent
KPI_FILE = AGENT_ROOT / "kpi_definitions.yaml"

MONTH_ORDER = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# ---------------------------------------------------------------------------
#  YAML loading / saving
# ---------------------------------------------------------------------------

def _load_kpi_definitions() -> list[dict]:
    if not KPI_FILE.exists():
        return []
    with open(KPI_FILE, "r") as f:
        data = yaml.safe_load(f)
    return data.get("kpis", [])


def _save_kpi_definitions(kpis: list[dict]) -> None:
    with open(KPI_FILE, "w") as f:
        yaml.dump({"kpis": kpis}, f, default_flow_style=False, sort_keys=False)


# ---------------------------------------------------------------------------
#  Current period helper
# ---------------------------------------------------------------------------

def _get_current_period(tm1: TM1Service) -> dict[str, str]:
    """Read current period from sys_parameters cube.
    Cube dims: [sys_module] x [sys_measure_parameters]
    Valid modules: gl, listed_share, cashflow, prop_res, prop_agr, financing, equip_rental, cost_alloc
    Valid measures: Current Month, Current Year, Financial Year, Financial Year Start Month, Current Period
    """
    month = str(tm1.cells.get_value(
        "sys_parameters", "gl,Current Month"
    )).strip()
    year = str(tm1.cells.get_value(
        "sys_parameters", "gl,Current Year"
    )).strip()
    fy = str(tm1.cells.get_value(
        "sys_parameters", "gl,Financial Year"
    )).strip()
    return {"year": year, "month": month, "fy": fy}


# ---------------------------------------------------------------------------
#  Compute functions per source_type
# ---------------------------------------------------------------------------

def _compute_gl_by_type(
    tm1: TM1Service, params: dict, year: str, month: str
) -> float:
    """Sum GL amounts filtered by account_type attribute values."""
    account_types = [t.upper() for t in params.get("account_types", [])]
    sign = params.get("sign", 1)
    period = params.get("period", "current")
    month_element = "YTD" if period == "ytd" else month

    mdx = f"""
    SELECT {{[measure_gl_src_trial_balance].[amount]}} ON 0,
           {{[account].[All_Account].Children}} ON 1
    FROM [gl_src_trial_balance]
    WHERE ([year].[{year}],[month].[{month_element}],[version].[actual],
           [entity].[All_Entity],[contact].[All_Contact],
           [tracking_1].[All_Tracking_1],[tracking_2].[All_Tracking_2])
    """
    cells = tm1.cells.execute_mdx(mdx)

    at_cache: dict[str, str] = {}
    for coords in cells:
        acc = list(coords)[1]
        if acc not in at_cache:
            try:
                at = tm1.elements.get_attribute_value("account", "account", acc, "account_type")
                at_cache[acc] = str(at).strip().upper() if at else ""
            except Exception:
                at_cache[acc] = ""

    total = 0.0
    for coords, val in cells.items():
        acc = list(coords)[1]
        if at_cache.get(acc, "") in account_types:
            total += (val or 0)
    return round(total * sign, 2)


def _compute_cashflow_activity(
    tm1: TM1Service, params: dict, year: str, month: str
) -> float:
    element = params.get("element", "")
    for cube in ["cashflow_cal_metrics", "cashflow_pln_forecast"]:
        try:
            mdx = f"""
            SELECT {{[measure_{cube}].[amount]}} ON 0,
                   {{[cashflow_activity].[{element}]}} ON 1
            FROM [{cube}]
            WHERE ([year].[{year}],[month].[{month}],[version].[actual],[entity].[All_Entity])
            """
            cells = tm1.cells.execute_mdx(mdx)
            for _, val in cells.items():
                return round(val or 0, 2)
        except Exception:
            continue
    return 0.0


def _compute_portfolio(
    tm1: TM1Service, params: dict, year: str, month: str
) -> float:
    measure = params.get("measure", "total_value")
    try:
        mdx = f"""
        SELECT {{[measure_listed_share_src_holdings].[{measure}]}} ON 0,
               {{[listed_share].[All_Listed_Share]}} ON 1
        FROM [listed_share_src_holdings]
        WHERE ([year].[{year}],[month].[{month}],[version].[actual],[entity].[All_Entity])
        """
        cells = tm1.cells.execute_mdx(mdx)
        for _, val in cells.items():
            return round(val or 0, 2)
    except Exception:
        return 0.0


def _compute_data_quality(
    tm1: TM1Service, params: dict, year: str, month: str
) -> float:
    check = params.get("check", "")

    if check == "unmapped_cashflow_accounts":
        try:
            from apps.ai_agent.skills.pattern_analysis import find_unmapped_cashflow_accounts
            result = find_unmapped_cashflow_accounts()
            return float(result.get("unmapped_count", 0))
        except Exception:
            return -1.0

    elif check == "gl_reconciliation_delta":
        try:
            from apps.ai_agent.skills.validation import reconcile_gl_totals
            result = reconcile_gl_totals(year, month)
            return abs(result.get("difference", 0))
        except Exception:
            return -1.0

    elif check == "model_structure_ok":
        try:
            from apps.ai_agent.skills.validation import verify_model_structure
            result = verify_model_structure()
            return 1.0 if result.get("passed") else 0.0
        except Exception:
            return -1.0

    return -1.0


def _compute_derived(computed: dict[str, float], params: dict) -> float:
    formula = params.get("formula", "")
    expr = formula
    for kpi_id, val in sorted(computed.items(), key=lambda x: -len(x[0])):
        expr = expr.replace(kpi_id, str(val))
    try:
        return round(eval(expr), 2)
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
#  Threshold evaluation
# ---------------------------------------------------------------------------

def _evaluate_thresholds(value: float, thresholds: dict) -> str:
    if thresholds.get("critical_below") is not None and value < thresholds["critical_below"]:
        return "critical"
    if thresholds.get("critical_above") is not None and value > thresholds["critical_above"]:
        return "critical"
    if thresholds.get("warning_below") is not None and value < thresholds["warning_below"]:
        return "warning"
    if thresholds.get("warning_above") is not None and value > thresholds["warning_above"]:
        return "warning"
    return "ok"


# ---------------------------------------------------------------------------
#  Public tool functions
# ---------------------------------------------------------------------------

def get_current_period() -> dict[str, Any]:
    """Return the current period (year, month, financial year) from sys_parameters."""
    try:
        with TM1Service(**TM1_CONFIG) as tm1:
            return _get_current_period(tm1)
    except Exception as e:
        return {"error": str(e)}


def get_all_kpi_values(
    year: str = "",
    month: str = "",
    entity: str = "All_Entity",
) -> dict[str, Any]:
    """
    Compute ALL KPIs defined in kpi_definitions.yaml for the given period.
    If year/month are empty, reads the current period from sys_parameters.

    Returns a dict with 'period', 'categories' (grouped), and 'kpis' (flat list).
    """
    kpi_defs = _load_kpi_definitions()
    if not kpi_defs:
        return {"error": "No KPIs defined in kpi_definitions.yaml"}

    try:
        with TM1Service(**TM1_CONFIG) as tm1:
            if not year or not month:
                period = _get_current_period(tm1)
                year = period["year"]
                month = period["month"]

            computed: dict[str, float] = {}

            # First pass: non-derived
            for kpi in kpi_defs:
                if kpi.get("source_type") == "derived":
                    continue
                kpi_id = kpi["id"]
                params = kpi.get("source_params", {})
                source = kpi.get("source_type", "")
                try:
                    if source == "gl_by_type":
                        val = _compute_gl_by_type(tm1, params, year, month)
                    elif source == "cashflow_activity":
                        val = _compute_cashflow_activity(tm1, params, year, month)
                    elif source == "portfolio":
                        val = _compute_portfolio(tm1, params, year, month)
                    elif source == "data_quality":
                        val = _compute_data_quality(tm1, params, year, month)
                    else:
                        val = 0.0
                except Exception:
                    val = 0.0
                computed[kpi_id] = val

            # Second pass: derived
            for kpi in kpi_defs:
                if kpi.get("source_type") != "derived":
                    continue
                kpi_id = kpi["id"]
                params = kpi.get("source_params", {})
                val = _compute_derived(computed, params)
                computed[kpi_id] = val

            # Build results
            results: list[dict] = []
            for kpi in kpi_defs:
                kpi_id = kpi["id"]
                val = computed.get(kpi_id, 0.0)
                thresholds = kpi.get("thresholds", {})
                status = _evaluate_thresholds(val, thresholds) if thresholds else "ok"
                results.append({
                    "id": kpi_id,
                    "name": kpi["name"],
                    "category": kpi.get("category", ""),
                    "description": kpi.get("description", ""),
                    "value": val,
                    "format": kpi.get("format", "number"),
                    "status": status,
                })

        categories: dict[str, list[dict]] = {}
        for r in results:
            categories.setdefault(r["category"], []).append(r)

        return {
            "period": {"year": year, "month": month},
            "kpis": results,
            "categories": categories,
        }
    except Exception as e:
        return {"error": str(e)}


def list_kpi_definitions() -> dict[str, Any]:
    """Return all KPI definitions from kpi_definitions.yaml (without computing values)."""
    kpi_defs = _load_kpi_definitions()
    categories: dict[str, list[dict]] = {}
    for kpi in kpi_defs:
        cat = kpi.get("category", "Uncategorised")
        categories.setdefault(cat, []).append({
            "id": kpi["id"],
            "name": kpi["name"],
            "description": kpi.get("description", ""),
            "source_type": kpi.get("source_type", ""),
            "format": kpi.get("format", "number"),
            "has_thresholds": bool(kpi.get("thresholds")),
        })
    return {"kpi_count": len(kpi_defs), "categories": categories}


def add_kpi_definition(
    kpi_id: str,
    name: str,
    category: str,
    description: str = "",
    source_type: str = "gl_by_type",
    source_params: dict | None = None,
    kpi_format: str = "currency",
    thresholds: dict | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """
    Add a new KPI definition to kpi_definitions.yaml.
    Set confirm=True to actually write.

    kpi_id: Unique id e.g. 'gl_rental_income'
    name: Display name e.g. 'Rental Income'
    category: 'GL', 'Cashflow', 'Listed Shares', or 'Data Quality'
    source_type: 'gl_by_type', 'cashflow_activity', 'portfolio', 'data_quality', 'derived'
    source_params: Source-specific parameters (varies by source_type)
    kpi_format: 'currency', 'number', or 'percentage'
    thresholds: Optional warning_above/below, critical_above/below
    confirm: Must be True to write.
    """
    new_kpi = {
        "id": kpi_id, "name": name, "category": category,
        "description": description, "source_type": source_type,
        "source_params": source_params or {}, "format": kpi_format,
    }
    if thresholds:
        new_kpi["thresholds"] = thresholds

    if not confirm:
        return {"status": "dry_run", "kpi": new_kpi,
                "message": "Set confirm=True to add this KPI to kpi_definitions.yaml"}

    kpi_defs = _load_kpi_definitions()
    if kpi_id in {k["id"] for k in kpi_defs}:
        return {"error": f"KPI '{kpi_id}' already exists. Remove it first."}

    kpi_defs.append(new_kpi)
    _save_kpi_definitions(kpi_defs)
    return {"status": "success", "kpi": new_kpi, "total_kpis": len(kpi_defs)}


def remove_kpi_definition(
    kpi_id: str,
    confirm: bool = False,
) -> dict[str, Any]:
    """
    Remove a KPI from kpi_definitions.yaml by id.
    Set confirm=True to actually remove.
    """
    kpi_defs = _load_kpi_definitions()
    match = [k for k in kpi_defs if k["id"] == kpi_id]
    if not match:
        return {"error": f"No KPI with id '{kpi_id}'"}
    if not confirm:
        return {"status": "dry_run", "kpi_to_remove": match[0],
                "message": f"Set confirm=True to remove '{kpi_id}'"}

    kpi_defs = [k for k in kpi_defs if k["id"] != kpi_id]
    _save_kpi_definitions(kpi_defs)
    return {"status": "success", "removed": kpi_id, "remaining": len(kpi_defs)}


def get_kpi_dashboard() -> dict[str, Any]:
    """Shortcut: compute all KPIs for current period."""
    return get_all_kpi_values()


def get_gl_summary(
    year: str, month: str, entity: str = "All_Entity", version: str = "actual"
) -> dict[str, Any]:
    """Return GL summary (income, expenses, net profit) for a specific period."""
    try:
        with TM1Service(**TM1_CONFIG) as tm1:
            income = _compute_gl_by_type(
                tm1, {"account_types": ["REVENUE"], "sign": -1}, year, month)
            expenses = _compute_gl_by_type(
                tm1, {"account_types": ["EXPENSE", "DIRECTCOSTS", "OVERHEADS"], "sign": 1}, year, month)
        return {
            "year": year, "month": month, "entity": entity, "version": version,
            "total_income": income, "total_expenses": expenses,
            "net_profit": round(income - expenses, 2),
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
#  Tool schemas
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "name": "get_current_period",
        "description": "Return the current period (year, month, financial year) from sys_parameters.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_all_kpi_values",
        "description": "Compute ALL KPIs from kpi_definitions.yaml for a period. Returns values grouped by category with threshold status.",
        "input_schema": {
            "type": "object",
            "properties": {
                "year": {"type": "string", "description": "e.g. '2025'. Leave empty for current."},
                "month": {"type": "string", "description": "e.g. 'Jul'. Leave empty for current."},
                "entity": {"type": "string", "description": "Default 'All_Entity'"},
            },
        },
    },
    {
        "name": "list_kpi_definitions",
        "description": "List all KPI definitions from kpi_definitions.yaml without computing values.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "add_kpi_definition",
        "description": "Add a new KPI to kpi_definitions.yaml. Set confirm=True to write. source_type: gl_by_type, cashflow_activity, portfolio, data_quality, derived.",
        "input_schema": {
            "type": "object",
            "properties": {
                "kpi_id": {"type": "string", "description": "Unique id e.g. 'gl_rental_income'"},
                "name": {"type": "string", "description": "Display name"},
                "category": {"type": "string", "description": "'GL', 'Cashflow', 'Listed Shares', 'Data Quality'"},
                "description": {"type": "string"},
                "source_type": {"type": "string"},
                "source_params": {"type": "object"},
                "kpi_format": {"type": "string", "description": "'currency', 'number', 'percentage'"},
                "thresholds": {"type": "object"},
                "confirm": {"type": "boolean"},
            },
            "required": ["kpi_id", "name", "category"],
        },
    },
    {
        "name": "remove_kpi_definition",
        "description": "Remove a KPI from kpi_definitions.yaml by id. Set confirm=True to remove.",
        "input_schema": {
            "type": "object",
            "properties": {
                "kpi_id": {"type": "string"},
                "confirm": {"type": "boolean"},
            },
            "required": ["kpi_id"],
        },
    },
    {
        "name": "get_gl_summary",
        "description": "Return GL summary (income, expenses, net profit) for a period and entity.",
        "input_schema": {
            "type": "object",
            "properties": {
                "year": {"type": "string"},
                "month": {"type": "string"},
                "entity": {"type": "string"},
                "version": {"type": "string"},
            },
            "required": ["year", "month"],
        },
    },
    {
        "name": "get_kpi_dashboard",
        "description": "Shortcut: compute all KPIs for the current period. Same as get_all_kpi_values with no arguments.",
        "input_schema": {"type": "object", "properties": {}},
    },
]

TOOL_FUNCTIONS = {
    "get_current_period": get_current_period,
    "get_all_kpi_values": get_all_kpi_values,
    "list_kpi_definitions": list_kpi_definitions,
    "add_kpi_definition": add_kpi_definition,
    "remove_kpi_definition": remove_kpi_definition,
    "get_kpi_dashboard": get_kpi_dashboard,
    "get_gl_summary": get_gl_summary,
}
