"""
Skill: PAW Integration — IBM Planning Analytics Workspace embedded widgets.

Uses the IBM Planning Analytics Workspace UI API to embed cube viewers,
dimension editors, set editors, websheets, and books as iframes in the portal.

API reference: https://ibm.github.io/planninganalyticsapi/
URL format:    GET {paw_base}/ui?type=<widget_type>&param1=val1&param2=val2

Authentication: TM1 native (mode 1) — POST {paw_base}/login
                Body: {"username": "<user>", "password": "<password>"}
                Returns x-csrf-token for subsequent non-GET requests.

Widget types (per official API):
  type=cube-viewer:      server (req), cube (req), view, private, toolbar, properties
  type=dimension-editor: server (req), dimension (req), hierarchy
  type=set-editor:       server (req), cube (req), dimension (req), uniqueName (req),
                         hierarchy, alias, private, dimensionCaption, hierarchyCaption
  type=book:             path (req) — use embed=true or perspective=dashboard for embedding
  type=websheet:         Passes through TM1 Web URL API params (Action, Workbook, etc.)
"""
from __future__ import annotations

import sys
import os
import urllib.parse
from typing import Any

import logging

from apps.ai_agent.agent.config import settings

log = logging.getLogger('ai_agent')


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def _paw_base_url() -> str:
    return f"http://{settings.paw_host}:{settings.paw_port}"


def _tm1_api_base() -> str:
    return f"http://{settings.tm1_host}:{settings.tm1_port}/api/v1"


_cached_server_name: str = ""


def _get_server_name() -> str:
    """Return the configured or auto-detected TM1 server name."""
    global _cached_server_name
    if settings.paw_server_name:
        return settings.paw_server_name
    if _cached_server_name:
        return _cached_server_name
    try:
        import requests
        resp = requests.get(
            f"{_tm1_api_base()}/Configuration",
            auth=(settings.tm1_user, settings.tm1_password),
            timeout=5, verify=False,
        )
        if resp.ok:
            _cached_server_name = resp.json().get("ServerName", "default")
            return _cached_server_name
    except Exception as e:
        log.warning("PAW skill: Could not auto-detect server name: %s", e,
                    extra={"tool": "paw_integration", "error_type": "server_detect"})
    return "default"


def _paw_ui_url(params: dict) -> str:
    """
    Build a PAW UI embed URL routed through the /paw/ reverse proxy.
    This keeps iframe requests same-origin, avoiding CORS/auth issues.
    The proxy strips /paw and forwards to the actual PAW server.
    """
    query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    return f"/paw/ui?{query}"


def _make_iframe_widget(
    title: str,
    paw_url: str,
    paw_type: str,
    width: int = 4,
    height: str = "xl",
    extra_props: dict | None = None,
) -> dict:
    """Create a widget config dict that the frontend renders as a PAW iframe."""
    import uuid
    widget_id = f"paw_{uuid.uuid4().hex[:8]}"
    props = {
        "pawUrl": paw_url,
        "pawType": paw_type,
        **(extra_props or {}),
    }
    widget_type_map = {
        "cube_viewer": "PAWCubeViewer",
        "dimension_editor": "PAWDimensionEditor",
        "set_editor": "PAWDimensionEditor",
        "book": "PAWBook",
        "websheet": "PAWCubeViewer",
    }
    return {
        "status": "widget_created",
        "message": f"Opened PAW {paw_type}: {title}",
        "widget": {
            "id": widget_id,
            "type": widget_type_map.get(paw_type, "PAWCubeViewer"),
            "title": title,
            "width": width,
            "height": height,
            "props": props,
        },
    }


# ---------------------------------------------------------------------------
#  PAW Session management
# ---------------------------------------------------------------------------

_paw_session_token: str = ""


def _paw_authenticate() -> str:
    """
    Authenticate with PAW using TM1 native credentials (mode 1).
    Per official API: POST {paw_base}/login with {"username": ..., "password": ...}
    Returns the x-csrf-token for subsequent non-GET requests.
    """
    global _paw_session_token
    if _paw_session_token:
        return _paw_session_token
    try:
        import requests
        resp = requests.post(
            f"http://{settings.paw_host}:{settings.paw_port}/login",
            json={"username": settings.tm1_user, "password": settings.tm1_password},
            timeout=10, verify=False,
        )
        if resp.ok:
            _paw_session_token = resp.headers.get("x-csrf-token", "")
            log.info("PAW session authenticated", extra={"tool": "paw_integration"})
            return _paw_session_token
        log.warning("PAW auth failed: %s %s", resp.status_code, resp.text[:200],
                    extra={"tool": "paw_integration", "error_type": f"http_{resp.status_code}"})
    except Exception as e:
        log.error("PAW auth error: %s", e,
                  extra={"tool": "paw_integration", "error_type": "auth_error"})
    return ""


# ---------------------------------------------------------------------------
#  Tool functions — widget openers
# ---------------------------------------------------------------------------

def paw_open_cube_viewer(
    cube_name: str,
    view_name: str = "",
    private: bool = False,
    toolbar: str = "export,save,swapAxes,suppressZero,refresh,sandbox",
) -> dict[str, Any]:
    """
    Open an interactive PAW cube viewer embedded in the chat.
    Uses the official PAW UI API: /ui?type=cube-viewer&server=...&cube=...

    cube_name: Name of the TM1 cube to open (required)
    view_name: Optional saved view name to apply
    private: If true, open a private view (default false)
    toolbar: Comma-separated toolbar actions: export,save,swapAxes,suppressZero,refresh,sandbox,toggleOverview or 'all'
    """
    if not settings.paw_enabled:
        return {"error": "PAW integration is disabled. Set PAW_ENABLED=true in .env"}

    server = _get_server_name()
    params: dict[str, str] = {
        "type": "cube-viewer",
        "server": server,
        "cube": cube_name,
    }
    if view_name:
        params["view"] = view_name
    if private:
        params["private"] = "true"
    if toolbar:
        params["toolbar"] = toolbar

    url = _paw_ui_url(params)
    title = f"{cube_name}" + (f" — {view_name}" if view_name else "")
    log.info("PAW: Opening cube viewer for %s", title,
             extra={"tool": "paw_open_cube_viewer", "cube": cube_name})
    return _make_iframe_widget(title, url, "cube_viewer",
                               extra_props={"cube": cube_name, "view": view_name})


def paw_open_dimension_editor(
    dimension_name: str,
    hierarchy_name: str = "",
) -> dict[str, Any]:
    """
    Open a PAW dimension editor embedded in the chat.
    Uses the official PAW UI API: /ui?type=dimension-editor&server=...&dimension=...

    dimension_name: Name of the TM1 dimension (required)
    hierarchy_name: Hierarchy within the dimension (defaults to dimension name if empty)
    """
    if not settings.paw_enabled:
        return {"error": "PAW integration is disabled. Set PAW_ENABLED=true in .env"}

    server = _get_server_name()
    params: dict[str, str] = {
        "type": "dimension-editor",
        "server": server,
        "dimension": dimension_name,
    }
    if hierarchy_name:
        params["hierarchy"] = hierarchy_name
    url = _paw_ui_url(params)
    title = f"{dimension_name}" + (f" ({hierarchy_name})" if hierarchy_name else "")
    log.info("PAW: Opening dimension editor for %s", title,
             extra={"tool": "paw_open_dimension_editor", "dimension": dimension_name})
    return _make_iframe_widget(title, url, "dimension_editor",
                               extra_props={"dimension": dimension_name})


def paw_open_set_editor(
    cube_name: str,
    dimension_name: str,
    unique_name: str,
    hierarchy_name: str = "",
    alias: str = "",
    private: bool = False,
) -> dict[str, Any]:
    """
    Open a PAW set (subset) editor embedded in the chat.
    Uses the official PAW UI API: /ui?type=set-editor&server=...&cube=...&dimension=...&uniqueName=...

    cube_name: Name of the TM1 cube (required per API)
    dimension_name: Name of the TM1 dimension (required)
    unique_name: Unique ID of the set or subset (required), e.g. 'All_Entity' or 'FY 2025 Budget'
    hierarchy_name: Optional hierarchy name (defaults to dimension name)
    alias: Optional alias attribute name to display
    private: If true, open a private subset (default false)
    """
    if not settings.paw_enabled:
        return {"error": "PAW integration is disabled. Set PAW_ENABLED=true in .env"}

    server = _get_server_name()
    params: dict[str, str] = {
        "type": "set-editor",
        "server": server,
        "cube": cube_name,
        "dimension": dimension_name,
        "uniqueName": unique_name,
    }
    if hierarchy_name:
        params["hierarchy"] = hierarchy_name
    if alias:
        params["alias"] = alias
    if private:
        params["private"] = "true"
    url = _paw_ui_url(params)
    title = f"Subset: {dimension_name} — {unique_name}"
    log.info("PAW: Opening set editor for %s", title,
             extra={"tool": "paw_open_set_editor", "dimension": dimension_name})
    return _make_iframe_widget(title, url, "set_editor",
                               extra_props={"dimension": dimension_name, "cube": cube_name})


def paw_open_book(
    path: str,
    embed: bool = True,
) -> dict[str, Any]:
    """
    Open a PAW book embedded in the chat.
    Uses the official PAW UI API: /ui?type=book&path=/shared/myBook
    Use paw_list_books() first to discover available books.

    path: Absolute path to the book in PAW (required), e.g. '/shared/myBook'
    embed: If true, hide the top navigation bar for cleaner embedding (default true)
    """
    if not settings.paw_enabled:
        return {"error": "PAW integration is disabled. Set PAW_ENABLED=true in .env"}

    if embed:
        query = urllib.parse.urlencode({
            "perspective": "dashboard",
            "path": path,
            "embed": "true",
        }, quote_via=urllib.parse.quote)
        url = f"/paw/?{query}"
    else:
        params: dict[str, str] = {"type": "book", "path": path}
        url = _paw_ui_url(params)

    book_name = path.split("/")[-1] if "/" in path else path
    log.info("PAW: Opening book %s", book_name,
             extra={"tool": "paw_open_book"})
    return _make_iframe_widget(book_name, url, "book",
                               extra_props={"book": path})


def paw_open_websheet(
    action: str = "Open",
    workbook: str = "",
    tm1_server: str = "",
    admin_host: str = "localhost",
    extra_params: str = "",
) -> dict[str, Any]:
    """
    Open a TM1 Web compatible websheet embedded in the chat.
    Uses the official PAW UI API: /ui?type=websheet&Action=Open&Workbook=...&TM1Server=...

    action: TM1 Web action, usually 'Open' (default)
    workbook: Path to the websheet, e.g. 'Applications/Planning Sample/Management Reporting/Actual v Budget'
    tm1_server: TM1 server name (defaults to auto-detected)
    admin_host: TM1 admin host (default 'localhost')
    extra_params: Additional TM1 Web URL params as 'key1=val1&key2=val2'
    """
    if not settings.paw_enabled:
        return {"error": "PAW integration is disabled. Set PAW_ENABLED=true in .env"}

    server = tm1_server or _get_server_name()
    params: dict[str, str] = {
        "type": "websheet",
        "Action": action,
        "TM1Server": server,
        "AdminHost": admin_host,
    }
    if workbook:
        params["Workbook"] = workbook

    if extra_params:
        for part in extra_params.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                params[k] = v

    url = _paw_ui_url(params)
    title = workbook.split("/")[-1] if "/" in workbook else (workbook or "TM1 Websheet")
    log.info("PAW: Opening websheet %s", title, extra={"tool": "paw_open_websheet"})
    return _make_iframe_widget(title, url, "websheet")


# ---------------------------------------------------------------------------
#  Tool functions — discovery
# ---------------------------------------------------------------------------

def paw_list_books() -> dict[str, Any]:
    """
    List available PAW books/sheets.
    Uses the PAW v0 API to discover all shared books.
    """
    if not settings.paw_enabled:
        return {"error": "PAW integration is disabled. Set PAW_ENABLED=true in .env"}

    try:
        import requests
        token = _paw_authenticate()
        headers: dict[str, str] = {"Accept": "application/json"}
        if token:
            headers["x-csrf-token"] = token

        resp = requests.get(
            f"{_paw_base_url()}/api/v0/Books",
            headers=headers, timeout=10, verify=False,
        )
        if resp.ok:
            books = resp.json()
            if isinstance(books, dict) and "value" in books:
                books = books["value"]
            return {
                "books": [
                    {
                        "id": b.get("ID", b.get("id", "")),
                        "name": b.get("Name", b.get("name", "")),
                        "path": b.get("Path", b.get("path", "")),
                    }
                    for b in (books if isinstance(books, list) else [])
                ],
                "count": len(books) if isinstance(books, list) else 0,
            }
        log.warning("PAW list books failed: %s", resp.status_code,
                    extra={"tool": "paw_list_books", "error_type": f"http_{resp.status_code}"})
        return {"books": [], "count": 0, "note": f"PAW API returned {resp.status_code}"}
    except Exception as e:
        log.error("PAW list books error: %s", e,
                  extra={"tool": "paw_list_books", "error_type": type(e).__name__})
        return {"error": f"Failed to list PAW books: {e}"}


def paw_list_views(cube_name: str) -> dict[str, Any]:
    """
    List saved views for a TM1 cube via REST API.

    cube_name: Name of the TM1 cube
    """
    if not settings.paw_enabled:
        return {"error": "PAW integration is disabled. Set PAW_ENABLED=true in .env"}

    try:
        import requests
        resp = requests.get(
            f"{_tm1_api_base()}/Cubes('{cube_name}')/Views?$select=Name",
            auth=(settings.tm1_user, settings.tm1_password),
            timeout=10, verify=False,
            headers={"Accept": "application/json"},
        )
        if resp.ok:
            views = resp.json().get("value", [])
            return {
                "cube": cube_name,
                "views": [{"name": v.get("Name", "")} for v in views],
                "count": len(views),
            }
        log.warning("PAW list views failed: %s for cube %s", resp.status_code, cube_name,
                    extra={"tool": "paw_list_views", "cube": cube_name,
                           "error_type": f"http_{resp.status_code}"})
        return {"cube": cube_name, "views": [], "count": 0}
    except Exception as e:
        log.error("PAW list views error: %s", e,
                  extra={"tool": "paw_list_views", "error_type": type(e).__name__})
        return {"error": f"Failed to list views for {cube_name}: {e}"}


def paw_list_subsets(dimension_name: str) -> dict[str, Any]:
    """
    List saved subsets for a TM1 dimension via REST API.

    dimension_name: Name of the TM1 dimension
    """
    if not settings.paw_enabled:
        return {"error": "PAW integration is disabled. Set PAW_ENABLED=true in .env"}

    try:
        import requests
        resp = requests.get(
            f"{_tm1_api_base()}/Dimensions('{dimension_name}')/Hierarchies('{dimension_name}')/Subsets?$select=Name,Expression",
            auth=(settings.tm1_user, settings.tm1_password),
            timeout=10, verify=False,
            headers={"Accept": "application/json"},
        )
        if resp.ok:
            subsets = resp.json().get("value", [])
            return {
                "dimension": dimension_name,
                "subsets": [
                    {
                        "name": s.get("Name", ""),
                        "type": "dynamic" if s.get("Expression") else "static",
                    }
                    for s in subsets
                ],
                "count": len(subsets),
            }
        log.warning("PAW list subsets failed: %s for %s", resp.status_code, dimension_name,
                    extra={"tool": "paw_list_subsets", "dimension": dimension_name,
                           "error_type": f"http_{resp.status_code}"})
        return {"dimension": dimension_name, "subsets": [], "count": 0}
    except Exception as e:
        log.error("PAW list subsets error: %s", e,
                  extra={"tool": "paw_list_subsets", "error_type": type(e).__name__})
        return {"error": f"Failed to list subsets for {dimension_name}: {e}"}


def paw_get_current_view() -> dict[str, Any]:
    """
    Get the PAW view currently in use (stored from the user's open widget).
    Use this to see which cube, server, and queryState the user is looking at.
    The agent has a local store that is updated when the user sends a message with a PAW widget active.
    """
    if not settings.paw_enabled:
        return {"error": "PAW integration is disabled. Set PAW_ENABLED=true in .env"}
    try:
        raise ImportError("agent_view_store not yet migrated to Django")
        view = load_agent_current_view()
        if not view:
            return {"message": "No current view stored. Open a PAW cube viewer and send a message so the view is recorded."}
        return {
            "cubeName": view.get("cubeName"),
            "serverName": view.get("serverName"),
            "queryState_length": len(view.get("queryState", "")),
            "updated_at": view.get("updated_at"),
            "note": "Use paw_get_view_mdx to decode queryState and get MDX or structure; use paw_get_view_data to get cell data.",
        }
    except Exception as e:
        log.error("paw_get_current_view error: %s", e, extra={"tool": "paw_get_current_view"})
        return {"error": str(e)}


def paw_get_view_mdx(query_state: str = "") -> dict[str, Any]:
    """
    Decode PAW queryState (base64+gzip JSON) to view structure and extract MDX if present.
    If query_state is omitted, uses the stored current view from the open PAW widget.
    Returns decoded JSON keys and any 'mdx' or 'MDX' field found so you can run it or inspect the view.
    """
    if not settings.paw_enabled:
        return {"error": "PAW integration is disabled. Set PAW_ENABLED=true in .env"}
    try:
        raise ImportError("agent_view_store not yet migrated to Django")
        qs = query_state.strip()
        if not qs:
            view = load_agent_current_view()
            if not view:
                return {"error": "No query_state provided and no current view stored. Open a PAW cube viewer and send a message first."}
            qs = view.get("queryState", "")
        if not qs:
            return {"error": "No queryState to decode."}
        decoded = decode_query_state_to_json(qs)
        keys = list(decoded.keys()) if isinstance(decoded, dict) else []
        mdx = decoded.get("mdx") or decoded.get("MDX") or decoded.get("query") if isinstance(decoded, dict) else None
        if mdx and isinstance(mdx, dict):
            mdx = mdx.get("mdx") or mdx.get("MDX") or mdx.get("query")
        result = {"decoded_keys": keys, "decoded_preview": {k: (str(v)[:200] + "..." if len(str(v)) > 200 else v) for k, v in list(decoded.items())[:10]} if isinstance(decoded, dict) else str(decoded)[:500]}
        if mdx and isinstance(mdx, str):
            result["mdx"] = mdx
        else:
            result["mdx"] = None
            result["note"] = "No MDX string in queryState. Use decoded structure to build MDX or run tm1_rest_execute_mdx_cellset with a custom MDX."
        return result
    except Exception as e:
        log.error("paw_get_view_mdx error: %s", e, extra={"tool": "paw_get_view_mdx"})
        return {"error": str(e)}


def paw_get_view_data(mdx: str = "", top: int = 1000) -> dict[str, Any]:
    """
    Get cell data by executing MDX. If mdx is omitted, uses the current view's queryState
    to try to get MDX (from paw_get_view_mdx) and runs it. Returns axes and cells.
    """
    if not settings.paw_enabled:
        return {"error": "PAW integration is disabled. Set PAW_ENABLED=true in .env"}
    mdx_to_run = mdx.strip()
    if not mdx_to_run:
        try:
            raise ImportError("agent_view_store not yet migrated to Django")
            view = None  # load_agent_current_view()
            if not view:
                return {"error": "No current view and no mdx provided. Open a PAW cube viewer and send a message, or pass mdx=..."}
            decoded = decode_query_state_to_json(view.get("queryState", ""))
            mdx_to_run = (decoded.get("mdx") or decoded.get("MDX") or decoded.get("query")) if isinstance(decoded, dict) else None
            if isinstance(mdx_to_run, dict):
                mdx_to_run = mdx_to_run.get("mdx") or mdx_to_run.get("MDX")
            if not mdx_to_run or not isinstance(mdx_to_run, str):
                return {"error": "Current view queryState does not contain MDX. Use paw_get_view_mdx to see structure, then call tm1_rest_execute_mdx_cellset with a custom MDX.", "decoded_keys": list(decoded.keys()) if isinstance(decoded, dict) else []}
        except Exception as e:
            return {"error": f"Could not get MDX from current view: {e}"}
    try:
        from apps.ai_agent.skills import tm1_rest_api
        return tm1_rest_api.tm1_rest_execute_mdx_cellset(mdx_to_run, top=top)
    except Exception as e:
        log.error("paw_get_view_data error: %s", e, extra={"tool": "paw_get_view_data"})
        return {"error": str(e)}


# ---------------------------------------------------------------------------
#  Tool functions — MDX & URL generation
# ---------------------------------------------------------------------------

def paw_build_mdx(
    cube_name: str,
    rows: str = "",
    columns: str = "",
    where: str = "",
    row_set: str = "",
    column_set: str = "",
    suppress_zeros: bool = False,
) -> dict[str, Any]:
    """
    Build an MDX SELECT statement from structured inputs.

    cube_name: TM1 cube name (required)
    rows: Dimension on rows, e.g. "account"
    columns: Dimension on columns, e.g. "year"
    where: WHERE clause elements, e.g. "[version].[Actual],[entity].[Klikk_Group]"
           or "version:Actual,entity:Klikk_Group" (dim:element shorthand)
    row_set: Override row set expression, e.g. "{[account].[Revenue],[account].[COGS]}"
             or "Revenue,COGS" (element names — wrapped automatically)
    column_set: Override column set expression, e.g. "{[year].[2024],[year].[2025]}"
                or "2024,2025" (element names — wrapped automatically)
    suppress_zeros: Wrap row set in NON EMPTY to suppress zero rows
    """
    if not cube_name:
        return {"error": "cube_name is required"}

    if not rows and not columns:
        return {"error": "At least one of 'rows' or 'columns' must be specified"}

    def _build_set(dim: str, override: str) -> str:
        if not override:
            return f"{{[{dim}].Members}}"
        override = override.strip()
        if override.startswith("{") or override.startswith("TM1") or override.upper().startswith("FILTER") or "(" in override:
            return override
        parts = [e.strip() for e in override.split(",") if e.strip()]
        return "{" + ",".join(f"[{dim}].[{p}]" for p in parts) + "}"

    def _build_where(raw: str) -> str:
        if not raw or not raw.strip():
            return ""
        raw = raw.strip()
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        members = []
        for p in parts:
            if ":" in p and not p.startswith("["):
                dim, elem = p.split(":", 1)
                members.append(f"[{dim.strip()}].[{elem.strip()}]")
            else:
                members.append(p)
        return " WHERE (" + ",".join(members) + ")"

    col_dim = columns or rows
    row_dim = rows if columns else ""
    col_set_expr = _build_set(col_dim, column_set if columns else row_set)
    row_set_expr = _build_set(row_dim, row_set) if row_dim else ""

    if row_dim:
        if suppress_zeros:
            mdx = f"SELECT {col_set_expr} ON 0, NON EMPTY {row_set_expr} ON 1 FROM [{cube_name}]"
        else:
            mdx = f"SELECT {col_set_expr} ON 0, {row_set_expr} ON 1 FROM [{cube_name}]"
    else:
        mdx = f"SELECT {col_set_expr} ON 0 FROM [{cube_name}]"

    mdx += _build_where(where)

    return {"mdx": mdx, "cube": cube_name}


def paw_generate_view_url(
    cube_name: str,
    view_name: str = "",
    server_name: str = "",
) -> dict[str, Any]:
    """
    Generate a PAW cube viewer URL from a cube name.
    Only the cube name is required — PAW renders the rest.

    cube_name: TM1 cube name (required)
    view_name: Saved TM1 view name (optional — opens default view if empty)
    server_name: TM1 server name (auto-detected if empty)
    """
    if not settings.paw_enabled:
        return {"error": "PAW integration is disabled. Set PAW_ENABLED=true in .env"}
    if not cube_name:
        return {"error": "cube_name is required"}

    server = server_name or _get_server_name()
    params: dict[str, str] = {
        "type": "cube-viewer",
        "server": server,
        "cube": cube_name,
    }
    if view_name:
        params["view"] = view_name

    url = _paw_ui_url(params)
    return {
        "url": url,
        "cube": cube_name,
        "view": view_name or "(default)",
        "server": server,
    }


def paw_status() -> dict[str, Any]:
    """
    Check PAW connectivity and return status info.
    Tests both the PAW UI and the TM1 REST API.
    """
    result: dict[str, Any] = {
        "paw_enabled": settings.paw_enabled,
        "paw_url": _paw_base_url(),
        "tm1_url": _tm1_api_base(),
    }

    if not settings.paw_enabled:
        result["status"] = "disabled"
        return result

    try:
        import requests
        resp = requests.get(f"{_paw_base_url()}/", timeout=5, verify=False)
        result["paw_reachable"] = resp.status_code < 500
        result["paw_status_code"] = resp.status_code
    except Exception as e:
        result["paw_reachable"] = False
        result["paw_error"] = str(e)

    try:
        import requests
        resp = requests.get(
            f"{_tm1_api_base()}/Configuration/ProductVersion",
            auth=(settings.tm1_user, settings.tm1_password),
            timeout=5, verify=False,
        )
        if resp.ok:
            result["tm1_reachable"] = True
            result["tm1_version"] = resp.json().get("value", "unknown")
            result["tm1_server_name"] = _get_server_name()
        else:
            result["tm1_reachable"] = False
    except Exception as e:
        result["tm1_reachable"] = False
        result["tm1_error"] = str(e)

    result["status"] = "ok" if result.get("paw_reachable") and result.get("tm1_reachable") else "degraded"
    return result


# ---------------------------------------------------------------------------
#  Tool schemas (loaded by tool_registry.py)
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: list[dict] = []
TOOL_FUNCTIONS: dict[str, Any] = {}

if settings.paw_enabled:
    TOOL_SCHEMAS = [
        {
            "name": "paw_open_cube_viewer",
            "description": (
                "Open an interactive PAW cube viewer embedded in the chat. "
                "Uses the official IBM PAW UI API (/ui?type=cube-viewer). "
                "Supports writeback, sandboxes, export, and all PAW pivot features. "
                "Parameters: server (auto), cube (required), view (optional), "
                "private (optional), toolbar (optional: export,save,swapAxes,suppressZero,refresh,sandbox,toggleOverview or 'all')."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "cube_name": {"type": "string", "description": "TM1 cube name (required), e.g. 'gl_src_trial_balance'"},
                    "view_name": {"type": "string", "description": "Saved view name to open. If omitted, opens the default view."},
                    "private": {"type": "boolean", "description": "If true, open a private view (default false)"},
                    "toolbar": {
                        "type": "string",
                        "description": "Comma-separated toolbar actions: export,save,swapAxes,suppressZero,refresh,sandbox,toggleOverview. Use 'all' for all actions.",
                    },
                },
                "required": ["cube_name"],
            },
        },
        {
            "name": "paw_open_dimension_editor",
            "description": (
                "Open a PAW dimension editor embedded in the chat. "
                "Uses the official IBM PAW UI API (/ui?type=dimension-editor). "
                "Shows the full dimension hierarchy for browsing and editing elements. "
                "Parameters: server (auto), dimension (required), hierarchy (optional — defaults to dimension name)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "dimension_name": {"type": "string", "description": "TM1 dimension name (required), e.g. 'account'"},
                    "hierarchy_name": {"type": "string", "description": "Hierarchy name (defaults to dimension name if omitted)"},
                },
                "required": ["dimension_name"],
            },
        },
        {
            "name": "paw_open_set_editor",
            "description": (
                "Open a PAW set (subset) editor embedded in the chat. "
                "Uses the official IBM PAW UI API (/ui?type=set-editor). "
                "For creating or editing element subsets. "
                "Parameters: server (auto), cube (required), dimension (required), uniqueName (required), "
                "hierarchy (optional), alias (optional), private (optional)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "cube_name": {"type": "string", "description": "TM1 cube name (required per API)"},
                    "dimension_name": {"type": "string", "description": "TM1 dimension name (required)"},
                    "unique_name": {"type": "string", "description": "Unique ID of the set or subset (required), e.g. 'All_Entity', 'FY 2025 Budget'"},
                    "hierarchy_name": {"type": "string", "description": "Hierarchy name (defaults to dimension name)"},
                    "alias": {"type": "string", "description": "Alias attribute name to display"},
                    "private": {"type": "boolean", "description": "Open a private subset (default false)"},
                },
                "required": ["cube_name", "dimension_name", "unique_name"],
            },
        },
        {
            "name": "paw_open_book",
            "description": (
                "Open a PAW book embedded in the chat. "
                "Uses the official IBM PAW UI API (/ui?type=book or /?perspective=dashboard for embedding). "
                "Books are saved workbook layouts with cube views, charts, and dashboards. "
                "Use paw_list_books() first to discover available books."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to the book (required), e.g. '/shared/myBook'"},
                    "embed": {"type": "boolean", "description": "Hide top navigation bar (default true)"},
                },
                "required": ["path"],
            },
        },
        {
            "name": "paw_open_websheet",
            "description": (
                "Open a TM1 Web compatible websheet embedded in the chat. "
                "Uses the official IBM PAW UI API (/ui?type=websheet). "
                "Passes through TM1 Web URL API parameters."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "workbook": {
                        "type": "string",
                        "description": "Path to the websheet, e.g. 'Applications/Planning Sample/Management Reporting/Actual v Budget'",
                    },
                    "action": {"type": "string", "description": "TM1 Web action (default 'Open')"},
                    "tm1_server": {"type": "string", "description": "TM1 server name (defaults to auto-detected)"},
                    "extra_params": {"type": "string", "description": "Additional TM1 Web URL params as 'key1=val1&key2=val2'"},
                },
                "required": ["workbook"],
            },
        },
        {
            "name": "paw_list_books",
            "description": "List all available PAW books/sheets. Use to discover what books exist before opening one.",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "paw_list_views",
            "description": "List saved views for a TM1 cube via PAW/REST API. Use before paw_open_cube_viewer to find view names.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "cube_name": {"type": "string", "description": "TM1 cube name"},
                },
                "required": ["cube_name"],
            },
        },
        {
            "name": "paw_list_subsets",
            "description": "List saved subsets for a TM1 dimension via REST API. Use before paw_open_set_editor to find subset names.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "dimension_name": {"type": "string", "description": "TM1 dimension name"},
                },
                "required": ["dimension_name"],
            },
        },
        {
            "name": "paw_status",
            "description": "Check PAW and TM1 connectivity status, version, and server name.",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "paw_get_current_view",
            "description": (
                "Get the PAW view currently in use (the view stored from the user's open widget). "
                "The agent has a local store updated when the user sends a message with a PAW widget active. "
                "Returns cubeName, serverName, queryState length, updated_at. Use paw_get_view_mdx to decode and get MDX; paw_get_view_data to get cell data."
            ),
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "paw_get_view_mdx",
            "description": (
                "Decode PAW queryState (base64+gzip JSON) to view structure and extract MDX if present. "
                "If query_state is omitted, uses the stored current view. Returns decoded keys, preview, and mdx string if found. "
                "Use to get view MDX by query state or inspect the view the user is looking at."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query_state": {"type": "string", "description": "Optional. Base64 queryState from PAW. If omitted, uses stored current view."},
                },
            },
        },
        {
            "name": "paw_get_view_data",
            "description": (
                "Get cell data by executing MDX. If mdx is omitted, uses the current view's queryState to get MDX and runs it. "
                "Returns axes and cells (cube data). Use after paw_get_view_mdx if you need to extract values from the view."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "mdx": {"type": "string", "description": "Optional. MDX SELECT statement. If omitted, uses MDX from current view queryState if available."},
                    "top": {"type": "integer", "description": "Max cells to return (default 1000)."},
                },
            },
        },
        {
            "name": "paw_build_mdx",
            "description": (
                "Build an MDX SELECT statement from structured inputs without executing it. "
                "Specify a cube, row and column dimensions, optional element subsets, and a WHERE clause. "
                "Supports shorthand: 'dim:element' for WHERE, comma-separated element names for sets. "
                "Returns the generated MDX string. Pair with paw_generate_view_url to get a clickable PAW link, "
                "or with tm1_execute_mdx_rows to run the query."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "cube_name": {"type": "string", "description": "TM1 cube name (required)"},
                    "rows": {"type": "string", "description": "Dimension on rows, e.g. 'account'"},
                    "columns": {"type": "string", "description": "Dimension on columns, e.g. 'year'"},
                    "where": {
                        "type": "string",
                        "description": (
                            "WHERE clause elements. Accepts TM1 member refs like '[version].[Actual],[entity].[Klikk_Group]' "
                            "or shorthand 'version:Actual,entity:Klikk_Group'"
                        ),
                    },
                    "row_set": {
                        "type": "string",
                        "description": "Row elements — comma-separated names (e.g. 'Revenue,COGS') or full MDX set expression",
                    },
                    "column_set": {
                        "type": "string",
                        "description": "Column elements — comma-separated names (e.g. '2024,2025') or full MDX set expression",
                    },
                    "suppress_zeros": {"type": "boolean", "description": "Wrap rows in NON EMPTY to suppress zero rows (default false)"},
                },
                "required": ["cube_name"],
            },
        },
        {
            "name": "paw_generate_view_url",
            "description": (
                "Generate a PAW cube viewer URL from a cube name. "
                "Only the cube name is needed — PAW renders the view. "
                "Use after a writeback or TI process to give the user a direct link to the cube. "
                "Optionally pass a saved view name to open a specific view."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "cube_name": {"type": "string", "description": "TM1 cube name (required)"},
                    "view_name": {"type": "string", "description": "Saved TM1 view name (optional — opens default if empty)"},
                    "server_name": {"type": "string", "description": "TM1 server name (auto-detected if empty)"},
                },
                "required": ["cube_name"],
            },
        },
    ]

    TOOL_FUNCTIONS = {
        "paw_open_cube_viewer": paw_open_cube_viewer,
        "paw_open_dimension_editor": paw_open_dimension_editor,
        "paw_open_set_editor": paw_open_set_editor,
        "paw_open_book": paw_open_book,
        "paw_open_websheet": paw_open_websheet,
        "paw_list_books": paw_list_books,
        "paw_list_views": paw_list_views,
        "paw_list_subsets": paw_list_subsets,
        "paw_status": paw_status,
        "paw_get_current_view": paw_get_current_view,
        "paw_get_view_mdx": paw_get_view_mdx,
        "paw_get_view_data": paw_get_view_data,
        "paw_build_mdx": paw_build_mdx,
        "paw_generate_view_url": paw_generate_view_url,
    }
