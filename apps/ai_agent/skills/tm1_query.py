"""
TM1 Query — thin re-export from mcp_bridge (canonical MCP server tools).
Kept for backward compatibility with api/tm1.py and other imports.
"""
from apps.ai_agent.skills.mcp_bridge import (
    tm1_query_mdx,
    tm1_execute_mdx_rows,
    tm1_read_view,
    tm1_get_cell_value,
    tm1_read_view_as_table,
    tm1_list_views,
    TM1_QUERY_SCHEMAS as TOOL_SCHEMAS,
    TOOL_FUNCTIONS,
)

# Filter TOOL_FUNCTIONS to only query tools
TOOL_FUNCTIONS = {k: v for k, v in TOOL_FUNCTIONS.items() if k in (
    "tm1_query_mdx", "tm1_execute_mdx_rows", "tm1_read_view",
    "tm1_get_cell_value", "tm1_read_view_as_table", "tm1_list_views",
)}
