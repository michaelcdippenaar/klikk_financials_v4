"""
Skill: TM1 REST API — Direct OData v4 access to IBM Planning Analytics.

Provides tools for operations that TM1py doesn't support well:
- Server monitoring (threads, sessions, message log, transaction log)
- Sandbox management (create, list, activate, delete)
- Cellset operations (execute MDX, read cells, write cells)
- Server administration (configuration, error logs)
- Audit trail (transaction log, message log)

All errors are logged via the structured logger.
"""
from __future__ import annotations

import sys
import os
import urllib3
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

import logging

from apps.ai_agent.agent.config import settings
from apps.ai_agent.tm1.tm1_session import get_tm1_auth as _get_user_tm1_auth

log = logging.getLogger('ai_agent')

# Suppress InsecureRequestWarning for verify=False
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_BASE_URL = f"http://{settings.tm1_host}:{settings.tm1_port}/api/v1"
_AUTH = HTTPBasicAuth(settings.tm1_user, settings.tm1_password)
_HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _request(method: str, path: str, json_body: dict | None = None,
             params: dict | None = None, timeout: int = 30,
             username: str | None = None) -> dict[str, Any]:
    """Make a TM1 REST API request with error handling and logging.

    If *username* is provided, uses the stored per-user TM1 auth;
    otherwise falls back to the default admin Basic auth from config.
    """
    url = f"{_BASE_URL}/{path}"
    auth = _get_user_tm1_auth(username) if username else _AUTH
    try:
        resp = requests.request(
            method, url, auth=auth, headers=_HEADERS,
            json=json_body, params=params, timeout=timeout, verify=False,
        )
        if resp.status_code >= 400:
            error_detail = resp.text
            try:
                error_json = resp.json()
                error_detail = error_json.get("error", {}).get("message", resp.text)
            except Exception:
                pass
            log.error(
                "TM1 REST %s %s -> %s: %s", method, path, resp.status_code, error_detail,
                extra={"tool": "tm1_rest_api", "detail": f"{method} {path}",
                       "error_type": f"http_{resp.status_code}"},
            )
            return {"error": f"HTTP {resp.status_code}: {error_detail}"}
        if resp.status_code == 204:
            return {"status": "ok"}
        return resp.json()
    except requests.exceptions.ConnectionError as e:
        log.error("TM1 REST connection failed: %s", e,
                  extra={"tool": "tm1_rest_api", "error_type": "connection_error"})
        return {"error": f"Cannot connect to TM1 at {_BASE_URL}: {e}"}
    except Exception as e:
        log.error("TM1 REST error: %s", e,
                  extra={"tool": "tm1_rest_api", "error_type": type(e).__name__},
                  exc_info=True)
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# 1. Server status
# ---------------------------------------------------------------------------

def tm1_rest_server_status() -> dict[str, Any]:
    """
    Get TM1 server health: version, server name, active sessions, threads summary.
    """
    result: dict[str, Any] = {}

    # Configuration (ProductVersion, ServerName)
    config = _request("GET", "Configuration")
    if "error" in config:
        return config
    result["product_version"] = config.get("ProductVersion", "unknown")
    result["server_name"] = config.get("ServerName", "unknown")
    result["admin_host"] = config.get("AdminHost", "")
    result["port_number"] = config.get("PortNumber", "")

    # Active sessions
    sessions = _request("GET", "Sessions", params={"$count": "true"})
    if "error" not in sessions:
        session_list = sessions.get("value", [])
        result["active_session_count"] = len(session_list)
        result["active_users"] = [s.get("User", {}).get("Name", s.get("ID", ""))
                                  for s in session_list]

    # Active threads summary
    threads = _request("GET", "Threads")
    if "error" not in threads:
        thread_list = threads.get("value", [])
        result["thread_count"] = len(thread_list)
        states = {}
        for t in thread_list:
            state = t.get("State", "Unknown")
            states[state] = states.get(state, 0) + 1
        result["thread_states"] = states

    return result


# ---------------------------------------------------------------------------
# 2. Message log
# ---------------------------------------------------------------------------

def tm1_rest_message_log(top: int = 20, level: str | None = None,
                         logger: str | None = None,
                         since: str | None = None) -> dict[str, Any]:
    """
    Read TM1 server message log. Critical for debugging MDX errors
    like 'member not found' BEFORE they hit our app.

    top: Number of entries to return (default 20).
    level: Filter by level — 'Error', 'Warning', 'Info', etc.
    logger: Filter by logger name.
    since: ISO timestamp to filter entries after, e.g. '2025-01-01T00:00:00'.
    """
    params: dict[str, str] = {
        "$top": str(top),
        "$orderby": "TimeStamp desc",
    }
    filters: list[str] = []
    if level:
        filters.append(f"Level eq '{level}'")
    if logger:
        filters.append(f"Logger eq '{logger}'")
    if since:
        filters.append(f"TimeStamp ge {since}")
    if filters:
        params["$filter"] = " and ".join(filters)

    data = _request("GET", "MessageLogEntries", params=params)
    if "error" in data:
        return data

    entries = data.get("value", [])
    result = []
    for e in entries:
        result.append({
            "timestamp": e.get("TimeStamp", ""),
            "level": e.get("Level", ""),
            "logger": e.get("Logger", ""),
            "message": e.get("Message", ""),
        })
    return {"entries": result, "count": len(result)}


# ---------------------------------------------------------------------------
# 3. Transaction log
# ---------------------------------------------------------------------------

def tm1_rest_transaction_log(top: int = 20, cube: str | None = None,
                             since: str | None = None) -> dict[str, Any]:
    """
    Read TM1 transaction/audit log — shows who wrote what data and when.

    top: Number of entries (default 20).
    cube: Filter by cube name.
    since: ISO timestamp to filter entries after.
    """
    params: dict[str, str] = {
        "$top": str(top),
        "$orderby": "TimeStamp desc",
    }
    filters: list[str] = []
    if cube:
        filters.append(f"Cube eq '{cube}'")
    if since:
        filters.append(f"TimeStamp ge {since}")
    if filters:
        params["$filter"] = " and ".join(filters)

    data = _request("GET", "TransactionLogEntries", params=params)
    if "error" in data:
        return data

    entries = data.get("value", [])
    result = []
    for e in entries:
        result.append({
            "timestamp": e.get("TimeStamp", ""),
            "user": e.get("User", ""),
            "cube": e.get("Cube", ""),
            "tuple": e.get("Tuple", []),
            "old_value": e.get("OldValue", ""),
            "new_value": e.get("NewValue", ""),
            "status_message": e.get("StatusMessage", ""),
        })
    return {"entries": result, "count": len(result)}


# ---------------------------------------------------------------------------
# 4. Active sessions
# ---------------------------------------------------------------------------

def tm1_rest_active_sessions() -> dict[str, Any]:
    """List active TM1 sessions with user, ID, and connection details."""
    data = _request("GET", "Sessions")
    if "error" in data:
        return data

    sessions = data.get("value", [])
    result = []
    for s in sessions:
        result.append({
            "id": s.get("ID", ""),
            "user": s.get("User", {}).get("Name", "") if isinstance(s.get("User"), dict) else str(s.get("User", "")),
            "active": s.get("Active", ""),
            "last_active": s.get("LastActive", ""),
            "threads": s.get("Threads", []),
        })
    return {"sessions": result, "count": len(result)}


# ---------------------------------------------------------------------------
# 5. Active threads
# ---------------------------------------------------------------------------

def tm1_rest_active_threads() -> dict[str, Any]:
    """
    List active threads — shows what's running, blocked, or holding locks.
    """
    data = _request("GET", "Threads")
    if "error" in data:
        return data

    threads = data.get("value", [])
    result = []
    long_running = []
    for t in threads:
        entry = {
            "id": t.get("ID", ""),
            "type": t.get("Type", ""),
            "state": t.get("State", ""),
            "function": t.get("Function", ""),
            "object_name": t.get("ObjectName", ""),
            "object_type": t.get("ObjectType", ""),
            "elapsed_time": t.get("ElapsedTime", ""),
            "wait_time": t.get("WaitTime", ""),
            "info": t.get("Info", ""),
        }
        result.append(entry)
        # Flag threads with non-idle states or long wait times
        state = t.get("State", "")
        if state not in ("Idle", ""):
            long_running.append(entry)

    return {
        "threads": result,
        "count": len(result),
        "active_non_idle": long_running,
        "non_idle_count": len(long_running),
    }


# ---------------------------------------------------------------------------
# 6. Sandbox list
# ---------------------------------------------------------------------------

def tm1_rest_sandbox_list() -> dict[str, Any]:
    """List all sandboxes on the TM1 server."""
    data = _request("GET", "Sandboxes")
    if "error" in data:
        return data

    sandboxes = data.get("value", [])
    result = []
    for s in sandboxes:
        result.append({
            "name": s.get("Name", ""),
            "id": s.get("ID", ""),
            "is_active": s.get("IsActive", False),
            "is_loaded": s.get("IsLoaded", False),
            "queued_changes": s.get("QueuedChanges", 0),
        })
    return {"sandboxes": result, "count": len(result)}


# ---------------------------------------------------------------------------
# 7. Sandbox create
# ---------------------------------------------------------------------------

def tm1_rest_sandbox_create(name: str) -> dict[str, Any]:
    """
    Create a sandbox for what-if analysis.

    name: Name for the new sandbox.
    """
    data = _request("POST", "Sandboxes", json_body={"Name": name})
    if "error" in data:
        return data
    return {"status": "created", "name": name, "detail": data}


# ---------------------------------------------------------------------------
# 8. Sandbox delete
# ---------------------------------------------------------------------------

def tm1_rest_sandbox_delete(name: str) -> dict[str, Any]:
    """
    Delete a sandbox by name.

    name: Exact sandbox name to delete.
    """
    # URL-encode the sandbox name for the OData path
    safe_name = name.replace("'", "''")
    data = _request("DELETE", f"Sandboxes('{safe_name}')")
    if "error" in data:
        return data
    return {"status": "deleted", "name": name}


# ---------------------------------------------------------------------------
# 9. Execute MDX via REST (full cellset)
# ---------------------------------------------------------------------------

def tm1_rest_execute_mdx_cellset(mdx: str, top: int = 1000) -> dict[str, Any]:
    """
    Execute MDX via REST API and return the full cellset with axis members.
    Bypasses TM1py for richer cellset info (axis tuples, ordinals).

    mdx: Full MDX SELECT statement.
    top: Max cells to return (default 1000).
    """
    # Step 1: Execute MDX to get cellset ID
    exec_result = _request("POST", "ExecuteMDX", json_body={"MDX": mdx})
    if "error" in exec_result:
        return exec_result

    cellset_id = exec_result.get("ID")
    if not cellset_id:
        log.error("TM1 REST ExecuteMDX returned no cellset ID",
                  extra={"tool": "tm1_rest_api", "detail": "ExecuteMDX",
                         "error_type": "missing_cellset_id"})
        return {"error": "ExecuteMDX returned no cellset ID", "raw": exec_result}

    # Step 2: Read cellset with cells and axis members
    expand = (
        "Cells($select=Value,Ordinal;"
        f"$top={top}),"
        "Axes($expand=Tuples($expand=Members($select=Name,UniqueName)))"
    )
    cellset = _request("GET", f"Cellsets('{cellset_id}')", params={"$expand": expand})

    # Step 3: Clean up cellset on server (best effort)
    _request("DELETE", f"Cellsets('{cellset_id}')")

    if "error" in cellset:
        return cellset

    # Parse axes
    axes = []
    for ax in cellset.get("Axes", []):
        tuples = []
        for tup in ax.get("Tuples", []):
            members = [m.get("Name", "") for m in tup.get("Members", [])]
            tuples.append(members)
        axes.append({
            "ordinal": ax.get("Ordinal", ""),
            "cardinality": ax.get("Cardinality", len(tuples)),
            "tuples": tuples,
        })

    # Parse cells
    cells = []
    for c in cellset.get("Cells", []):
        cells.append({
            "ordinal": c.get("Ordinal", ""),
            "value": c.get("Value"),
        })

    return {
        "cellset_id": cellset_id,
        "axes": axes,
        "cells": cells,
        "cell_count": len(cells),
    }


# ---------------------------------------------------------------------------
# 10. Execute named view via REST (POST tm1.Execute)
# ---------------------------------------------------------------------------

def tm1_rest_execute_view(cube_name: str, view_name: str,
                          private: bool = False,
                          top: int = 1000) -> dict[str, Any]:
    """
    Execute a named TM1 view via REST POST and return cell data with axis members.
    Uses the correct POST method for tm1.Execute (GET returns blank).

    cube_name: Exact cube name, e.g. 'listed_share_src_transactions'
    view_name: Exact view name, e.g. 'rpt_buy and sell trades'
    private: If true, use private view (default false = public)
    top: Max cells to return (default 1000)
    """
    safe_cube = cube_name.replace("'", "''")
    safe_view = view_name.replace("'", "''")

    view_collection = "PrivateViews" if private else "Views"
    path = f"Cubes('{safe_cube}')/{view_collection}('{safe_view}')/tm1.Execute"

    expand = (
        "Axes($expand=Tuples($expand=Members($select=Name))),"
        f"Cells($select=Value,Ordinal;$top={top})"
    )
    data = _request("POST", path, json_body={}, params={"$expand": expand})
    if "error" in data:
        return data

    # Parse axes into readable tuples
    axes = []
    for ax in data.get("Axes", []):
        tuples = []
        for tup in ax.get("Tuples", []):
            members = [m.get("Name", "") for m in tup.get("Members", [])]
            tuples.append(members)
        axes.append({
            "ordinal": ax.get("Ordinal", ""),
            "cardinality": len(tuples),
            "tuples": tuples[:200],
        })

    # Parse cells
    cells = data.get("Cells", [])
    cell_values = [{"ordinal": c.get("Ordinal", ""), "value": c.get("Value")} for c in cells]

    # Build a tabular view if possible (2-axis: rows x columns)
    rows = []
    if len(axes) >= 2:
        col_tuples = axes[0].get("tuples", [])
        row_tuples = axes[1].get("tuples", [])
        num_cols = len(col_tuples)
        for r_idx, row_tup in enumerate(row_tuples):
            row = {"_row": " | ".join(row_tup)}
            for c_idx, col_tup in enumerate(col_tuples):
                cell_idx = r_idx * num_cols + c_idx
                val = cells[cell_idx].get("Value") if cell_idx < len(cells) else None
                col_label = " | ".join(col_tup)
                row[col_label] = val
            rows.append(row)

    return {
        "cube": cube_name,
        "view": view_name,
        "axes": axes,
        "cells": cell_values[:top],
        "cell_count": len(cell_values),
        "table": rows[:500] if rows else [],
        "table_row_count": len(rows),
    }


# ---------------------------------------------------------------------------
# 11. Write values via REST (formerly 10)
# ---------------------------------------------------------------------------

def tm1_rest_write_values(cube_name: str, cells: list,
                          confirm: bool = False) -> dict[str, Any]:
    """
    Write cell values via REST API.
    IMPORTANT: set confirm=True to actually write. Default is dry-run.

    cube_name: Exact cube name.
    cells: List of dicts, each with 'coordinates' (list of element names) and 'value'.
           Example: [{"coordinates": ["2025","Jan","actual","Klikk_Org","acc_001",
                       "All_Contact","All_Tracking_1","All_Tracking_2","amount"],
                      "value": 12345.67}]
    confirm: Must be True to write. Default False (safe dry-run).
    """
    if not confirm:
        return {
            "status": "dry_run",
            "cube": cube_name,
            "cell_count": len(cells),
            "cells_preview": cells[:5],
            "message": "Dry run only. Set confirm=True to actually write values.",
        }

    # Build the update payload
    # TM1 REST write: POST /Cubes('{name}')/tm1.Update
    # Body: {"Cells": [{"Tuple@odata.bind": ["Dimensions('d1')/Hierarchies('d1')/Elements('e1')", ...], "Value": ...}]}
    # Alternative simpler approach: write one cell at a time via cellset
    safe_name = cube_name.replace("'", "''")
    errors = []
    written = 0

    for cell in cells:
        coords = cell.get("coordinates", [])
        value = cell.get("value")
        if not coords:
            errors.append({"cell": cell, "error": "Missing coordinates"})
            continue

        # Use ExecuteMDX to create a single-cell cellset, then write
        # Build a simple MDX that targets the exact cell
        # This approach works universally
        elements_str = ",".join(str(c) for c in coords)
        write_body = {
            "Cells": [{}],
            "Value": str(value),
        }
        # Use the direct cell update endpoint
        result = _request(
            "POST",
            f"Cubes('{safe_name}')/tm1.Update",
            json_body={
                "Cells": [{
                    "Tuple@odata.bind": [
                        f"Dimensions('{safe_name}')/Hierarchies('{safe_name}')/Elements('{c}')"
                        for c in coords
                    ],
                    "Value": value,
                }],
            },
        )
        if "error" in result:
            errors.append({"coordinates": coords, "error": result["error"]})
        else:
            written += 1

    return {
        "status": "completed",
        "cube": cube_name,
        "cells_written": written,
        "errors": errors,
        "error_count": len(errors),
    }


# ---------------------------------------------------------------------------
# 11. Error log files
# ---------------------------------------------------------------------------

def tm1_rest_error_log() -> dict[str, Any]:
    """Read TM1 error log file listing."""
    data = _request("GET", "ErrorLogFiles")
    if "error" in data:
        return data

    files = data.get("value", [])
    result = []
    for f in files:
        result.append({
            "filename": f.get("Filename", ""),
            "size": f.get("Size", 0),
            "last_updated": f.get("LastUpdated", ""),
        })
    return {"error_log_files": result, "count": len(result)}


# ---------------------------------------------------------------------------
# 12. Cube info via REST
# ---------------------------------------------------------------------------

def tm1_rest_cube_info(cube_name: str) -> dict[str, Any]:
    """
    Detailed cube info via REST: dimensions, rules, views.

    cube_name: Exact cube name.
    """
    safe_name = cube_name.replace("'", "''")
    expand = "Dimensions($select=Name),Views($select=Name)"
    data = _request("GET", f"Cubes('{safe_name}')", params={"$expand": expand, "$select": "Name,Rules,LastDataUpdate"})
    if "error" in data:
        return data

    dims = [d.get("Name", "") for d in data.get("Dimensions", [])]
    views = [v.get("Name", "") for v in data.get("Views", [])]
    rules_text = data.get("Rules", "") or ""
    has_rules = bool(rules_text.strip())

    return {
        "cube": data.get("Name", cube_name),
        "dimensions": dims,
        "dimension_count": len(dims),
        "views": views,
        "view_count": len(views),
        "has_rules": has_rules,
        "rules_preview": rules_text[:500] if has_rules else "",
        "last_data_update": data.get("LastDataUpdate", ""),
    }


# ---------------------------------------------------------------------------
# Tool schemas (Anthropic format)
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "name": "tm1_rest_server_status",
        "description": (
            "Get TM1 server health via REST API: product version, server name, "
            "active session count, active users, thread count and states."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "tm1_rest_message_log",
        "description": (
            "Read TM1 server message log via REST API. Critical for debugging — "
            "reveals MDX errors like 'member not found', TI process failures, "
            "and server warnings BEFORE they hit our app. Filter by level, logger, or timestamp."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "top": {"type": "integer", "description": "Number of log entries to return (default 20)"},
                "level": {"type": "string", "description": "Filter by level: 'Error', 'Warning', 'Info'"},
                "logger": {"type": "string", "description": "Filter by logger name"},
                "since": {"type": "string", "description": "ISO timestamp to filter entries after, e.g. '2025-01-01T00:00:00'"},
            },
        },
    },
    {
        "name": "tm1_rest_transaction_log",
        "description": (
            "Read TM1 transaction/audit log via REST API. Shows who wrote what data "
            "and when — essential for auditing data changes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "top": {"type": "integer", "description": "Number of entries (default 20)"},
                "cube": {"type": "string", "description": "Filter by cube name"},
                "since": {"type": "string", "description": "ISO timestamp to filter after"},
            },
        },
    },
    {
        "name": "tm1_rest_active_sessions",
        "description": "List all active TM1 sessions with user, ID, and connection details via REST API.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "tm1_rest_active_threads",
        "description": (
            "List active TM1 threads via REST API — shows what's currently running, "
            "blocked, or holding locks. Highlights non-idle threads."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "tm1_rest_sandbox_list",
        "description": "List all sandboxes on the TM1 server via REST API.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "tm1_rest_sandbox_create",
        "description": "Create a new TM1 sandbox for what-if analysis via REST API.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name for the new sandbox"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "tm1_rest_sandbox_delete",
        "description": "Delete a TM1 sandbox by name via REST API.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Exact sandbox name to delete"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "tm1_rest_execute_view",
        "description": (
            "Execute a named TM1 view via REST API (POST tm1.Execute). Returns cell data with axis members "
            "and a tabular representation. Use this to run saved views and get structured results. "
            "Uses POST (not GET) as required by the TM1 OData API."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cube_name": {"type": "string", "description": "Exact cube name, e.g. 'listed_share_src_transactions'"},
                "view_name": {"type": "string", "description": "Exact view name, e.g. 'rpt_buy and sell trades'"},
                "private": {"type": "boolean", "description": "True for private view (default false = public)"},
                "top": {"type": "integer", "description": "Max cells to return (default 1000)"},
            },
            "required": ["cube_name", "view_name"],
        },
    },
    {
        "name": "tm1_rest_execute_mdx_cellset",
        "description": (
            "ADMIN/FALLBACK MDX tool — execute MDX via REST API returning full cellset with axis tuples and ordinals. "
            "Only use this when you need axis metadata or tm1_query_mdx / tm1_execute_mdx_rows fail. "
            "For normal data queries, prefer tm1_query_mdx or tm1_execute_mdx_rows."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mdx": {"type": "string", "description": "Full MDX SELECT statement"},
                "top": {"type": "integer", "description": "Max cells to return (default 1000)"},
            },
            "required": ["mdx"],
        },
    },
    {
        "name": "tm1_rest_write_values",
        "description": (
            "Write cell values to a TM1 cube via REST API. "
            "IMPORTANT: set confirm=True to actually write. Default is safe dry-run."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cube_name": {"type": "string", "description": "Exact cube name"},
                "cells": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "coordinates": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Ordered element names matching cube dimension order",
                            },
                            "value": {"description": "Value to write (numeric or string)"},
                        },
                        "required": ["coordinates", "value"],
                    },
                    "description": "List of cells to write, each with coordinates and value",
                },
                "confirm": {"type": "boolean", "description": "Must be true to write. Default false (dry-run)."},
            },
            "required": ["cube_name", "cells"],
        },
    },
    {
        "name": "tm1_rest_error_log",
        "description": "Read TM1 error log file listing via REST API — shows server error log files with sizes and timestamps.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "tm1_rest_cube_info",
        "description": (
            "Get detailed cube info via TM1 REST API: dimensions, views, rules preview, "
            "and last data update timestamp."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cube_name": {"type": "string", "description": "Exact cube name"},
            },
            "required": ["cube_name"],
        },
    },
]

# ---------------------------------------------------------------------------
# Function registry
# ---------------------------------------------------------------------------

TOOL_FUNCTIONS = {
    "tm1_rest_server_status": tm1_rest_server_status,
    "tm1_rest_message_log": tm1_rest_message_log,
    "tm1_rest_transaction_log": tm1_rest_transaction_log,
    "tm1_rest_active_sessions": tm1_rest_active_sessions,
    "tm1_rest_active_threads": tm1_rest_active_threads,
    "tm1_rest_sandbox_list": tm1_rest_sandbox_list,
    "tm1_rest_sandbox_create": tm1_rest_sandbox_create,
    "tm1_rest_sandbox_delete": tm1_rest_sandbox_delete,
    "tm1_rest_execute_view": tm1_rest_execute_view,
    "tm1_rest_execute_mdx_cellset": tm1_rest_execute_mdx_cellset,
    "tm1_rest_write_values": tm1_rest_write_values,
    "tm1_rest_error_log": tm1_rest_error_log,
    "tm1_rest_cube_info": tm1_rest_cube_info,
}
