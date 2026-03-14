"""
MCP Bridge — single source of truth for TM1 + PostgreSQL tools.

Imports canonical tool implementations from mcp_tm1_server/ (persistent TM1
connection, tested PG queries) and exposes them in the backend's
TOOL_SCHEMAS / TOOL_FUNCTIONS format.

Tools that only exist in the backend (enhanced find_element, validate_elements,
update_element_attribute, read_view_as_table) use the MCP server's persistent
TM1 connection via _get_tm1().

Parameter adapters handle name differences between backend schemas (what the AI
uses) and MCP function signatures.
"""
from __future__ import annotations

import logging
import os
import pickle
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

# ---------------------------------------------------------------------------
#  Import TM1 tools from apps.ai_agent.tm1
# ---------------------------------------------------------------------------
from apps.ai_agent.tm1 import tm1_tools as _tm1
from apps.ai_agent.tm1 import pg_tools as _pg
from apps.ai_agent.tm1 import report_builder as _rb
from apps.ai_agent.tm1 import sql_builder as _sql

log = logging.getLogger('ai_agent')

# Re-export the persistent TM1 connection helper for enhanced tools
_get_tm1 = _tm1._get_tm1


# ---------------------------------------------------------------------------
#  In-memory TM1 metadata cache (schema-poll invalidation)
# ---------------------------------------------------------------------------

_POLL_INTERVAL = 60  # seconds between schema timestamp polls
_CACHE_SNAPSHOT_PATH = os.path.join(
    os.path.dirname(__file__), ".tm1_cache_snapshot.pkl"
)
_SNAPSHOT_SAVE_INTERVAL = 120  # save snapshot at most every 2 minutes


class _TM1Cache:
    """Thread-safe in-memory cache invalidated by TM1 LastSchemaUpdate timestamps.

    Instead of per-key TTLs, a single cheap REST call polls cube schema
    timestamps.  If any timestamp changed since the last poll the entire
    cache is cleared, guaranteeing freshness with minimal overhead.

    The cache is persisted to disk so it survives process restarts.
    On init the snapshot is loaded; if the TM1 schema has since changed
    the snapshot is discarded.
    """

    def __init__(self) -> None:
        self._store: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._last_schema: dict[str, str] = {}
        self._last_poll: float = 0.0
        self._hits = 0
        self._misses = 0
        self._last_save: float = 0.0
        self._load_snapshot()

    # -- snapshot persistence -----------------------------------------------

    def _load_snapshot(self) -> None:
        """Load persisted cache from disk on startup."""
        try:
            if not os.path.exists(_CACHE_SNAPSHOT_PATH):
                return
            with open(_CACHE_SNAPSHOT_PATH, "rb") as f:
                snap = pickle.load(f)
            self._store = snap.get("store", {})
            self._last_schema = snap.get("schema", {})
            count = len(self._store)
            log.info("TM1 cache: loaded %d keys from snapshot", count)
        except Exception as e:
            log.warning("TM1 cache: could not load snapshot: %s", e)
            self._store = {}

    def _save_snapshot(self) -> None:
        """Persist current cache to disk (called after writes, debounced)."""
        now = _time.monotonic()
        if now - self._last_save < _SNAPSHOT_SAVE_INTERVAL:
            return
        self._last_save = now
        try:
            snap = {"store": dict(self._store), "schema": dict(self._last_schema)}
            tmp = _CACHE_SNAPSHOT_PATH + ".tmp"
            with open(tmp, "wb") as f:
                pickle.dump(snap, f, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, _CACHE_SNAPSHOT_PATH)
            log.debug("TM1 cache: snapshot saved (%d keys)", len(self._store))
        except Exception as e:
            log.warning("TM1 cache: could not save snapshot: %s", e)

    # -- read / write -------------------------------------------------------

    def get(self, key: str) -> Any | None:
        with self._lock:
            if key in self._store:
                self._hits += 1
                return self._store[key]
            self._misses += 1
        return None

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._store[key] = value
            self._save_snapshot()

    def invalidate(self, prefix: str = "") -> int:
        """Drop cache entries.  Empty prefix clears everything."""
        with self._lock:
            if not prefix:
                count = len(self._store)
                self._store.clear()
                # Remove snapshot so it doesn't reload stale data
                try:
                    if os.path.exists(_CACHE_SNAPSHOT_PATH):
                        os.remove(_CACHE_SNAPSHOT_PATH)
                except Exception:
                    pass
                return count
            keys = [k for k in self._store if k.startswith(prefix)]
            for k in keys:
                del self._store[k]
            return len(keys)

    # -- schema-poll invalidation -------------------------------------------

    def poll_schema(self) -> bool:
        """Poll TM1 schema timestamps; returns True if cache was invalidated.

        At most once per _POLL_INTERVAL seconds.  A single HTTP GET returns
        LastSchemaUpdate for every cube — if any changed, nuke the cache.
        """
        now = _time.monotonic()
        if now - self._last_poll < _POLL_INTERVAL:
            return False

        new_ts = _tm1.tm1_get_schema_timestamps()
        if not new_ts:
            self._last_poll = now
            return False

        changed = new_ts != self._last_schema
        if changed:
            count = self.invalidate()
            log.info("TM1 schema change detected — cache cleared (%d keys)", count)
            self._last_schema = new_ts
        else:
            # Schema unchanged — persist schema into snapshot so next restart
            # knows what schema was current when these entries were cached.
            with self._lock:
                self._last_schema = new_ts
        self._last_poll = now
        return changed

    # -- introspection ------------------------------------------------------

    def stats(self) -> dict:
        with self._lock:
            return {
                "cached_keys": len(self._store),
                "hits": self._hits,
                "misses": self._misses,
                "last_poll_ago_s": round(_time.monotonic() - self._last_poll)
                if self._last_poll
                else None,
            }


_tm1_cache = _TM1Cache()


def _cache_key(func_name: str, *args: Any) -> str:
    return f"{func_name}:{':'.join(str(a) for a in args)}"


def tm1_cache_stats() -> dict[str, Any]:
    """Return current TM1 metadata cache statistics."""
    return _tm1_cache.stats()


def tm1_cache_clear() -> dict[str, Any]:
    """Manually clear the TM1 metadata cache."""
    count = _tm1_cache.invalidate()
    return {"status": "cleared", "keys_removed": count}


# ---------------------------------------------------------------------------
#  Element lookup pre-warming
# ---------------------------------------------------------------------------

_PREWARM_DIMS = ["entity", "account", "listed_share", "month", "version"]
_prewarm_done = False
_prewarm_lock = threading.Lock()


def _prewarm_element_lookups() -> None:
    """Pre-cache element exports for key dimensions (lazy, first call only)."""
    global _prewarm_done
    if _prewarm_done:
        return
    with _prewarm_lock:
        if _prewarm_done:
            return
        for dim in _PREWARM_DIMS:
            key = _cache_key("tm1_export_dimension_attributes", dim, dim, "", 500)
            if _tm1_cache.get(key) is not None:
                continue
            try:
                result = _tm1.tm1_export_dimension_attributes(
                    dimension_name=dim, hierarchy_name=dim,
                )
                if isinstance(result, dict) and "error" not in result:
                    _tm1_cache.set(key, result)
                    log.info("Pre-warmed element lookup: %s (%d elements)",
                             dim, result.get("count", 0))
            except Exception:
                log.debug("Pre-warm failed for %s", dim, exc_info=True)
        _prewarm_done = True


# ---------------------------------------------------------------------------
#  PG tools — identical in MCP and backend, import directly
# ---------------------------------------------------------------------------

pg_query_financials = _pg.pg_query_financials
pg_list_tables = _pg.pg_list_tables
pg_describe_table = _pg.pg_describe_table
pg_get_xero_gl_sample = _pg.pg_get_xero_gl_sample
pg_get_share_data = _pg.pg_get_share_data
pg_get_share_summary = _pg.pg_get_share_summary

# ---------------------------------------------------------------------------
#  TM1 Metadata — cached wrappers
#
#  Each wrapper checks _tm1_cache before calling the underlying TM1 function.
#  Results stay cached until the next schema-poll detects a change.
# ---------------------------------------------------------------------------

# -- no-arg structure lookups -----------------------------------------------

def tm1_list_cubes() -> dict[str, Any]:
    key = _cache_key("tm1_list_cubes")
    cached = _tm1_cache.get(key)
    if cached is not None:
        return cached
    result = _tm1.tm1_list_cubes()
    if "error" not in result:
        _tm1_cache.set(key, result)
    return result


def tm1_list_dimensions() -> dict[str, Any]:
    key = _cache_key("tm1_list_dimensions")
    cached = _tm1_cache.get(key)
    if cached is not None:
        return cached
    result = _tm1.tm1_list_dimensions()
    if "error" not in result:
        _tm1_cache.set(key, result)
    return result


def tm1_list_processes() -> dict[str, Any]:
    key = _cache_key("tm1_list_processes")
    cached = _tm1_cache.get(key)
    if cached is not None:
        return cached
    result = _tm1.tm1_list_processes()
    if "error" not in result:
        _tm1_cache.set(key, result)
    return result


# -- single-arg structure lookups -------------------------------------------

def tm1_get_process_code(process_name: str) -> dict[str, Any]:
    key = _cache_key("tm1_get_process_code", process_name)
    cached = _tm1_cache.get(key)
    if cached is not None:
        return cached
    result = _tm1.tm1_get_process_code(process_name)
    if "error" not in result:
        _tm1_cache.set(key, result)
    return result


def tm1_get_cube_rules(cube_name: str) -> dict[str, Any]:
    key = _cache_key("tm1_get_cube_rules", cube_name)
    cached = _tm1_cache.get(key)
    if cached is not None:
        return cached
    result = _tm1.tm1_get_cube_rules(cube_name)
    if "error" not in result:
        _tm1_cache.set(key, result)
    return result


def tm1_list_views(cube_name: str) -> dict[str, Any]:
    key = _cache_key("tm1_list_views", cube_name)
    cached = _tm1_cache.get(key)
    if cached is not None:
        return cached
    result = _tm1.tm1_list_views(cube_name)
    if "error" not in result:
        _tm1_cache.set(key, result)
    return result


def tm1_export_dimension_attributes(
    dimension_name: str,
    hierarchy_name: str = "",
    element_type: str = "",
    attributes: list[str] | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    key = _cache_key("tm1_export_dimension_attributes", dimension_name, hierarchy_name, element_type, limit)
    if not attributes:
        cached = _tm1_cache.get(key)
        if cached is not None:
            return cached
    result = _tm1.tm1_export_dimension_attributes(
        dimension_name=dimension_name,
        hierarchy_name=hierarchy_name,
        element_type=element_type,
        attributes=attributes,
        limit=limit,
    )
    if "error" not in result and not attributes:
        _tm1_cache.set(key, result)
    return result


# -- not cached (always live) -----------------------------------------------

tm1_get_server_info = _tm1.tm1_get_server_info

# -- element/hierarchy metadata (cached) ------------------------------------

def tm1_get_dimension_elements(
    dimension_name: str,
    hierarchy_name: str = "",
    element_type: str = "",
    limit: int = 200,
) -> dict[str, Any]:
    key = _cache_key("tm1_get_dimension_elements", dimension_name, hierarchy_name, element_type, limit)
    cached = _tm1_cache.get(key)
    if cached is not None:
        return cached
    result = _tm1.tm1_get_dimension_elements(
        dimension_name=dimension_name,
        hierarchy_name=hierarchy_name,
        element_type=element_type,
    )
    if "error" not in result and "elements" in result:
        result["elements"] = result["elements"][:limit]
        result["count"] = len(result["elements"])
        result["truncated"] = result["count"] >= limit
        _tm1_cache.set(key, result)
    return result


def tm1_get_element_attributes_bulk(
    dimension_name: str,
    elements: list[str] | None = None,
    attributes: list[str] | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    key = _cache_key("tm1_get_element_attributes_bulk", dimension_name, limit)
    if not elements and not attributes:
        cached = _tm1_cache.get(key)
        if cached is not None:
            return cached

    if not elements:
        try:
            tm1 = _get_tm1()
            hier = dimension_name
            all_names = tm1.elements.get_element_names(dimension_name, hier)
            elements = [e for e in all_names
                        if not e.startswith("All_") and not e.startswith("unmapped")][:limit]
        except Exception as e:
            return {"error": str(e)}

    result = _tm1.tm1_get_element_attributes_bulk(
        dimension_name=dimension_name,
        elements=elements,
        attributes=attributes,
    )
    if "error" not in result and not attributes:
        _tm1_cache.set(key, result)
    return result


# ---------------------------------------------------------------------------
#  TM1 Query — adapters for parameter name differences
# ---------------------------------------------------------------------------

def tm1_query_mdx(mdx: str, top: int = 1000) -> dict[str, Any]:
    """Adapter: backend schema uses 'top', MCP uses 'top_records'."""
    return _tm1.tm1_query_mdx(mdx=mdx, top_records=top)


def tm1_execute_mdx_rows(mdx: str, top: int = 500) -> dict[str, Any]:
    """Adapter: apply top limit to MCP result."""
    result = _tm1.tm1_execute_mdx_rows(mdx=mdx)
    if "error" not in result and "rows" in result:
        result["rows"] = result["rows"][:top]
        result["count"] = len(result["rows"])
    return result


def tm1_read_view(cube_name: str, view_name: str) -> dict[str, Any]:
    """Direct import — compatible signatures."""
    return _tm1.tm1_read_view(cube_name=cube_name, view_name=view_name, private=False)


def tm1_get_cell_value(cube_name: str, coordinates: list) -> dict[str, Any]:
    """Adapter: backend schema uses 'coordinates', MCP uses 'elements'."""
    return _tm1.tm1_get_cell_value(cube_name=cube_name, elements=coordinates)


# ---------------------------------------------------------------------------
#  TM1 Management — adapters
# ---------------------------------------------------------------------------

def tm1_run_process(
    process_name: str,
    parameters: dict | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Adapter: backend defaults confirm=False (safe dry-run)."""
    if not confirm:
        return {
            "status": "dry_run",
            "process_name": process_name,
            "parameters": parameters or {},
            "message": "Dry run only. Set confirm=True to actually execute this process.",
        }
    result = _tm1.tm1_run_process(
        process_name=process_name,
        parameters=parameters,
        confirm=True,
    )
    _tm1_cache.invalidate()
    return result


def tm1_write_cell(
    cube_name: str,
    coordinates: list,
    value: float,
    confirm: bool = False,
) -> dict[str, Any]:
    """Adapter: backend uses 'coordinates', MCP uses 'elements'. Default confirm=False."""
    if not confirm:
        return {
            "status": "dry_run",
            "cube": cube_name,
            "coordinates": coordinates,
            "value": value,
            "message": "Dry run only. Set confirm=True to write.",
        }
    result = _tm1.tm1_write_cell(
        cube_name=cube_name,
        elements=coordinates,
        value=value,
        confirm=True,
    )
    _tm1_cache.invalidate()
    return result


# ---------------------------------------------------------------------------
#  Enhanced backend-only tools (use MCP's persistent TM1 connection)
# ---------------------------------------------------------------------------

def tm1_get_element_attributes(dimension_name: str) -> dict[str, Any]:
    """
    List all element attribute DEFINITIONS for a dimension (names and types).
    Different from MCP's tm1_get_element_attributes which gets VALUES for one element.
    """
    key = _cache_key("tm1_get_element_attributes", dimension_name)
    cached = _tm1_cache.get(key)
    if cached is not None:
        return cached
    try:
        tm1 = _get_tm1()
        attrs = tm1.elements.get_element_attributes(dimension_name, dimension_name)
        result = [{"name": a.name, "attribute_type": a.attribute_type} for a in attrs]
        out = {"dimension": dimension_name, "attributes": result}
        _tm1_cache.set(key, out)
        return out
    except Exception as e:
        return {"error": str(e)}


def tm1_get_element_attribute_value(
    dimension_name: str, element_name: str, attribute_name: str
) -> dict[str, Any]:
    """Read a single element attribute value."""
    key = _cache_key("tm1_get_element_attribute_value", dimension_name, element_name, attribute_name)
    cached = _tm1_cache.get(key)
    if cached is not None:
        return cached
    try:
        tm1 = _get_tm1()
        result_dict = tm1.elements.get_attribute_of_elements(
            dimension_name=dimension_name,
            hierarchy_name=dimension_name,
            attribute=attribute_name,
            elements=[element_name],
        )
        value = result_dict.get(element_name, None)
        out = {
            "dimension": dimension_name,
            "element": element_name,
            "attribute": attribute_name,
            "value": value,
        }
        _tm1_cache.set(key, out)
        return out
    except Exception as e:
        return {"error": str(e)}


def tm1_read_view_as_table(cube_name: str, view_name: str, top: int = 500) -> dict[str, Any]:
    """Read a named public view and return as a table (headers + rows)."""
    try:
        tm1 = _get_tm1()
        df = tm1.cells.execute_view_dataframe(
            cube_name=cube_name,
            view_name=view_name,
            private=False,
        )
        if df is None or len(df) == 0:
            return {"cube": cube_name, "view": view_name, "headers": [], "rows": [], "row_count": 0}
        return {
            "cube": cube_name,
            "view": view_name,
            "headers": list(df.columns),
            "rows": df.head(top).values.tolist(),
            "row_count": len(df),
        }
    except Exception as e:
        return {"error": str(e)}


def tm1_get_hierarchy(dimension_name: str, hierarchy_name: str = "") -> dict[str, Any]:
    """
    Return parent-child structure as edge list (backend format).
    MCP returns tree format; backend returns flat edges — keep backend format
    for consistency with existing AI behavior.
    """
    hier = hierarchy_name if hierarchy_name else dimension_name
    key = _cache_key("tm1_get_hierarchy", dimension_name, hier)
    cached = _tm1_cache.get(key)
    if cached is not None:
        return cached
    try:
        tm1 = _get_tm1()
        hierarchy = tm1.hierarchies.get(dimension_name, hier)
        edges = []
        for el in hierarchy.elements.values():
            for child_name, weight in el.components.items():
                edges.append({"parent": el.name, "child": child_name, "weight": weight})
        out = {
            "dimension": dimension_name,
            "hierarchy": hier,
            "edges": edges[:500],
            "truncated": len(edges) > 500,
        }
        _tm1_cache.set(key, out)
        return out
    except Exception as e:
        return {"error": str(e)}


def tm1_find_element(
    search: str,
    dimension_names: list[str] | None = None,
) -> dict[str, Any]:
    """
    Enhanced element search — searches names, attributes, and RAG context.
    Richer than MCP's tm1_find_element: includes attribute search on key dimensions,
    RAG context notes, and PostgreSQL fallback suggestions.
    Uses MCP's persistent TM1 connection.
    """
    try:
        # Ensure key dimension element data is pre-cached
        threading.Thread(target=_prewarm_element_lookups, daemon=True).start()
        tm1 = _get_tm1()
        if dimension_names:
            dims_to_search = dimension_names
        else:
            all_dims = tm1.dimensions.get_all_names()
            dims_to_search = [d for d in all_dims if not d.startswith("}")]

        search_lower = search.lower()
        found_in: list[dict] = []

        for dim in dims_to_search:
            try:
                elements = tm1.elements.get_element_names(dim, dim)
            except Exception:
                continue

            matches = []
            for el in elements:
                el_lower = el.lower()
                if el_lower == search_lower:
                    matches.append({"name": el, "match": "exact"})
                elif search_lower in el_lower or el_lower in search_lower:
                    matches.append({"name": el, "match": "partial"})

            if matches:
                matches.sort(key=lambda m: (0 if m["match"] == "exact" else 1, m["name"]))
                found_in.append({
                    "dimension": dim,
                    "matches": matches[:10],
                    "match_count": len(matches),
                })

        # Also try to get element context from RAG if we have an exact match
        context_notes = []
        if found_in:
            try:
                from apps.ai_agent.skills.element_context import get_element_context
                for hit in found_in:
                    for m in hit["matches"]:
                        if m["match"] == "exact":
                            ctx = get_element_context(hit["dimension"], m["name"])
                            if ctx and ctx.get("notes"):
                                context_notes.append({
                                    "dimension": hit["dimension"],
                                    "element": m["name"],
                                    "notes": ctx["notes"][:3],
                                })
            except Exception:
                pass

        # Search attributes/aliases on key dimensions (GUIDs won't match by name)
        _ATTR_SEARCH_DIMS = ["entity", "listed_share", "contact", "account"]
        dims_with_attrs = [d for d in _ATTR_SEARCH_DIMS if d in dims_to_search or not dimension_names]

        def _search_dim_attrs(dim):
            """Search attributes of a single dimension in parallel."""
            try:
                attr_defs = tm1.elements.get_element_attributes(dim, dim)

                # Fetch all attribute values in parallel
                def _fetch_attr(attr_name):
                    try:
                        return attr_name, tm1.elements.get_attribute_of_elements(dim, dim, attr_name)
                    except Exception:
                        return attr_name, {}

                attr_matches = []
                seen_elements = set()
                with ThreadPoolExecutor(max_workers=min(len(attr_defs), 6)) as attr_pool:
                    for attr_name, attr_values in attr_pool.map(
                        lambda a: _fetch_attr(a.name), attr_defs
                    ):
                        for el_name, attr_val in attr_values.items():
                            if el_name in seen_elements:
                                continue
                            if attr_val and search_lower in str(attr_val).lower():
                                seen_elements.add(el_name)
                                attr_matches.append({
                                    "name": el_name,
                                    "match": "attribute",
                                    "attribute": attr_name,
                                    "value": str(attr_val),
                                })
                                if len(attr_matches) >= 20:
                                    return attr_matches
                return attr_matches
            except Exception:
                return []

        # Search all key dimensions in parallel
        with ThreadPoolExecutor(max_workers=min(len(dims_with_attrs), 4)) as pool:
            futures = {pool.submit(_search_dim_attrs, d): d for d in dims_with_attrs}
            for fut in as_completed(futures):
                dim = futures[fut]
                attr_matches = fut.result()
                if attr_matches:
                    found_in.append({
                        "dimension": dim,
                        "matches": attr_matches,
                        "match_count": len(attr_matches),
                    })

        if not found_in:
            return {
                "status": "not_found",
                "search": search,
                "message": (
                    f"No element matching '{search}' found in any dimension (searched names and attributes). "
                    "For share/stock lookups, try pg_get_share_data(symbol_search) which searches PostgreSQL "
                    "by company name, symbol, or share code. Otherwise ask the user for clarification."
                ),
            }

        return {
            "status": "found",
            "search": search,
            "found_in": found_in,
            "context": context_notes if context_notes else None,
            "message": (
                f"Found '{search}' in {len(found_in)} dimension(s). "
                "If ambiguous, ask the user which dimension they meant."
            ),
        }
    except Exception as e:
        return {"error": str(e)}


def tm1_validate_elements(
    cube_name: str,
    elements: dict[str, str],
) -> dict[str, Any]:
    """
    Validate element names exist in their respective dimensions for a given cube.
    Enhanced version: validates at cube level (dimension→element pairs),
    returns suggestions for invalid elements.
    Uses MCP's persistent TM1 connection.
    """
    try:
        tm1 = _get_tm1()
        cube = tm1.cubes.get(cube_name)
        cube_dims = [d.lower() for d in cube.dimensions]

        invalid = []
        for dim_name, elem_name in elements.items():
            if dim_name.lower() not in cube_dims:
                actual_dim = None
                for cd in cube.dimensions:
                    if cd.lower() == dim_name.lower():
                        actual_dim = cd
                        break
                if not actual_dim:
                    invalid.append({
                        "dimension": dim_name,
                        "element": elem_name,
                        "issue": "dimension_not_in_cube",
                        "cube_dimensions": cube.dimensions,
                    })
                    continue
                dim_name = actual_dim

            try:
                exists = tm1.elements.exists(dim_name, dim_name, elem_name)
            except Exception:
                exists = False

            if not exists:
                try:
                    all_elements = tm1.elements.get_element_names(dim_name, dim_name)
                    suggestions = _fuzzy_match(elem_name, all_elements, max_results=5)
                except Exception:
                    suggestions = []
                invalid.append({
                    "dimension": dim_name,
                    "element": elem_name,
                    "issue": "member_not_found",
                    "suggestions": suggestions,
                })

        if not invalid:
            return {"status": "valid", "cube": cube_name, "message": "All elements exist."}

        return {
            "status": "invalid",
            "cube": cube_name,
            "invalid_elements": invalid,
            "message": (
                f"Found {len(invalid)} invalid element(s). "
                "Check the suggestions and ask the user which element they meant."
            ),
        }
    except Exception as e:
        return {"error": str(e)}


def tm1_update_element_attribute(
    dimension_name: str,
    element_name: str,
    attribute_name: str,
    value: str,
    confirm: bool = False,
) -> dict[str, Any]:
    """Update a single element attribute value. Requires confirm=True."""
    if not confirm:
        return {
            "status": "dry_run",
            "dimension": dimension_name,
            "element": element_name,
            "attribute": attribute_name,
            "new_value": value,
            "message": "Dry run only. Set confirm=True to update.",
        }
    try:
        tm1 = _get_tm1()
        tm1.elements.update_element_attribute(
            dimension_name=dimension_name,
            hierarchy_name=dimension_name,
            element_name=element_name,
            attribute_name=attribute_name,
            value=value,
        )
        _tm1_cache.invalidate(f"tm1_get_element_attribute")
        _tm1_cache.invalidate(f"tm1_get_element_attributes_bulk:{dimension_name}")
        _tm1_cache.invalidate(f"tm1_export_dimension_attributes:{dimension_name}")
        return {
            "status": "success",
            "dimension": dimension_name,
            "element": element_name,
            "attribute": attribute_name,
            "new_value": value,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ---------------------------------------------------------------------------
#  TM1 Report Builder (from MCP — natural language MDX)
# ---------------------------------------------------------------------------

tm1_build_report = _rb.tm1_build_report
tm1_resolve_report_elements = _rb.tm1_resolve_report_elements
tm1_list_report_cubes = _rb.tm1_list_report_cubes

# ---------------------------------------------------------------------------
#  SQL Builder (from MCP — natural language to SQL)
# ---------------------------------------------------------------------------

sql_build_query = _sql.sql_build_query
sql_list_tables_schema = _sql.sql_list_tables_schema


# ---------------------------------------------------------------------------
#  Fuzzy match helper (used by validate_elements)
# ---------------------------------------------------------------------------

def _fuzzy_match(query: str, candidates: list[str], max_results: int = 5) -> list[str]:
    query_lower = query.lower()
    scored = []
    for c in candidates:
        c_lower = c.lower()
        if c_lower == query_lower:
            scored.append((c, 100))
        elif c_lower.startswith(query_lower) or query_lower.startswith(c_lower):
            scored.append((c, 80))
        elif query_lower in c_lower or c_lower in query_lower:
            scored.append((c, 60))
        else:
            query_tokens = set(query_lower.replace("_", " ").replace("-", " ").split())
            c_tokens = set(c_lower.replace("_", " ").replace("-", " ").split())
            overlap = query_tokens & c_tokens
            if overlap:
                scored.append((c, 40 + len(overlap) * 10))
    scored.sort(key=lambda x: -x[1])
    return [s[0] for s in scored[:max_results]]


# ===================================================================
#  TOOL SCHEMAS — Anthropic format (used by tool_registry.py)
# ===================================================================

# --- TM1 Metadata schemas ---

TM1_METADATA_SCHEMAS = [
    {
        "name": "tm1_list_dimensions",
        "description": "Return all user dimension names in the TM1 model (excludes system dimensions).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "tm1_list_cubes",
        "description": "Return all user cubes with their dimension lists and whether they have rules.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "tm1_get_dimension_elements",
        "description": "Return elements of a TM1 dimension. Can filter to leaf (N) or consolidated (C) elements.",
        "input_schema": {
            "type": "object",
            "properties": {
                "dimension_name": {"type": "string", "description": "Exact dimension name, e.g. 'account'"},
                "hierarchy_name": {"type": "string", "description": "Named hierarchy e.g. 'Grouping'. Leave empty for default."},
                "element_type": {"type": "string", "description": "'all', 'leaf', or 'consolidated'"},
                "limit": {"type": "integer", "description": "Max elements to return (default 200)"},
            },
            "required": ["dimension_name"],
        },
    },
    {
        "name": "tm1_get_element_attributes",
        "description": "List all element attributes defined for a dimension (names and types).",
        "input_schema": {
            "type": "object",
            "properties": {
                "dimension_name": {"type": "string", "description": "Exact dimension name"},
            },
            "required": ["dimension_name"],
        },
    },
    {
        "name": "tm1_get_element_attribute_value",
        "description": "Read a single element attribute value, e.g. get the cashflow_category attribute for account 'acc_001'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "dimension_name": {"type": "string"},
                "element_name": {"type": "string"},
                "attribute_name": {"type": "string"},
            },
            "required": ["dimension_name", "element_name", "attribute_name"],
        },
    },
    {
        "name": "tm1_list_processes",
        "description": "Return all custom TI process names with parameter signatures (excludes Bedrock/system processes).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "tm1_get_process_code",
        "description": "Return the full TI process code (prolog, metadata, data, epilog) for a named process.",
        "input_schema": {
            "type": "object",
            "properties": {
                "process_name": {"type": "string", "description": "Exact process name, e.g. 'cub.gl_src_trial_balance.import'"},
            },
            "required": ["process_name"],
        },
    },
    {
        "name": "tm1_get_cube_rules",
        "description": "Return the TM1 rules text for a cube.",
        "input_schema": {
            "type": "object",
            "properties": {
                "cube_name": {"type": "string", "description": "Exact cube name, e.g. 'gl_pln_forecast'"},
            },
            "required": ["cube_name"],
        },
    },
    {
        "name": "tm1_get_hierarchy",
        "description": "Return the parent-child structure of a named hierarchy within a dimension.",
        "input_schema": {
            "type": "object",
            "properties": {
                "dimension_name": {"type": "string", "description": "e.g. 'account'"},
                "hierarchy_name": {"type": "string", "description": "e.g. 'Grouping'. Leave empty for default."},
            },
            "required": ["dimension_name"],
        },
    },
    {
        "name": "tm1_find_element",
        "description": (
            "Search for an element across dimensions to find where it belongs. "
            "Use this when the user mentions an element name and you're unsure which dimension "
            "it's in, or to disambiguate an element that could exist in multiple dimensions. "
            "Returns matching dimensions with exact/partial matches and any stored context notes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "search": {
                    "type": "string",
                    "description": "Element name or partial name, e.g. 'Klikk_Org', 'revenue', 'operating'",
                },
                "dimension_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional: limit search to these dimensions. Leave empty to search all.",
                },
            },
            "required": ["search"],
        },
    },
    {
        "name": "tm1_validate_elements",
        "description": (
            "Validate that element names exist in their dimensions for a given cube. "
            "Returns suggestions for any invalid elements. "
            "USE THIS when a query fails with 'member not found' — extract the failing element, "
            "validate it, then ask the user which suggestion they meant."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cube_name": {"type": "string", "description": "Exact cube name"},
                "elements": {
                    "type": "object",
                    "description": "Dict of {dimension_name: element_name} to validate",
                    "additionalProperties": {"type": "string"},
                },
            },
            "required": ["cube_name", "elements"],
        },
    },
    {
        "name": "tm1_get_element_attributes_bulk",
        "description": (
            "Read attribute values for multiple elements at once — returns a table. "
            "Much more efficient than calling tm1_get_element_attribute_value one at a time. "
            "IMPORTANT: Many dimensions use GUIDs as element names with friendly names in alias attributes. "
            "Use this tool to discover what elements actually represent (e.g. entity GUIDs → company names)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dimension_name": {"type": "string", "description": "Dimension name, e.g. 'entity', 'account'"},
                "elements": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of element names. If empty, reads all (up to limit).",
                },
                "attributes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of attribute names. If empty, reads all attributes.",
                },
                "limit": {"type": "integer", "description": "Max elements to return (default 100)"},
            },
            "required": ["dimension_name"],
        },
    },
    {
        "name": "tm1_export_dimension_attributes",
        "description": (
            "Export all elements of a dimension with their aliases and attributes as a flat table. "
            "Use to get a full picture of element metadata (names, aliases, types, custom attributes)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dimension_name": {"type": "string", "description": "Dimension name, e.g. 'account', 'entity', 'listed_share'"},
                "hierarchy_name": {"type": "string", "description": "Hierarchy (default: same as dimension)"},
                "element_type": {"type": "string", "description": "'Numeric', 'String', 'Consolidated', or '' for all"},
                "attributes": {"type": "array", "items": {"type": "string"}, "description": "Attribute names to include (default: all)"},
                "limit": {"type": "integer", "description": "Max elements (default 500)"},
            },
            "required": ["dimension_name"],
        },
    },
]

# --- TM1 Query schemas ---

TM1_QUERY_SCHEMAS = [
    {
        "name": "tm1_query_mdx",
        "description": "PRIMARY MDX tool — execute an MDX SELECT and return cell data as coordinate/value pairs. Use this for all data queries. For tabular display prefer tm1_execute_mdx_rows instead.",
        "input_schema": {
            "type": "object",
            "properties": {
                "mdx": {"type": "string", "description": "Full MDX SELECT statement"},
                "top": {"type": "integer", "description": "Max rows to return (default 1000)"},
            },
            "required": ["mdx"],
        },
    },
    {
        "name": "tm1_execute_mdx_rows",
        "description": "Execute an MDX query and return results as a table with column headers and rows. Better for displaying data than tm1_query_mdx.",
        "input_schema": {
            "type": "object",
            "properties": {
                "mdx": {"type": "string", "description": "Full MDX SELECT statement"},
                "top": {"type": "integer", "description": "Max rows (default 500)"},
            },
            "required": ["mdx"],
        },
    },
    {
        "name": "tm1_read_view",
        "description": "Read data from a named public TM1 view.",
        "input_schema": {
            "type": "object",
            "properties": {
                "cube_name": {"type": "string", "description": "Exact cube name e.g. 'gl_src_trial_balance'"},
                "view_name": {"type": "string", "description": "Exact view name e.g. 'ops_gl_import_check'"},
            },
            "required": ["cube_name", "view_name"],
        },
    },
    {
        "name": "tm1_get_cell_value",
        "description": "Read a single cell value from a TM1 cube by coordinate tuple.",
        "input_schema": {
            "type": "object",
            "properties": {
                "cube_name": {"type": "string", "description": "Exact cube name"},
                "coordinates": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Ordered element names, one per cube dimension",
                },
            },
            "required": ["cube_name", "coordinates"],
        },
    },
    {
        "name": "tm1_read_view_as_table",
        "description": "Read data from a named public TM1 view and return as a table with headers + rows. Use tm1_list_views first to find available views.",
        "input_schema": {
            "type": "object",
            "properties": {
                "cube_name": {"type": "string", "description": "Exact cube name"},
                "view_name": {"type": "string", "description": "Exact view name"},
                "top": {"type": "integer", "description": "Max rows (default 500)"},
            },
            "required": ["cube_name", "view_name"],
        },
    },
    {
        "name": "tm1_list_views",
        "description": "List all public views available for a given cube.",
        "input_schema": {
            "type": "object",
            "properties": {
                "cube_name": {"type": "string", "description": "Exact cube name"},
            },
            "required": ["cube_name"],
        },
    },
]

# --- TM1 Management schemas ---

TM1_MANAGEMENT_SCHEMAS = [
    {
        "name": "tm1_run_process",
        "description": (
            "Execute a TI process on the TM1 server. "
            "IMPORTANT: set confirm=True to actually run. Without it this is a safe dry-run preview."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "process_name": {"type": "string", "description": "Exact process name, e.g. 'cub.gl_src_trial_balance.import'"},
                "parameters": {
                    "type": "object",
                    "description": "Parameter name->value dict, e.g. {\"pYear\": \"2025\", \"pMonth\": \"Jul\"}",
                    "additionalProperties": {"type": "string"},
                },
                "confirm": {"type": "boolean", "description": "Must be true to actually execute. Default false."},
            },
            "required": ["process_name"],
        },
    },
    {
        "name": "tm1_write_cell",
        "description": (
            "Write a numeric value to a single TM1 cube cell. "
            "IMPORTANT: set confirm=True to actually write."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cube_name": {"type": "string"},
                "coordinates": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Ordered element names matching cube dimension order",
                },
                "value": {"type": "number", "description": "Numeric value to write"},
                "confirm": {"type": "boolean", "description": "Must be true to write"},
            },
            "required": ["cube_name", "coordinates", "value"],
        },
    },
    {
        "name": "tm1_update_element_attribute",
        "description": (
            "Update a TM1 element attribute value. "
            "IMPORTANT: set confirm=True to actually update."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dimension_name": {"type": "string"},
                "element_name": {"type": "string"},
                "attribute_name": {"type": "string"},
                "value": {"type": "string", "description": "New attribute value"},
                "confirm": {"type": "boolean", "description": "Must be true to update"},
            },
            "required": ["dimension_name", "element_name", "attribute_name", "value"],
        },
    },
    {
        "name": "tm1_get_server_info",
        "description": "Return TM1 server name and active session count.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "tm1_cache_stats",
        "description": "Return TM1 metadata cache statistics (hits, misses, cached keys, last poll).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "tm1_cache_clear",
        "description": "Manually clear the TM1 metadata cache. Use if you suspect stale data.",
        "input_schema": {"type": "object", "properties": {}},
    },
]

# --- PostgreSQL schemas ---

PG_SCHEMAS = [
    {
        "name": "pg_query_financials",
        "description": (
            "Run a read-only SELECT against klikk_financials_v4. SELECT only.\n"
            "IMPORTANT — use EXACT table names (Django snake_case). Common tables:\n"
            "  Xero GL:       xero_cube_xerotrailbalance (year, month, account_code, account_name, contact_name, tracking_option_1, tracking_option_2, amount, balance_to_date)\n"
            "  Symbols:       financial_investments_symbol (id, symbol, name, exchange, category)\n"
            "  Prices:        financial_investments_pricepoint (symbol_id, date, open, high, low, close, volume)\n"
            "  Dividends:     financial_investments_dividend (symbol_id, date, amount, currency)\n"
            "  Company info:  financial_investments_symbolinfo (symbol_id, data [JSONB], fetched_at)\n"
            "  Holdings:      investec_investecjseportfolio (date, share_code, company, quantity, total_value, profit_loss)\n"
            "  Transactions:  investec_investecjsetransaction (date, share_name, type, quantity, value)\n"
            "  Share mapping: investec_investecjsesharenamemapping (id, share_name, share_code, company)\n"
            "  Performance:   investec_investecjsesharemonthlyperformance (date, share_name, closing_price, dividend_yield)\n"
            "Do NOT guess table names — use pg_list_tables if unsure. Always use LIMIT."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "SELECT statement. Use EXACT table names from the description above. Always include LIMIT."},
                "limit": {"type": "integer", "description": "Max rows to return (default 100, max 1000)"},
            },
            "required": ["sql"],
        },
    },
    {
        "name": "pg_list_tables",
        "description": "List all tables in klikk_financials_v4 or klikk_bi_etl with sizes and row counts.",
        "input_schema": {
            "type": "object",
            "properties": {
                "database": {"type": "string", "description": "'financials' for klikk_financials_v4, 'bi' for klikk_bi_etl"},
            },
            "required": ["database"],
        },
    },
    {
        "name": "pg_describe_table",
        "description": "Return column names and data types for a PostgreSQL table.",
        "input_schema": {
            "type": "object",
            "properties": {
                "database": {"type": "string", "description": "'financials' or 'bi'"},
                "table_name": {"type": "string", "description": "Table name, e.g. 'xero_cube_xerotrailbalance' or 'public.my_table'"},
            },
            "required": ["database", "table_name"],
        },
    },
    {
        "name": "pg_get_xero_gl_sample",
        "description": "Fetch a sample of Xero GL trial balance rows from PostgreSQL for a given year and month.",
        "input_schema": {
            "type": "object",
            "properties": {
                "year": {"type": "integer", "description": "Calendar year, e.g. 2025"},
                "month": {"type": "integer", "description": "Month number 1-12"},
                "limit": {"type": "integer", "description": "Max rows (default 50)"},
            },
            "required": ["year", "month"],
        },
    },
    {
        "name": "pg_get_share_data",
        "description": (
            "Fetch detailed data for a specific share by symbol or name (fuzzy match). "
            "Returns holdings, dividends, prices, and/or transactions. "
            "Example: pg_get_share_data('Absa') or pg_get_share_data('NED.JO', 'holdings,dividends')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol_search": {
                    "type": "string",
                    "description": "Share symbol (e.g. 'ABG.JO') or company name (e.g. 'Absa'). Fuzzy matched.",
                },
                "include": {
                    "type": "string",
                    "description": "Comma-separated: holdings, dividends, prices, transactions, performance. Default: 'holdings,dividends,prices'.",
                },
            },
            "required": ["symbol_search"],
        },
    },
    {
        "name": "pg_get_share_summary",
        "description": "Fetch a summary of ALL tracked shares with latest prices and Investec portfolio positions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max rows (default 50, max 200)"},
            },
        },
    },
]

# --- TM1 Report Builder schemas (from MCP) ---

TM1_REPORT_BUILDER_SCHEMAS = [
    {
        "name": "tm1_build_report",
        "description": (
            "Build and execute a TM1 report from natural language. "
            "Examples: 'trial balance by account for 2025 actual', "
            "'share holdings by share for Klikk 2025', "
            "'cashflow summary by activity for 2025'. "
            "Auto-detects cube, resolves element names via aliases, builds MDX, and returns data."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural language report request"},
                "cube_name": {"type": "string", "description": "Override auto-detected cube (optional)"},
                "rows_dimension": {"type": "string", "description": "Force dimension on rows (optional)"},
                "columns_dimension": {"type": "string", "description": "Force dimension on columns (optional)"},
                "measure": {"type": "string", "description": "Force measure element (optional)"},
                "top_n": {"type": "integer", "description": "Max rows (default 50)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "tm1_resolve_report_elements",
        "description": (
            "Resolve natural language references to TM1 elements. "
            "Use to preview what the report builder would match before running a full report."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Text containing element references"},
                "dimension_name": {"type": "string", "description": "Limit search to this dimension (optional)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "tm1_list_report_cubes",
        "description": "List available cube profiles for natural language reporting with their keywords and dimensions.",
        "input_schema": {"type": "object", "properties": {}},
    },
]

# --- SQL Builder schemas ---

SQL_BUILDER_SCHEMAS = [
    {
        "name": "sql_build_query",
        "description": (
            "Build and execute a SQL query from natural language against klikk_financials_v4. "
            "Knows the database schema (Xero GL, Investec portfolio, market data, bank transactions). "
            "Examples: 'total expenses by account for 2025', 'top 10 shares by value', "
            "'dividends received this year', 'latest portfolio holdings'. "
            "Auto-detects relevant tables, builds SQL, and executes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "Natural language query, e.g. 'total expenses by account for 2025'"},
                "tables": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Override auto-detected tables (optional)",
                },
                "execute": {"type": "boolean", "description": "Execute the query (default true). Set false to only see the SQL."},
                "limit": {"type": "integer", "description": "Max rows (default 100)"},
            },
            "required": ["question"],
        },
    },
    {
        "name": "sql_list_tables_schema",
        "description": (
            "List all known PostgreSQL tables with descriptions, columns, and query keywords. "
            "Use this to understand what financial data is available before building queries."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]


# ===================================================================
#  Exported registries (consumed by tool_registry.py)
# ===================================================================

# Grouped schemas for keyword routing
TOOL_SCHEMAS_TM1_METADATA = TM1_METADATA_SCHEMAS
TOOL_SCHEMAS_TM1_QUERY = TM1_QUERY_SCHEMAS
TOOL_SCHEMAS_TM1_MANAGEMENT = TM1_MANAGEMENT_SCHEMAS
TOOL_SCHEMAS_PG = PG_SCHEMAS
TOOL_SCHEMAS_TM1_REPORT_BUILDER = TM1_REPORT_BUILDER_SCHEMAS
TOOL_SCHEMAS_SQL_BUILDER = SQL_BUILDER_SCHEMAS

# Combined for convenience
TOOL_SCHEMAS = (
    TM1_METADATA_SCHEMAS
    + TM1_QUERY_SCHEMAS
    + TM1_MANAGEMENT_SCHEMAS
    + PG_SCHEMAS
    + TM1_REPORT_BUILDER_SCHEMAS
    + SQL_BUILDER_SCHEMAS
)

TOOL_FUNCTIONS = {
    # TM1 Metadata
    "tm1_list_dimensions": tm1_list_dimensions,
    "tm1_list_cubes": tm1_list_cubes,
    "tm1_get_dimension_elements": tm1_get_dimension_elements,
    "tm1_get_element_attributes": tm1_get_element_attributes,
    "tm1_get_element_attribute_value": tm1_get_element_attribute_value,
    "tm1_list_processes": tm1_list_processes,
    "tm1_get_process_code": tm1_get_process_code,
    "tm1_get_cube_rules": tm1_get_cube_rules,
    "tm1_get_hierarchy": tm1_get_hierarchy,
    "tm1_find_element": tm1_find_element,
    "tm1_validate_elements": tm1_validate_elements,
    "tm1_get_element_attributes_bulk": tm1_get_element_attributes_bulk,
    "tm1_export_dimension_attributes": tm1_export_dimension_attributes,
    # TM1 Query
    "tm1_query_mdx": tm1_query_mdx,
    "tm1_execute_mdx_rows": tm1_execute_mdx_rows,
    "tm1_read_view": tm1_read_view,
    "tm1_get_cell_value": tm1_get_cell_value,
    "tm1_read_view_as_table": tm1_read_view_as_table,
    "tm1_list_views": tm1_list_views,
    # TM1 Management
    "tm1_run_process": tm1_run_process,
    "tm1_write_cell": tm1_write_cell,
    "tm1_update_element_attribute": tm1_update_element_attribute,
    "tm1_get_server_info": tm1_get_server_info,
    "tm1_cache_stats": tm1_cache_stats,
    "tm1_cache_clear": tm1_cache_clear,
    # PostgreSQL
    "pg_query_financials": pg_query_financials,
    "pg_list_tables": pg_list_tables,
    "pg_describe_table": pg_describe_table,
    "pg_get_xero_gl_sample": pg_get_xero_gl_sample,
    "pg_get_share_data": pg_get_share_data,
    "pg_get_share_summary": pg_get_share_summary,
    # TM1 Report Builder
    "tm1_build_report": tm1_build_report,
    "tm1_resolve_report_elements": tm1_resolve_report_elements,
    "tm1_list_report_cubes": tm1_list_report_cubes,
    # SQL Builder
    "sql_build_query": sql_build_query,
    "sql_list_tables_schema": sql_list_tables_schema,
}
