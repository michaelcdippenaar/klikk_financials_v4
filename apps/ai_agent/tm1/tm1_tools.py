"""
TM1 tool implementations for the MCP server.

Wraps TM1py calls with a persistent connection (singleton with reconnect)
and exposes them as plain functions that server.py registers as MCP tools.
"""
from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from TM1py import TM1Service
from TM1py.Exceptions import TM1pyException

from apps.ai_agent.agent.config import TM1_CONFIG

log = logging.getLogger("mcp_tm1")

# ---------------------------------------------------------------------------
#  Persistent TM1 connection
# ---------------------------------------------------------------------------

_tm1_lock = threading.Lock()
_tm1: TM1Service | None = None


def _get_tm1() -> TM1Service:
    """Return a persistent TM1Service, reconnecting if needed."""
    global _tm1
    with _tm1_lock:
        if _tm1 is None:
            log.info("Connecting to TM1 at %s:%s", TM1_CONFIG["address"], TM1_CONFIG["port"])
            _tm1 = TM1Service(**TM1_CONFIG)
        else:
            # Quick health check
            try:
                _tm1.server.get_server_name()
            except Exception:
                log.warning("TM1 connection lost, reconnecting...")
                try:
                    _tm1.logout()
                except Exception:
                    pass
                _tm1 = TM1Service(**TM1_CONFIG)
        return _tm1


def _safe(func):
    """Decorator: catch TM1py errors and return {error: ...} dict."""
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except TM1pyException as e:
            return {"error": f"TM1 error: {e}"}
        except Exception as e:
            return {"error": str(e)}
    wrapper.__name__ = func.__name__
    wrapper.__doc__ = func.__doc__
    return wrapper


# ---------------------------------------------------------------------------
#  Metadata tools
# ---------------------------------------------------------------------------

@_safe
def tm1_list_cubes() -> dict[str, Any]:
    """List all cubes on the TM1 server with their dimensions."""
    tm1 = _get_tm1()
    cubes = tm1.cubes.get_all()
    result = []
    for c in cubes:
        if c.name.startswith("}"):
            continue  # skip control cubes
        dims = c.dimensions
        # dimensions may be strings or objects depending on TM1py version
        dim_names = [d.name if hasattr(d, "name") else str(d) for d in dims] if dims else []
        result.append({"name": c.name, "dimensions": dim_names})
    return {"cubes": result, "count": len(result)}


@_safe
def tm1_list_dimensions() -> dict[str, Any]:
    """List all dimensions on the TM1 server."""
    tm1 = _get_tm1()
    dims = tm1.dimensions.get_all_names()
    visible = [d for d in dims if not d.startswith("}")]
    return {"dimensions": visible, "count": len(visible)}


@_safe
def tm1_get_dimension_elements(
    dimension_name: str,
    hierarchy_name: str = "",
    element_type: str = "",
    parent: str = "",
) -> dict[str, Any]:
    """
    Get elements of a TM1 dimension. Optionally filter by type or parent.
    element_type: 'Numeric', 'String', 'Consolidated' or '' for all.
    parent: if provided, returns only children of that consolidated element.
    """
    tm1 = _get_tm1()
    hier = hierarchy_name or dimension_name
    if parent:
        children = tm1.elements.get_leaf_element_names(dimension_name, hier, parent)
        return {"dimension": dimension_name, "parent": parent, "elements": list(children), "count": len(children)}

    elements = tm1.elements.get_elements(dimension_name, hier)
    result = []
    for el in elements:
        raw = el.element_type
        if hasattr(raw, "name") and isinstance(raw.name, str):
            el_type = raw.name.capitalize()
        elif hasattr(raw, "value"):
            el_type = str(raw.value)
        else:
            el_type = str(raw)
        if element_type and element_type.lower() != "all" and el_type.lower() != element_type.lower():
            continue
        result.append({"name": el.name, "type": el_type})
    return {"dimension": dimension_name, "elements": result, "count": len(result)}


@_safe
def tm1_get_element_attributes(
    dimension_name: str,
    element_name: str,
    hierarchy_name: str = "",
) -> dict[str, Any]:
    """Get all attribute values for a specific element."""
    tm1 = _get_tm1()
    hier = hierarchy_name or dimension_name
    attr_defs = tm1.elements.get_element_attributes(dimension_name, hier)

    def _fetch_attr(attr_name):
        try:
            bulk = tm1.elements.get_attribute_of_elements(dimension_name, hier, attr_name)
            val = bulk.get(element_name)
            if val is not None and str(val).strip():
                return attr_name, val
        except Exception:
            pass
        return attr_name, None

    attrs = {}
    with ThreadPoolExecutor(max_workers=min(len(attr_defs), 8)) as pool:
        for attr_name, val in pool.map(lambda a: _fetch_attr(a.name), attr_defs):
            if val is not None:
                attrs[attr_name] = val
    return {"dimension": dimension_name, "element": element_name, "attributes": attrs}


@_safe
def tm1_get_element_attributes_bulk(
    dimension_name: str,
    elements: list[str],
    attributes: list[str] | None = None,
    hierarchy_name: str = "",
) -> dict[str, Any]:
    """Get attributes for multiple elements at once (batch)."""
    tm1 = _get_tm1()
    hier = hierarchy_name or dimension_name
    if not attributes:
        attr_defs = tm1.elements.get_element_attributes(dimension_name, hier)
        attributes = [a.name for a in attr_defs]

    # Bulk-fetch each attribute in parallel (one API call per attribute)
    def _fetch_attr(attr):
        try:
            return attr, tm1.elements.get_attribute_of_elements(dimension_name, hier, attr)
        except Exception:
            return attr, {}

    attr_data: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=min(len(attributes), 8)) as pool:
        for attr, data in pool.map(_fetch_attr, attributes):
            attr_data[attr] = data

    result = {}
    for el_name in elements[:100]:
        attrs = {}
        for attr in attributes:
            val = attr_data.get(attr, {}).get(el_name)
            if val is not None and str(val).strip():
                attrs[attr] = val
        result[el_name] = attrs
    return {"dimension": dimension_name, "elements": result, "count": len(result)}


@_safe
def tm1_export_dimension_attributes(
    dimension_name: str,
    hierarchy_name: str = "",
    element_type: str = "",
    attributes: list[str] | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """
    Export all elements of a dimension with their aliases and attributes in one call.
    Returns a flat table: each row is {element, type, attr1, attr2, ...}.
    Use this to get a full picture of a dimension's element metadata.

    dimension_name: e.g. 'account', 'entity', 'listed_share'
    element_type: filter to 'Numeric', 'String', 'Consolidated', or '' for all
    attributes: list of attribute names to include (default: all)
    limit: max elements to return (default 500)
    """
    tm1 = _get_tm1()
    hier = hierarchy_name or dimension_name

    # Get attribute definitions
    attr_defs = tm1.elements.get_element_attributes(dimension_name, hier)
    all_attr_names = [a.name for a in attr_defs]
    attr_names = attributes if attributes else all_attr_names

    # Bulk-fetch each attribute in parallel (one API call per attribute)
    def _fetch_attr(attr):
        try:
            return attr, tm1.elements.get_attribute_of_elements(dimension_name, hier, attr)
        except Exception:
            return attr, {}

    attr_data: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(len(attr_names), 8)) as pool:
        for attr, data in pool.map(_fetch_attr, attr_names):
            attr_data[attr] = data

    # Get elements with types
    elements = tm1.elements.get_elements(dimension_name, hier)

    # Map element type ints to strings
    _TYPE_MAP = {1: "Numeric", 2: "String", 3: "Consolidated"}

    rows = []
    for el in elements:
        raw_type = el.element_type.value if hasattr(el.element_type, "value") else el.element_type
        el_type = _TYPE_MAP.get(raw_type, str(raw_type)) if isinstance(raw_type, int) else str(raw_type)

        if element_type and el_type.lower() != element_type.lower():
            continue

        row: dict[str, Any] = {"element": el.name, "type": el_type}
        for attr in attr_names:
            val = attr_data.get(attr, {}).get(el.name)
            row[attr] = val if val is not None and str(val).strip() else None
        rows.append(row)

        if len(rows) >= limit:
            break

    return {
        "dimension": dimension_name,
        "attributes": attr_names,
        "all_available_attributes": all_attr_names,
        "elements": rows,
        "count": len(rows),
        "truncated": len(rows) >= limit,
    }


@_safe
def tm1_get_hierarchy(
    dimension_name: str,
    hierarchy_name: str = "",
    max_depth: int = 3,
) -> dict[str, Any]:
    """Get hierarchy structure showing parent-child relationships."""
    tm1 = _get_tm1()
    hier = hierarchy_name or dimension_name
    hierarchy = tm1.hierarchies.get(dimension_name, hier)

    def _build_tree(el_name: str, depth: int) -> dict:
        node = {"name": el_name}
        if depth < max_depth:
            el = hierarchy.elements.get(el_name)
            if el and el.components:
                node["children"] = [_build_tree(c, depth + 1) for c in list(el.components.keys())[:50]]
        return node

    # Find root elements (consolidated with no parents)
    all_children = set()
    for el in hierarchy.elements.values():
        for c in el.components:
            all_children.add(c)

    roots = [
        el.name for el in hierarchy.elements.values()
        if el.element_type.value == "Consolidated" and el.name not in all_children
    ]

    tree = [_build_tree(r, 0) for r in roots[:20]]
    return {"dimension": dimension_name, "hierarchy": hier, "roots": tree}


@_safe
def tm1_find_element(
    search_term: str,
    dimension_name: str = "",
    search_aliases: bool = True,
) -> dict[str, Any]:
    """
    Search for elements by substring across element names AND alias/attribute values.
    Case-insensitive. Finds elements like "Stellenbosch Municipality" even when the
    element name is a GUID, by searching through alias attributes (name, code, etc.).

    search_term: text to search for (e.g. 'Stellenbosch', 'Absa', 'rent')
    dimension_name: limit search to this dimension (default: search all)
    search_aliases: also search alias/attribute values, not just element names (default true)
    """
    tm1 = _get_tm1()
    search_lower = search_term.lower()

    if dimension_name:
        dims = [dimension_name]
    else:
        dims = [d for d in tm1.dimensions.get_all_names() if not d.startswith("}")]

    def _search_dimension(dim):
        """Search a single dimension for matching elements (name + attributes)."""
        dim_matches = []
        seen = set()
        try:
            elements = tm1.elements.get_element_names(dim, dim)
            for el in elements:
                if search_lower in el.lower() and el not in seen:
                    seen.add(el)
                    dim_matches.append({"dimension": dim, "element": el, "matched_on": "element_name"})

            if search_aliases:
                try:
                    attr_defs = tm1.elements.get_element_attributes(dim, dim)
                except Exception:
                    return dim_matches

                # Fetch all attributes in parallel within this dimension
                def _fetch_attr(attr_name):
                    try:
                        return attr_name, tm1.elements.get_attribute_of_elements(dim, dim, attr_name)
                    except Exception:
                        return attr_name, {}

                with ThreadPoolExecutor(max_workers=min(len(attr_defs), 6)) as attr_pool:
                    for attr_name, attr_values in attr_pool.map(
                        lambda a: _fetch_attr(a.name), attr_defs
                    ):
                        for el_name, attr_val in attr_values.items():
                            if attr_val and search_lower in str(attr_val).lower():
                                if el_name not in seen:
                                    seen.add(el_name)
                                    dim_matches.append({
                                        "dimension": dim,
                                        "element": el_name,
                                        "matched_on": f"attribute:{attr_name}",
                                        "matched_value": str(attr_val),
                                    })
        except Exception:
            pass
        return dim_matches

    # Search all dimensions in parallel
    all_matches = []
    with ThreadPoolExecutor(max_workers=min(len(dims), 10)) as pool:
        for dim_matches in pool.map(_search_dimension, dims):
            all_matches.extend(dim_matches)
            if len(all_matches) >= 50:
                return {"search_term": search_term, "matches": all_matches[:50], "truncated": True}

    return {"search_term": search_term, "matches": all_matches, "count": len(all_matches)}


@_safe
def tm1_validate_elements(
    dimension_name: str,
    element_names: list[str],
    hierarchy_name: str = "",
) -> dict[str, Any]:
    """Check which element names exist in a dimension and which don't."""
    tm1 = _get_tm1()
    hier = hierarchy_name or dimension_name
    existing = set(tm1.elements.get_element_names(dimension_name, hier))
    valid = [e for e in element_names if e in existing]
    invalid = [e for e in element_names if e not in existing]
    return {"dimension": dimension_name, "valid": valid, "invalid": invalid}


@_safe
def tm1_list_processes() -> dict[str, Any]:
    """List all TI processes on the server."""
    tm1 = _get_tm1()
    procs = tm1.processes.get_all_names()
    visible = [p for p in procs if not p.startswith("}")]
    return {"processes": visible, "count": len(visible)}


@_safe
def tm1_get_process_code(process_name: str) -> dict[str, Any]:
    """Get the code (Prolog, Metadata, Data, Epilog) of a TI process."""
    tm1 = _get_tm1()
    proc = tm1.processes.get(process_name)
    return {
        "name": process_name,
        "prolog": proc.prolog_procedure,
        "metadata": proc.metadata_procedure,
        "data": proc.data_procedure,
        "epilog": proc.epilog_procedure,
        "parameters": [
            {"name": p["Name"], "value": p.get("Value", ""), "prompt": p.get("Prompt", "")}
            for p in proc.parameters
        ] if proc.parameters else [],
    }


@_safe
def tm1_get_cube_rules(cube_name: str) -> dict[str, Any]:
    """Get the rules of a cube."""
    tm1 = _get_tm1()
    cube = tm1.cubes.get(cube_name)
    return {"cube": cube_name, "rules": cube.rules.text if cube.rules else "(no rules)"}


@_safe
def tm1_list_views(cube_name: str) -> dict[str, Any]:
    """List all saved views for a cube."""
    tm1 = _get_tm1()
    views = tm1.views.get_all_names(cube_name)
    return {"cube": cube_name, "views": views, "count": len(views)}


# ---------------------------------------------------------------------------
#  Query tools
# ---------------------------------------------------------------------------

@_safe
def tm1_query_mdx(mdx: str, top_records: int = 100) -> dict[str, Any]:
    """
    Execute an MDX query and return results.
    Returns cell values with their element coordinates.
    """
    tm1 = _get_tm1()
    cellset = tm1.cells.execute_mdx(mdx, top=top_records)
    rows = []
    for key, cell in cellset.items():
        rows.append({
            "coordinates": list(key),
            "value": cell["Value"],
        })
    return {"mdx": mdx, "rows": rows, "count": len(rows)}


@_safe
def tm1_execute_mdx_rows(mdx: str) -> dict[str, Any]:
    """
    Execute MDX and return as flat row-based records (dataframe style).
    Good for tabular output.
    """
    tm1 = _get_tm1()
    df = tm1.cells.execute_mdx_dataframe(mdx)
    records = df.to_dict(orient="records")
    if len(records) > 500:
        records = records[:500]
        truncated = True
    else:
        truncated = False
    return {"rows": records, "count": len(records), "truncated": truncated}


@_safe
def tm1_read_view(cube_name: str, view_name: str, private: bool = False) -> dict[str, Any]:
    """Read data from a saved TM1 view."""
    tm1 = _get_tm1()
    cellset = tm1.cells.execute_view(cube_name, view_name, private=private, top=200)
    rows = []
    for key, cell in cellset.items():
        rows.append({"coordinates": list(key), "value": cell["Value"]})
    return {"cube": cube_name, "view": view_name, "rows": rows, "count": len(rows)}


@_safe
def tm1_get_cell_value(
    cube_name: str,
    elements: list[str],
) -> dict[str, Any]:
    """Get a single cell value from a cube given element coordinates."""
    tm1 = _get_tm1()
    value = tm1.cells.get_value(cube_name, ",".join(elements))
    return {"cube": cube_name, "elements": elements, "value": value}


# ---------------------------------------------------------------------------
#  Management tools (write operations)
# ---------------------------------------------------------------------------

@_safe
def tm1_run_process(
    process_name: str,
    parameters: dict[str, Any] | None = None,
    confirm: bool = True,
) -> dict[str, Any]:
    """
    Execute a TI process. Set confirm=True (default) for safety.
    parameters: dict of parameter name -> value.
    """
    if not confirm:
        return {"error": "Set confirm=True to execute this process."}
    tm1 = _get_tm1()
    params = parameters or {}
    success, status, error_log = tm1.processes.execute(process_name, **params)
    return {
        "process": process_name,
        "success": success,
        "status": status,
        "error_log": error_log[:2000] if error_log else None,
    }


@_safe
def tm1_write_cell(
    cube_name: str,
    elements: list[str],
    value: Any,
    confirm: bool = True,
) -> dict[str, Any]:
    """Write a value to a single cell. Set confirm=True (default) for safety."""
    if not confirm:
        return {"error": "Set confirm=True to write this cell."}
    tm1 = _get_tm1()
    tm1.cells.write_value(value, cube_name, tuple(elements))
    return {"cube": cube_name, "elements": elements, "value": value, "status": "written"}


@_safe
def tm1_get_server_info() -> dict[str, Any]:
    """Get TM1 server info (name, version, uptime, etc.)."""
    tm1 = _get_tm1()
    config = tm1.server.get_server_name()
    active_users = tm1.monitoring.get_active_users()
    return {
        "server_name": config,
        "active_users": [u.name for u in active_users] if active_users else [],
    }


def tm1_get_schema_timestamps() -> dict[str, str]:
    """Cheap poll: return {cube_name: LastSchemaUpdate} for all cubes.

    Uses the TM1 REST API directly to fetch only Name and LastSchemaUpdate
    for every cube in a single request (~10-50ms).  Used by the metadata
    cache to detect when dimensions/rules/elements have changed.
    """
    try:
        tm1 = _get_tm1()
        response = tm1._tm1_rest.GET("/Cubes?$select=Name,LastSchemaUpdate")
        return {
            c["Name"]: c.get("LastSchemaUpdate", "")
            for c in response.json()["value"]
        }
    except Exception as exc:
        log.warning("Schema timestamp poll failed: %s", exc)
        return {}
