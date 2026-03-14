"""
Widget Generation Skill — allows the AI agent to create dashboard widgets on the fly.

Widget type definitions are loaded from widget_types.yaml so new widget types
can be added or updated without touching this code. The tool schema and
validation are built dynamically from that file.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
#  Load widget type definitions from YAML
# ---------------------------------------------------------------------------

_WIDGET_TYPES_FILE = Path(__file__).parent.parent.parent / "widget_types.yaml"


def _load_widget_types() -> dict[str, dict]:
    """Load widget type definitions. Re-reads on each call so
    hot-reloads work during development."""
    if not _WIDGET_TYPES_FILE.exists():
        return {}
    with open(_WIDGET_TYPES_FILE) as f:
        data = yaml.safe_load(f) or {}
    return data.get("widget_types", {})


def _build_tool_description() -> str:
    """Build the tool description dynamically from widget_types.yaml."""
    wt = _load_widget_types()
    lines = [
        "Create a dynamic dashboard widget that renders in the user's browser. "
        "Use this to show data visually — cube views, charts, KPI cards, dimension trees, etc. "
        "The widget appears inline in the chat and can be pinned to the dashboard.\n\n"
        "Widget types:\n"
    ]
    for name, defn in wt.items():
        desc = defn.get("description", "")
        props = defn.get("props", {})
        prop_str = ", ".join(f"{k} ({v})" for k, v in props.items())
        lines.append(f"- {name}: {desc}. Props: {prop_str}")
    lines.append(
        "\n\nIMPORTANT RULES:"
        "\n1. For TM1 cube widgets: build valid MDX queries using dimension/element names from the model."
        "\n2. For charts with non-TM1 data (SQL, investments, web data): you MUST first fetch the data "
        "(using pg_query_financials, pg_get_share_data, investment_price_performance, etc.), "
        "then pass headers+rows to the chart widget. NEVER create a chart without data."
        "\n3. Example: to chart dividends, first call investment_dividend_analysis to get data, "
        "then create_dashboard_widget with headers=['Year','Amount'] and rows=[[2021,310],[2022,1125]]."
        "\n4. DataGrid, BarChart, LineChart, PieChart all accept headers+rows for inline data."
    )
    return "\n".join(lines)


def _build_tool_schema() -> dict:
    """Build the Anthropic tool schema dynamically from widget_types.yaml."""
    wt = _load_widget_types()
    type_names = list(wt.keys()) if wt else [
        "CubeViewer", "DimensionTree", "DimensionEditor",
        "KPICard", "LineChart", "BarChart", "PieChart",
        "PivotTable", "DataGrid", "MDXEditor",
    ]
    return {
        "name": "create_dashboard_widget",
        "description": _build_tool_description(),
        "input_schema": {
            "type": "object",
            "properties": {
                "widget_type": {
                    "type": "string",
                    "enum": type_names,
                    "description": "Type of widget to create",
                },
                "title": {
                    "type": "string",
                    "description": "Display title for the widget",
                },
                "props": {
                    "type": "object",
                    "description": "Widget-specific properties (see type descriptions above)",
                },
                "width": {
                    "type": "integer",
                    "enum": [1, 2, 3, 4],
                    "description": "Grid width (1-4 columns). Default 2.",
                },
                "height": {
                    "type": "string",
                    "enum": ["sm", "md", "lg"],
                    "description": "Height preset. sm=200px, md=400px, lg=600px. Default md.",
                },
                "prefetch": {
                    "type": "boolean",
                    "description": "If true, pre-fetch MDX data server-side so the widget renders instantly without a frontend API call. Use for inline chat widgets.",
                },
            },
            "required": ["widget_type", "title", "props"],
        },
    }


# ---------------------------------------------------------------------------
#  Widget creation
# ---------------------------------------------------------------------------

def create_dashboard_widget(
    widget_type: str,
    title: str,
    props: dict | None = None,
    width: int | None = None,
    height: str | None = None,
    prefetch: bool = False,
    **extra_kwargs,
) -> dict:
    """
    Create a widget configuration for the Vue frontend to render.
    Returns the widget config so the frontend can render it dynamically.
    """
    if props is None:
        props = {}
    # LLMs sometimes pass widget props (cube, mdx, xAxis, series, etc.) as
    # top-level kwargs instead of nested inside `props`. Merge them in.
    _KNOWN_ARGS = {"widget_type", "title", "props", "width", "height", "prefetch"}
    for k, v in extra_kwargs.items():
        if k not in _KNOWN_ARGS:
            props[k] = v
    widget_types = _load_widget_types()
    valid_types = list(widget_types.keys()) if widget_types else [
        "CubeViewer", "DimensionTree", "DimensionEditor",
        "KPICard", "LineChart", "BarChart", "PieChart",
        "PivotTable", "DataGrid", "MDXEditor",
    ]

    if widget_type not in valid_types:
        return {"error": f"Invalid widget_type: {widget_type}. Valid: {valid_types}"}

    # Apply defaults from YAML definition
    defn = widget_types.get(widget_type, {})
    if width is None:
        width = defn.get("default_width", 2)
    if height is None:
        height = defn.get("default_height", "md")

    widget_id = f"w_{uuid.uuid4().hex[:8]}"

    # For data-bound widgets, auto-build MDX from props if needed
    data = None
    if defn.get("auto_mdx") and "mdx" not in props:
        mdx = _build_mdx_from_props(props)
        if mdx:
            props["mdx"] = mdx

    if "rows" in props and widget_type in ("DataGrid", "BarChart", "LineChart", "PieChart"):
        data = {"headers": props.get("headers", []), "rows": props["rows"]}

    # Pre-fetch MDX data so the widget renders instantly (no frontend API call)
    if prefetch and not data and props.get("mdx"):
        try:
            from apps.ai_agent.skills.tm1_query import tm1_execute_mdx_rows
            result = tm1_execute_mdx_rows(props["mdx"], props.get("maxRows", 500))
            if "headers" in result and "rows" in result:
                data = {"headers": result["headers"], "rows": result["rows"]}
        except Exception:
            pass  # Fall back to frontend fetching

    # Convert backend size units to frontend grid units
    _WIDTH_MAP = {1: 3, 2: 6, 3: 9, 4: 12}
    _HEIGHT_MAP = {"sm": 4, "md": 8, "lg": 12}
    grid_w = _WIDTH_MAP.get(width, 6)
    grid_h = _HEIGHT_MAP.get(height, 8) if isinstance(height, str) else (height or 8)

    # Extract data source for refresh engine
    # TODO: widget_store not yet migrated to Django — extract data source inline
    try:
        from apps.ai_agent.agent.config import settings as _ws_settings
        data_source = None  # widget_store._extract_data_source(widget_type, props)
    except Exception:
        data_source = None

    widget_config = {
        "id": widget_id,
        "type": widget_type,
        "title": title,
        "w": grid_w,
        "h": grid_h,
        "x": 0,
        "y": 0,
        "props": props,
        "data_source": data_source,
        "refresh_seconds": 30,
    }

    if data:
        widget_config["data"] = data

    # TODO: widget_store not yet migrated to Django — skip DB persistence for now
    # Widget is returned inline and rendered by the frontend directly

    return {
        "status": "widget_created",
        "message": f"Created {widget_type} widget: {title}",
        "widget": widget_config,
    }


def _build_mdx_from_props(props: dict) -> str | None:
    """Build MDX SELECT from CubeViewer-style props."""
    cube = props.get("cube")
    rows = props.get("rows")
    columns = props.get("columns")
    slicers = props.get("slicers", {})

    if not cube or not rows or not columns:
        return None

    expand_row = props.get("expandRow", "")
    expand_col = props.get("expandCol", "")

    col_set = (
        f"{{[{columns}].[{expand_col}].Children}}"
        if expand_col
        else f"{{[{columns}].Members}}"
    )
    row_set = (
        f"{{[{rows}].[{expand_row}].Children}}"
        if expand_row
        else f"{{[{rows}].Members}}"
    )

    mdx = f"SELECT {col_set} ON 0, {row_set} ON 1 FROM [{cube}]"

    if slicers:
        where_parts = [f"[{dim}].[{el}]" for dim, el in slicers.items()]
        mdx += f" WHERE ({', '.join(where_parts)})"

    return mdx


# ---------------------------------------------------------------------------
#  Tool registry interface (loaded by tool_registry.py)
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [_build_tool_schema()]

TOOL_FUNCTIONS = {
    "create_dashboard_widget": create_dashboard_widget,
}
