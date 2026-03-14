"""
PostgreSQL Query — thin re-export from mcp_bridge (canonical MCP server tools).
Kept for backward compatibility with api/sql.py and other imports.
"""
from apps.ai_agent.skills.mcp_bridge import (
    pg_query_financials,
    pg_list_tables,
    pg_describe_table,
    pg_get_xero_gl_sample,
    pg_get_share_data,
    pg_get_share_summary,
    PG_SCHEMAS as TOOL_SCHEMAS,
    TOOL_FUNCTIONS,
)

# Filter TOOL_FUNCTIONS to only PG tools
TOOL_FUNCTIONS = {k: v for k, v in TOOL_FUNCTIONS.items() if k in (
    "pg_query_financials", "pg_list_tables",
    "pg_describe_table", "pg_get_xero_gl_sample",
    "pg_get_share_data", "pg_get_share_summary",
)}
