"""
Skill: Session Review — read chat logs and analyse them to identify
opportunities for improving tools, prompts, and agent behaviour.

Log files live in <project>/logs/chat_sessions/<session_id>.txt
(written by api/chat.py on every turn).

Tools:
  - list_chat_sessions:   List available session log files
  - read_chat_session:    Return the full text of a session log
  - review_chat_session:  Structured analysis with improvement suggestions
"""
from __future__ import annotations

import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any



_CHAT_LOG_DIR = Path(__file__).resolve().parent.parent.parent.parent / "logs" / "chat_sessions"


# ---------------------------------------------------------------------------
#  Tool functions
# ---------------------------------------------------------------------------

def list_chat_sessions(limit: int = 20) -> dict[str, Any]:
    """
    List available chat session log files, most recent first.
    Returns session IDs, sizes, and turn counts.

    limit: Max sessions to return (default 20)
    """
    if not _CHAT_LOG_DIR.is_dir():
        return {"sessions": [], "count": 0, "note": "No chat logs directory found yet."}

    files = sorted(_CHAT_LOG_DIR.glob("*.txt"),
                   key=lambda p: p.stat().st_mtime, reverse=True)

    sessions = []
    for f in files[:limit]:
        text = ""
        try:
            text = f.read_text(encoding="utf-8")
        except Exception:
            pass
        turn_count = text.count("\nUSER:\n")
        info = {
            "session_id": f.stem,
            "size_kb": round(f.stat().st_size / 1024, 1),
            "modified": datetime.fromtimestamp(
                f.stat().st_mtime, tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M UTC"),
            "turns": turn_count,
        }
        for line in text[:512].splitlines():
            if line.startswith("User:"):
                info["user"] = line.split(":", 1)[1].strip()
        sessions.append(info)

    return {"sessions": sessions, "count": len(sessions)}


def read_chat_session(session_id: str, last_n_turns: int = 0) -> dict[str, Any]:
    """
    Return the full text of a chat session log, or just the last N turns.

    session_id: The session ID (filename without .txt)
    last_n_turns: If > 0, return only the last N turns (0 = full log)
    """
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)
    path = _CHAT_LOG_DIR / f"{safe_id}.txt"

    if not path.exists():
        return {"error": f"Session '{session_id}' not found."}

    text = path.read_text(encoding="utf-8")

    if last_n_turns > 0:
        separator = "=" * 72
        parts = text.split(separator)
        header = parts[0] if parts else ""
        turn_blocks = [p for p in parts[1:] if "USER:" in p]
        selected = turn_blocks[-last_n_turns:]
        text = header + separator + separator.join(selected)

    if len(text) > 15000:
        text = text[:15000] + "\n\n... (truncated — use last_n_turns to read specific turns)"

    return {"session_id": session_id, "content": text}


def review_chat_session(session_id: str) -> dict[str, Any]:
    """
    Analyse a chat session and return structured insights:
    - tools used (frequency, successes, errors)
    - widgets created
    - user question patterns
    - concrete suggestions for improving skills/tools

    session_id: The session ID to review
    """
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)
    path = _CHAT_LOG_DIR / f"{safe_id}.txt"

    if not path.exists():
        return {"error": f"Session '{session_id}' not found."}

    text = path.read_text(encoding="utf-8")
    separator = "=" * 72
    blocks = text.split(separator)

    turn_count = 0
    tool_usage: dict[str, int] = defaultdict(int)
    tool_errors: dict[str, list[str]] = defaultdict(list)
    widgets_created: list[dict] = []
    user_questions: list[str] = []
    errors: list[str] = []
    tools_per_turn: list[int] = []

    for block in blocks:
        if "USER:" not in block:
            continue
        turn_count += 1

        user_match = re.search(r"USER:\n(.+?)(?:\n\n|\nTOOL|\nASSISTANT)", block, re.DOTALL)
        if user_match:
            user_questions.append(user_match.group(1).strip()[:200])

        turn_tools = 0
        for tc_match in re.finditer(r"> (\w+)\(", block):
            tool_name = tc_match.group(1)
            tool_usage[tool_name] += 1
            turn_tools += 1
        tools_per_turn.append(turn_tools)

        for err_match in re.finditer(
            r'> (\w+)\(.*?\n\s+Result:.*?"error":\s*"([^"]+)"',
            block, re.DOTALL
        ):
            tool_errors[err_match.group(1)].append(err_match.group(2)[:150])

        for w_match in re.finditer(r"- (\w+): (.+?) \(id=", block):
            widgets_created.append({"type": w_match.group(1), "title": w_match.group(2)})

        err_block = re.search(r"ERROR:\n(.+?)(?:\n\n|\nASSISTANT)", block, re.DOTALL)
        if err_block:
            errors.append(err_block.group(1).strip()[:200])

    # --- Build improvement suggestions ---
    suggestions: list[str] = []

    failed_tools = {t for t, errs in tool_errors.items() if errs}
    if failed_tools:
        for t in sorted(failed_tools):
            unique_errs = list(set(tool_errors[t]))[:3]
            suggestions.append(
                f"Tool '{t}' had errors: {'; '.join(unique_errs)}. "
                "Consider adding input validation, retry logic, or better error messages."
            )

    if any("not found" in e.lower() or "member" in e.lower() for e in errors):
        if "tm1_validate_elements" not in tool_usage and "tm1_find_element" not in tool_usage:
            suggestions.append(
                "Element-not-found errors occurred but validation tools were unused. "
                "Update the system prompt to instruct the agent to call "
                "tm1_validate_elements before running MDX queries."
            )

    if tool_usage.get("tm1_get_dimension_elements", 0) > 5:
        suggestions.append(
            "tm1_get_dimension_elements was called excessively. "
            "Consider pre-caching commonly-used dimension element lists in the system prompt "
            "or element_context store to reduce API calls."
        )

    avg_tools = sum(tools_per_turn) / max(len(tools_per_turn), 1)
    if avg_tools > 5:
        suggestions.append(
            f"Average {avg_tools:.1f} tool calls per turn is high. "
            "Look for opportunities to combine multiple API calls into fewer, "
            "more efficient tools (e.g. bulk attribute reads instead of one-at-a-time)."
        )

    if not widgets_created and turn_count > 3:
        data_questions = [q for q in user_questions if any(
            kw in q.lower() for kw in ["show", "chart", "graph", "view", "revenue", "data"]
        )]
        if data_questions:
            suggestions.append(
                "User asked data-related questions but no widgets were created. "
                "Strengthen the system prompt to encourage widget creation for data queries."
            )

    if errors:
        suggestions.append(
            f"{len(errors)} turn-level error(s) — the agent crashed or timed out. "
            "Review these turns and add defensive error handling."
        )

    if not suggestions:
        suggestions.append("Session looks healthy — no obvious issues found.")

    return {
        "session_id": session_id,
        "summary": {
            "turns": turn_count,
            "total_tool_calls": sum(tool_usage.values()),
            "avg_tools_per_turn": round(avg_tools, 1),
            "unique_tools_used": len(tool_usage),
            "widgets_created": len(widgets_created),
            "errors": len(errors),
        },
        "user_questions": user_questions,
        "tool_usage": dict(sorted(tool_usage.items(), key=lambda x: -x[1])),
        "tool_errors": {t: errs[:5] for t, errs in tool_errors.items()},
        "widgets": widgets_created,
        "errors": errors,
        "improvement_suggestions": suggestions,
    }


# ---------------------------------------------------------------------------
#  Tool registry interface
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "name": "list_chat_sessions",
        "description": (
            "List available chat session log files with metadata (turns, size, date). "
            "Use this to discover which sessions are available for review."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max sessions to return (default 20)",
                },
            },
        },
    },
    {
        "name": "read_chat_session",
        "description": (
            "Read the full transcript of a chat session. "
            "Shows every user message, tool call (with inputs and results), and assistant response. "
            "Use last_n_turns to read only the most recent turns from a large session."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session ID to read",
                },
                "last_n_turns": {
                    "type": "integer",
                    "description": "Only return the last N turns (0 = full log)",
                },
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "review_chat_session",
        "description": (
            "Analyse a chat session and get structured insights: "
            "tool usage frequency, errors, widgets created, and concrete suggestions "
            "for improving agent skills and tools. "
            "Use this after reading a session to get actionable improvement ideas."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session ID to analyse",
                },
            },
            "required": ["session_id"],
        },
    },
]

TOOL_FUNCTIONS = {
    "list_chat_sessions": list_chat_sessions,
    "read_chat_session": read_chat_session,
    "review_chat_session": review_chat_session,
}
