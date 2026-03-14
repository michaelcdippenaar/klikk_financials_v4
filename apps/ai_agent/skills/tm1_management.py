"""
TM1 Management — thin re-export from mcp_bridge (canonical MCP server tools).
Kept for backward compatibility with api/tm1.py and other imports.
"""
from apps.ai_agent.skills.mcp_bridge import (
    tm1_run_process,
    tm1_write_cell,
    tm1_update_element_attribute,
    tm1_get_server_info,
    TM1_MANAGEMENT_SCHEMAS as TOOL_SCHEMAS,
    TOOL_FUNCTIONS,
)

# Filter TOOL_FUNCTIONS to only management tools
TOOL_FUNCTIONS = {k: v for k, v in TOOL_FUNCTIONS.items() if k in (
    "tm1_run_process", "tm1_write_cell", "tm1_update_element_attribute",
    "tm1_get_server_info",
)}
