"""
Skill: Model Validation & Testing
Verify model structure, reconcile GL totals, test connections.
Reuses expected object lists from scripts/verify_model.py.
"""
from __future__ import annotations

import sys
import os
from typing import Any

import psycopg2

from apps.ai_agent.agent.config import TM1_CONFIG, settings
from TM1py import TM1Service

# Expected user dimensions (from verify_model.py)
EXPECTED_DIMENSIONS = [
    "account", "contact", "cost_object", "entity",
    "financial_year", "hier_level", "hier_name",
    "investec_account", "listed_share", "listed_share_dividend_type",
    "listed_share_transaction_type", "measure_gl_pln_forecast",
    "measure_gl_rpt_trial_balance", "measure_gl_src_trial_balance",
    "measure_hier_cnt_account", "measure_hier_cnt_config",
    "measure_hier_cnt_listed_share", "measure_listed_share_cal_flow_metrics",
    "measure_listed_share_pln_forecast", "measure_listed_share_src_holdings",
    "measure_listed_share_src_transactions", "month", "period",
    "sys_measure_parameters", "sys_module",
    "tracking_1", "tracking_2", "version", "year",
]

# Expected cubes
EXPECTED_CUBES = [
    "gl_src_trial_balance", "gl_pln_forecast", "gl_rpt_trial_balance",
    "hier_cnt_account", "hier_cnt_config", "hier_cnt_listed_share",
    "listed_share_cal_flow_metrics", "listed_share_pln_forecast",
    "listed_share_src_holdings", "listed_share_src_transactions",
    "sys_parameters", "sys_rpt_financial_year", "sys_rpt_period",
]

# Cubes that should have rules
CUBES_WITH_RULES = [
    "gl_src_trial_balance", "gl_pln_forecast", "listed_share_pln_forecast",
]


def verify_model_structure() -> dict[str, Any]:
    """
    Check that all expected TM1 dimensions and cubes exist on the server.
    Returns a pass/fail report with lists of any missing objects.
    """
    try:
        with TM1Service(**TM1_CONFIG) as tm1:
            existing_dims = set(
                d for d in tm1.dimensions.get_all_names() if not d.startswith("}")
            )
            existing_cubes = set(
                c for c in tm1.cubes.get_all_names() if not c.startswith("}")
            )
            # Check rules
            rules_status = {}
            for cube_name in CUBES_WITH_RULES:
                if cube_name in existing_cubes:
                    try:
                        cube = tm1.cubes.get(cube_name)
                        rules_status[cube_name] = cube.has_rules
                    except Exception:
                        rules_status[cube_name] = None

        missing_dims = [d for d in EXPECTED_DIMENSIONS if d not in existing_dims]
        missing_cubes = [c for c in EXPECTED_CUBES if c not in existing_cubes]
        rules_missing = [c for c, has in rules_status.items() if not has]

        all_ok = not missing_dims and not missing_cubes and not rules_missing

        return {
            "passed": all_ok,
            "dimensions_checked": len(EXPECTED_DIMENSIONS),
            "cubes_checked": len(EXPECTED_CUBES),
            "missing_dimensions": missing_dims,
            "missing_cubes": missing_cubes,
            "rules_missing_on": rules_missing,
            "existing_dim_count": len(existing_dims),
            "existing_cube_count": len(existing_cubes),
        }
    except Exception as e:
        return {"error": str(e)}


def reconcile_gl_totals(year: str, month: str) -> dict[str, Any]:
    """
    Reconcile total GL amount in TM1 gl_src_trial_balance against PostgreSQL source.
    Confirms that the last import loaded correctly.

    year: Calendar year, e.g. '2025'
    month: Month name, e.g. 'Jul'
    """
    month_map = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
                 "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}
    month_num = month_map.get(month)
    if not month_num:
        return {"error": f"Unrecognised month: {month}. Use 3-letter format e.g. 'Jul'"}

    # TM1 total
    mdx = f"""
    SELECT {{[measure_gl_src_trial_balance].[amount]}} ON 0,
           {{[entity].[All_Entity]}} ON 1
    FROM [gl_src_trial_balance]
    WHERE ([year].[{year}],[month].[{month}],[version].[actual],
           [account].[All_Account],[contact].[All_Contact],
           [tracking_1].[All_Tracking_1],[tracking_2].[All_Tracking_2])
    """
    try:
        with TM1Service(**TM1_CONFIG) as tm1:
            cells = tm1.cells.execute_mdx(mdx)
        tm1_total = sum(v for v in cells.values() if v)
    except Exception as e:
        return {"error": f"TM1 query failed: {e}"}

    # PostgreSQL total
    sql = """
        SELECT COALESCE(SUM(amount), 0) AS total
        FROM xero_cube_xerotrailbalance
        WHERE year = %s AND month = %s
    """
    try:
        with psycopg2.connect(
            host=settings.pg_financials_host,
            port=settings.pg_financials_port,
            dbname=settings.pg_financials_db,
            user=settings.pg_financials_user,
            password=settings.pg_financials_password,
        ) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (int(year), month_num))
                pg_total = float(cur.fetchone()[0])
    except Exception as e:
        return {"error": f"PostgreSQL query failed: {e}"}

    diff = tm1_total - pg_total
    return {
        "year": year,
        "month": month,
        "tm1_total": round(tm1_total, 2),
        "pg_total": round(pg_total, 2),
        "difference": round(diff, 2),
        "reconciled": abs(diff) < 1.0,
        "note": "Difference < 1.0 is considered reconciled (rounding tolerance)",
    }


def test_tm1_connection() -> dict[str, Any]:
    """
    Test connectivity to the TM1 server. Returns server name and status.
    """
    try:
        with TM1Service(**TM1_CONFIG) as tm1:
            server_name = tm1.server.get_server_name()
            dim_count = len([d for d in tm1.dimensions.get_all_names()
                             if not d.startswith("}")])
        return {
            "status": "connected",
            "server": server_name,
            "host": settings.tm1_host,
            "port": settings.tm1_port,
            "user_dimension_count": dim_count,
        }
    except Exception as e:
        return {"status": "failed", "error": str(e)}


def test_postgres_connections() -> dict[str, Any]:
    """
    Test connectivity to both PostgreSQL databases (klikk_financials and klikk_bi_etl).
    Financials DB name is from settings.pg_financials_db (e.g. klikk_financials_db or klikk_financials_v4).
    """
    results = {}
    for name, dsn in [
        (settings.pg_financials_db, dict(
            host=settings.pg_financials_host, port=settings.pg_financials_port,
            dbname=settings.pg_financials_db, user=settings.pg_financials_user,
            password=settings.pg_financials_password,
        )),
        (settings.pg_bi_db, dict(
            host=settings.pg_bi_host, port=settings.pg_bi_port,
            dbname=settings.pg_bi_db, user=settings.pg_bi_user,
            password=settings.pg_bi_password,
        )),
    ]:
        try:
            with psycopg2.connect(**dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT version()")
                    ver = cur.fetchone()[0]
            results[name] = {"status": "connected", "version": ver[:60]}
        except Exception as e:
            results[name] = {"status": "failed", "error": str(e)}

    return {"databases": results}


# --- Tool schemas ---

TOOL_SCHEMAS = [
    {
        "name": "verify_model_structure",
        "description": "Check that all expected TM1 dimensions and cubes exist. Returns pass/fail with missing object lists.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "reconcile_gl_totals",
        "description": "Compare TM1 gl_src_trial_balance total vs PostgreSQL source for a period. Confirms data loaded correctly.",
        "input_schema": {
            "type": "object",
            "properties": {
                "year": {"type": "string", "description": "Calendar year e.g. '2025'"},
                "month": {"type": "string", "description": "Month name e.g. 'Jul'"},
            },
            "required": ["year", "month"],
        },
    },
    {
        "name": "test_tm1_connection",
        "description": "Test connectivity to the TM1 server and return server info.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "test_postgres_connections",
        "description": "Test connectivity to both PostgreSQL databases (klikk_financials_v4 and klikk_bi_etl).",
        "input_schema": {"type": "object", "properties": {}},
    },
]

TOOL_FUNCTIONS = {
    "verify_model_structure": verify_model_structure,
    "reconcile_gl_totals": reconcile_gl_totals,
    "test_tm1_connection": test_tm1_connection,
    "test_postgres_connections": test_postgres_connections,
}
