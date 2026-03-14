"""
Tool registry — single source of truth for all tool schemas and functions.
Supports both Anthropic and OpenAI tool-calling formats.

Optimisation: route_tools_for_message() returns only the subset of tool
schemas relevant to a given user message, reducing token count and
improving tool selection accuracy.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

import logging

log = logging.getLogger('ai_agent')

from apps.ai_agent.skills import (
    mcp_bridge,
    tm1_rest_api,
    pattern_analysis,
    kpi_monitor,
    validation,
    element_context,
    web_search,
    google_drive,
    widget_generation,
    paw_integration,
    session_review,
    financials_data,
    statistics,
    share_metrics,
    context_memory,
    report_builder,
    ai_setup,
    investment_analyst,
    dividend_forecast,
    agent_monitor,
)

_SKILL_MODULES = [
    mcp_bridge,
    tm1_rest_api,
    pattern_analysis,
    kpi_monitor,
    validation,
    element_context,
    web_search,
    google_drive,
    widget_generation,
    paw_integration,
    session_review,
    financials_data,
    statistics,
    share_metrics,
    context_memory,
    report_builder,
    ai_setup,
    investment_analyst,
    dividend_forecast,
    agent_monitor,
]

# ---------------------------------------------------------------------------
# Full registries (used for call_tool dispatch — always complete)
# ---------------------------------------------------------------------------

ANTHROPIC_SCHEMAS: list[dict] = []
for _mod in _SKILL_MODULES:
    ANTHROPIC_SCHEMAS.extend(_mod.TOOL_SCHEMAS)

_FUNCTIONS: dict[str, Any] = {}
for _mod in _SKILL_MODULES:
    _FUNCTIONS.update(_mod.TOOL_FUNCTIONS)

# Map each tool name → skill module name (for logging which skill a tool belongs to)
TOOL_TO_SKILL: dict[str, str] = {}
for _mod in _SKILL_MODULES:
    _skill_name = _mod.__name__.rsplit(".", 1)[-1]
    for _schema in _mod.TOOL_SCHEMAS:
        TOOL_TO_SKILL[_schema["name"]] = _skill_name


def _to_openai_schema(anthropic_schema: dict) -> dict:
    """Convert one Anthropic tool schema to OpenAI function-calling format."""
    return {
        "type": "function",
        "function": {
            "name": anthropic_schema["name"],
            "description": anthropic_schema.get("description", ""),
            "parameters": anthropic_schema.get("input_schema", {"type": "object", "properties": {}}),
        },
    }


OPENAI_SCHEMAS: list[dict] = [_to_openai_schema(s) for s in ANTHROPIC_SCHEMAS]

# ---------------------------------------------------------------------------
# Tool routing — keyword-based module selection
# ---------------------------------------------------------------------------

_ALWAYS_MODULES = [mcp_bridge, widget_generation]

_KEYWORD_ROUTES: list[tuple[list[str], list]] = [
    (
        ["server status", "message log", "transaction log", "thread", "session",
         "sandbox", "error log", "rest api", "server health", "server version",
         "who is logged", "active user", "write value", "write cell",
         "execute view", "named view", "run view", "rpt_", "report view"],
        [tm1_rest_api],
    ),
    (
        ["process", "chore", "ti ", "turbointegrator", "run process",
         "execute process", "schedule"],
        [mcp_bridge],
    ),
    (
        ["postgres", "sql", "xero", "gl data", "general ledger", "database",
         "pg ", "trial_balance import", "share", "stock", "portfolio",
         "investment", "dividend", "price history", "holdings", "investec",
         "symbol", "pricepoint"],
        [mcp_bridge],
    ),
    (
        ["vectorized", "vector search", "financials data", "data guide",
         "how data fits", "corpora", "rag financials", "klikk financials",
         "trail balance", "xero cube", "consolidate journals"],
        [financials_data],
    ),
    (
        ["anomal", "pattern", "outlier", "variance", "spike", "unusual"],
        [pattern_analysis],
    ),
    (
        ["kpi", "metric", "threshold", "alert", "monitor", "target"],
        [kpi_monitor],
    ),
    (
        ["validate", "verification", "check model", "verify", "reconcil"],
        [validation],
    ),
    (
        ["element context", "save context", "what do we know about",
         "index dimension", "index element", "remember"],
        [element_context],
    ),
    (
        ["search", "google", "look up", "internet", "stock price",
         "current price", "news", "web", "article", "publish article",
         "put article", "show article", "fetch article", "read article",
         "write article", "write report", "write summary"],
        [web_search],
    ),
    (
        ["drive", "google drive", "document", "gdrive"],
        [google_drive],
    ),
    (
        ["paw", "planning analytics workspace", "embed", "paw book",
         "paw view", "writeback", "view mdx", "query state", "get view data",
         "current view", "view query state", "extract values", "view data"],
        [paw_integration],
    ),
    (
        ["session", "chat log", "review session", "review chat",
         "improve skill", "improve tool", "chat history log"],
        [session_review],
    ),
    (
        ["forecast trend", "trend analysis", "time series", "history predict",
         "statistics", "linear trend", "growth rate", "volatility"],
        [statistics],
    ),
    (
        ["dividend per share", "payout ratio", "eps", "dividend yield",
         "share metrics", "dps", "retention ratio", "dividends per share"],
        [share_metrics],
    ),
    (
        ["remember", "global context", "save fact", "what did i say",
         "past conversation", "what do we know", "recall", "explained",
         "i told you", "context memory", "global fact"],
        [context_memory],
    ),
    (
        ["ai setup", "refresh rag", "update rag", "re-index", "reindex",
         "rag status", "seed facts", "refresh knowledge", "update knowledge",
         "rebuild index", "ai_setup"],
        [ai_setup],
    ),
    (
        ["investment analyst", "look up share", "share lookup", "find share",
         "upcoming dividend", "which stock", "which share", "p/e ratio",
         "pe ratio", "dividend yield", "analyst recommendation", "price target",
         "share data", "share cube", "dimension", "share research",
         "portfolio summary", "investment overview", "screen share",
         "filter stock", "filter share", "below", "above", "yield above",
         "ratio below", "return on investment", "roi", "performance",
         "compare share", "compare stock", "vs ", " versus ",
         "dividend growth", "dividend analysis", "how has",
         "price return", "total return", "yield on cost", "cagr"],
        [investment_analyst, web_search],
    ),
    (
        ["chart", "graph", "line chart", "bar chart", "pie chart", "visuali",
         "plot", "trend chart", "price chart", "price history chart"],
        [investment_analyst, mcp_bridge, share_metrics],
    ),
    (
        ["report", "dividend report", "holdings report", "transaction summary",
         "portfolio report", "build report", "google finance", "dividend history",
         "dividends received", "my holdings", "my portfolio", "what do i hold",
         "show my shares", "performance report"],
        [report_builder, mcp_bridge],
    ),
    (
        ["dividend forecast", "dividend adjustment", "declared dividend",
         "adjust dps", "budget dps", "dividend budget", "dps adjustment",
         "declared dps", "forecast dps", "pln_forecast", "dividend calendar"],
        [dividend_forecast],
    ),
    (
        ["tm1 report", "trial balance report", "natural language report",
         "build tm1 report", "resolve element", "report cube"],
        [mcp_bridge],
    ),
    (
        ["trial balance", "balance sheet", "income statement", "profit and loss",
         "p&l", "cash flow", "cube", "mdx", "dimension", "hierarchy",
         "element", "subset", "consolidat", "account", "entity", "version",
         "actual", "budget", "budget forecast", "variance", "period", "month",
         "january", "february", "march", "april", "may", "june",
         "july", "august", "september", "october", "november", "december",
         "klikk group", "klikk pty", "tremly", "gl_src", "data entry",
         "write back", "tm1", "planning analytics"],
        [mcp_bridge, kpi_monitor],
    ),
    (
        ["agent monitor", "agent health", "agent performance", "agent diagnose",
         "slow tool", "error rate", "health check", "system health",
         "tool performance", "session analytics", "how am i doing",
         "self diagnose", "agent status"],
        [agent_monitor],
    ),
]

# Build per-module schema lists once at startup
_MODULE_ANTHROPIC: dict[int, list[dict]] = {}
_MODULE_OPENAI: dict[int, list[dict]] = {}
for _mod in _SKILL_MODULES:
    _mid = id(_mod)
    _MODULE_ANTHROPIC[_mid] = list(_mod.TOOL_SCHEMAS)
    _MODULE_OPENAI[_mid] = [_to_openai_schema(s) for s in _mod.TOOL_SCHEMAS]


def route_tools_for_message(user_message: str) -> tuple[list[dict], list[dict], list[str]]:
    """Return (anthropic_schemas, openai_schemas, skill_names) relevant to *user_message*.

    Always includes core modules (mcp_bridge, widget_generation).
    Adds extra modules only when keywords match. Falls back to ALL tools if
    nothing matches beyond the always-on set (safety net).

    The third element is a sorted list of skill module names that were routed.
    """
    lower = (user_message or "").lower()
    selected_ids: set[int] = {id(m) for m in _ALWAYS_MODULES}

    for keywords, modules in _KEYWORD_ROUTES:
        if any(kw in lower for kw in keywords):
            for m in modules:
                selected_ids.add(id(m))

    # Safety: if only always-modules matched, send everything so the model
    # isn't limited on ambiguous queries.
    if selected_ids == {id(m) for m in _ALWAYS_MODULES}:
        skill_names = sorted(m.__name__.rsplit(".", 1)[-1] for m in _SKILL_MODULES)
        log.info("Tool routing: all skills (no keyword match)")
        return ANTHROPIC_SCHEMAS, OPENAI_SCHEMAS, skill_names

    # Collect matched skill names
    _id_to_name = {id(m): m.__name__.rsplit(".", 1)[-1] for m in _SKILL_MODULES}
    skill_names = sorted(_id_to_name[mid] for mid in selected_ids if mid in _id_to_name)
    log.info("Tool routing: %s", ", ".join(skill_names))

    anth: list[dict] = []
    oai: list[dict] = []
    for mid in selected_ids:
        anth.extend(_MODULE_ANTHROPIC.get(mid, []))
        oai.extend(_MODULE_OPENAI.get(mid, []))
    return anth, oai, skill_names


# ---------------------------------------------------------------------------
# Tool result helpers
# ---------------------------------------------------------------------------

MAX_RESULT_CHARS = 4000


def _smart_truncate(result: Any, max_chars: int) -> str:
    """Truncate structured results intelligently, preserving summary info."""
    if not isinstance(result, dict):
        text = json.dumps(result, default=str, indent=2)
        if len(text) > max_chars:
            return text[:max_chars] + "\n... (truncated)"
        return text

    # Find list-valued keys (rows, elements, matches, etc.)
    list_keys = [k for k, v in result.items() if isinstance(v, list) and len(v) > 0]

    if not list_keys:
        text = json.dumps(result, default=str, indent=2)
        if len(text) > max_chars:
            return text[:max_chars] + "\n... (truncated)"
        return text

    # Progressively trim the largest list until it fits
    trimmed = dict(result)
    for attempt in range(5):
        text = json.dumps(trimmed, default=str, indent=2)
        if len(text) <= max_chars:
            return text

        # Find the longest list and trim it
        longest_key = max(list_keys, key=lambda k: len(trimmed.get(k, [])))
        lst = trimmed[longest_key]
        if len(lst) <= 2:
            break
        keep = max(len(lst) // 2, 2)
        trimmed[longest_key] = lst[:keep]
        trimmed[f"_{longest_key}_note"] = f"Showing {keep} of {len(lst)} items"

    text = json.dumps(trimmed, default=str, indent=2)
    if len(text) > max_chars:
        return text[:max_chars] + "\n... (truncated)"
    return text


def tool_result_to_str(result: Any, max_chars: int = MAX_RESULT_CHARS) -> str:
    """Serialise a tool result to a string for API tool_result content.
    Truncates large results intelligently to *max_chars*.
    """
    if isinstance(result, str):
        text = result
    elif isinstance(result, dict):
        return _smart_truncate(result, max_chars)
    else:
        try:
            text = json.dumps(result, default=str, indent=2)
        except Exception:
            text = str(result)

    if len(text) > max_chars:
        return text[:max_chars] + "\n... (truncated)"
    return text


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

def call_tool(name: str, args: dict) -> Any:
    """
    Call a registered tool function by name with the given args dict.
    Returns the function result or an error dict on failure.
    """
    func = _FUNCTIONS.get(name)
    if not func:
        log.warning("Unknown tool called: %s", name, extra={"tool": name})
        return {"error": f"Unknown tool: '{name}'. Available: {sorted(_FUNCTIONS.keys())}"}

    t0 = time.monotonic()
    try:
        result = func(**args)
        duration = int((time.monotonic() - t0) * 1000)
        if isinstance(result, dict) and "error" in result:
            log.error(
                "Tool %s returned error: %s", name, result["error"],
                extra={"tool": name, "detail": json.dumps(args, default=str),
                       "error_type": "tool_result_error", "duration_ms": duration},
            )
        else:
            log.info("Tool %s OK (%dms)", name, duration,
                     extra={"tool": name, "duration_ms": duration})
        return result
    except TypeError as e:
        duration = int((time.monotonic() - t0) * 1000)
        log.error("Invalid args for %s: %s", name, e,
                  extra={"tool": name, "detail": json.dumps(args, default=str),
                         "error_type": "invalid_args", "duration_ms": duration},
                  exc_info=True)
        return {"error": f"Invalid arguments for {name}: {e}"}
    except Exception as e:
        duration = int((time.monotonic() - t0) * 1000)
        log.error("Tool %s exception: %s", name, e,
                  extra={"tool": name, "detail": json.dumps(args, default=str),
                         "error_type": type(e).__name__, "duration_ms": duration},
                  exc_info=True)
        return {"error": f"{type(e).__name__}: {e}"}


def list_tool_names() -> list[str]:
    return sorted(_FUNCTIONS.keys())
