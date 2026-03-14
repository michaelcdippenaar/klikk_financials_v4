"""
TM1 Metadata — thin re-export from mcp_bridge (canonical MCP server tools).
Kept for backward compatibility with api/tm1.py and other imports.
"""
from apps.ai_agent.skills.mcp_bridge import (
    tm1_list_dimensions,
    tm1_list_cubes,
    tm1_get_dimension_elements,
    tm1_get_element_attributes,
    tm1_get_element_attribute_value,
    tm1_get_element_attributes_bulk,
    tm1_export_dimension_attributes,
    tm1_list_processes,
    tm1_get_process_code,
    tm1_get_cube_rules,
    tm1_get_hierarchy,
    tm1_find_element,
    tm1_validate_elements,
    TM1_METADATA_SCHEMAS as TOOL_SCHEMAS,
    TOOL_FUNCTIONS,
)

# Filter TOOL_FUNCTIONS to only metadata tools
TOOL_FUNCTIONS = {k: v for k, v in TOOL_FUNCTIONS.items() if k in (
    "tm1_list_dimensions", "tm1_list_cubes", "tm1_get_dimension_elements",
    "tm1_get_element_attributes", "tm1_get_element_attribute_value",
    "tm1_get_element_attributes_bulk", "tm1_export_dimension_attributes",
    "tm1_list_processes", "tm1_get_process_code", "tm1_get_cube_rules",
    "tm1_get_hierarchy", "tm1_find_element", "tm1_validate_elements",
)}
