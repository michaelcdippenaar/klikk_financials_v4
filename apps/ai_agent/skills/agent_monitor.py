"""
Skill: Agent Monitor — Self-monitoring tools for the AI agent.

Provides comprehensive performance analytics, system health checks,
and diagnostic tools the agent can call to understand its own behavior.

Queries AgentToolExecutionLog, AgentSession, and in-memory caches to
surface tool latency, error rates, session trends, and system health.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.db.models import Avg, Count, F, Q
from django.db.models.functions import ExtractHour
from django.utils import timezone

log = logging.getLogger('ai_agent')


# ---------------------------------------------------------------------------
#  Tool Performance Analytics
# ---------------------------------------------------------------------------

def agent_tool_performance(
    hours: int = 24,
    tool_name: str = "",
) -> dict[str, Any]:
    """
    Analyse tool execution performance over a time window.
    Returns per-tool metrics: call count, success rate, avg/p95 latency, error counts.

    hours: Look-back window in hours (default 24).
    tool_name: Optional — filter to a specific tool name.
    """
    from apps.ai_agent.models import AgentToolExecutionLog

    since = timezone.now() - timedelta(hours=hours)
    qs = AgentToolExecutionLog.objects.filter(started_at__gte=since)
    if tool_name:
        qs = qs.filter(tool_name=tool_name)

    # Per-tool aggregation
    tool_stats = (
        qs.values("tool_name")
        .annotate(
            total=Count("id"),
            successes=Count("id", filter=Q(status="success")),
            errors=Count("id", filter=Q(status="error")),
            blocked=Count("id", filter=Q(status="blocked")),
        )
        .order_by("-total")
    )

    tools = []
    for ts in tool_stats:
        total = ts["total"]
        success_rate = round(ts["successes"] / total * 100, 1) if total else 0

        # Compute latency from started_at/finished_at
        latency_qs = qs.filter(
            tool_name=ts["tool_name"],
            finished_at__isnull=False,
        ).annotate(
            duration_ms=1000.0 * (
                F("finished_at__epoch") - F("started_at__epoch")
            ) if False else F("finished_at")  # placeholder, computed below
        )

        # Manual latency computation (Django doesn't natively support epoch diff)
        durations = []
        for log_entry in qs.filter(
            tool_name=ts["tool_name"],
            finished_at__isnull=False,
        ).values_list("started_at", "finished_at")[:500]:
            started, finished = log_entry
            if started and finished:
                dur = (finished - started).total_seconds() * 1000
                durations.append(dur)

        avg_ms = round(sum(durations) / len(durations)) if durations else None
        p95_ms = round(sorted(durations)[int(len(durations) * 0.95)]) if len(durations) >= 5 else avg_ms

        # Recent errors
        recent_errors = list(
            qs.filter(tool_name=ts["tool_name"], status="error")
            .values_list("error_message", flat=True)[:3]
        )

        tools.append({
            "tool_name": ts["tool_name"],
            "total_calls": total,
            "success_rate_pct": success_rate,
            "errors": ts["errors"],
            "blocked": ts["blocked"],
            "avg_latency_ms": avg_ms,
            "p95_latency_ms": p95_ms,
            "recent_errors": recent_errors if recent_errors else None,
        })

    return {
        "period_hours": hours,
        "total_executions": sum(t["total_calls"] for t in tools),
        "total_errors": sum(t["errors"] for t in tools),
        "tools": tools[:30],
    }


# ---------------------------------------------------------------------------
#  Session Analytics
# ---------------------------------------------------------------------------

def agent_session_analytics(
    days: int = 7,
) -> dict[str, Any]:
    """
    Analyse agent session trends over a time window.
    Returns: sessions per day, avg messages per session, tool calls per session,
    most active hours, top tools used.

    days: Look-back window in days (default 7).
    """
    from apps.ai_agent.models import AgentSession, AgentMessage, AgentToolExecutionLog

    since = timezone.now() - timedelta(days=days)

    # Session counts
    sessions = AgentSession.objects.filter(created_at__gte=since)
    total_sessions = sessions.count()

    # Messages per session
    msg_counts = (
        AgentMessage.objects
        .filter(session__created_at__gte=since, role__in=["user", "assistant"])
        .values("session_id")
        .annotate(msg_count=Count("id"))
    )
    avg_messages = round(
        sum(m["msg_count"] for m in msg_counts) / len(msg_counts), 1
    ) if msg_counts else 0

    # Tool calls per session
    tool_counts = (
        AgentToolExecutionLog.objects
        .filter(started_at__gte=since, session__isnull=False)
        .values("session_id")
        .annotate(tool_count=Count("id"))
    )
    avg_tools = round(
        sum(t["tool_count"] for t in tool_counts) / len(tool_counts), 1
    ) if tool_counts else 0

    # Sessions per day
    from django.db.models.functions import TruncDate
    daily = (
        sessions.annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(count=Count("id"))
        .order_by("day")
    )
    sessions_per_day = [
        {"date": str(d["day"]), "sessions": d["count"]}
        for d in daily
    ]

    # Most active hours
    hourly = (
        AgentToolExecutionLog.objects
        .filter(started_at__gte=since)
        .annotate(hour=ExtractHour("started_at"))
        .values("hour")
        .annotate(count=Count("id"))
        .order_by("-count")[:5]
    )
    peak_hours = [{"hour": h["hour"], "calls": h["count"]} for h in hourly]

    # Top tools by usage
    top_tools = (
        AgentToolExecutionLog.objects
        .filter(started_at__gte=since)
        .values("tool_name")
        .annotate(count=Count("id"))
        .order_by("-count")[:10]
    )

    return {
        "period_days": days,
        "total_sessions": total_sessions,
        "avg_messages_per_session": avg_messages,
        "avg_tool_calls_per_session": avg_tools,
        "sessions_per_day": sessions_per_day,
        "peak_hours": peak_hours,
        "top_tools": [{"tool": t["tool_name"], "calls": t["count"]} for t in top_tools],
    }


# ---------------------------------------------------------------------------
#  System Health Check
# ---------------------------------------------------------------------------

def agent_health_check() -> dict[str, Any]:
    """
    Run a comprehensive health check on all agent subsystems.
    Returns status for: TM1 connection, PostgreSQL, cache, RAG retriever,
    API keys, and recent error rates.
    """
    from apps.ai_agent.agent.config import settings, get_credential
    from apps.ai_agent.models import AgentToolExecutionLog

    health: dict[str, Any] = {"overall": "healthy", "checks": {}}

    # 1. TM1 Connection
    try:
        from apps.ai_agent.skills.mcp_bridge import _tm1_cache, tm1_cache_stats
        cache = tm1_cache_stats()
        health["checks"]["tm1_cache"] = {
            "status": "ok",
            "cached_keys": cache.get("cached_keys", 0),
            "hits": cache.get("hits", 0),
            "misses": cache.get("misses", 0),
            "hit_rate_pct": round(
                cache["hits"] / (cache["hits"] + cache["misses"]) * 100, 1
            ) if (cache.get("hits", 0) + cache.get("misses", 0)) > 0 else None,
            "last_poll_ago_s": cache.get("last_poll_ago_s"),
        }
    except Exception as e:
        health["checks"]["tm1_cache"] = {"status": "error", "error": str(e)}
        health["overall"] = "degraded"

    # 2. TM1 Connection (live check)
    try:
        from apps.ai_agent.tm1.tm1_tools import _get_tm1
        tm1 = _get_tm1()
        server_name = tm1.server.get_server_name()
        health["checks"]["tm1_connection"] = {"status": "ok", "server": server_name}
    except Exception as e:
        health["checks"]["tm1_connection"] = {"status": "error", "error": str(e)}
        health["overall"] = "degraded"

    # 3. RAG Retriever
    try:
        from apps.ai_agent.rag.retriever import _EMBED_AVAILABLE, _PGVECTOR_AVAILABLE
        health["checks"]["rag"] = {
            "status": "ok" if (_EMBED_AVAILABLE and _PGVECTOR_AVAILABLE) else "unavailable",
            "embed_available": _EMBED_AVAILABLE,
            "pgvector_available": _PGVECTOR_AVAILABLE,
        }
        if not _EMBED_AVAILABLE or not _PGVECTOR_AVAILABLE:
            health["overall"] = "degraded"
    except Exception as e:
        health["checks"]["rag"] = {"status": "error", "error": str(e)}

    # 4. API Keys
    api_keys = {}
    for key_name in ["anthropic_api_key", "openai_api_key"]:
        try:
            val = get_credential(key_name)
            api_keys[key_name] = "configured" if val else "missing"
        except Exception:
            api_keys[key_name] = "missing"
    health["checks"]["api_keys"] = api_keys

    # 5. AI Provider Config
    health["checks"]["ai_config"] = {
        "provider": settings.ai_provider,
        "anthropic_model": settings.anthropic_model,
        "openai_model": settings.openai_model,
        "max_tokens": settings.max_tokens,
        "max_tool_rounds": settings.max_tool_rounds,
    }

    # 6. Recent Error Rate (last hour)
    last_hour = timezone.now() - timedelta(hours=1)
    recent = AgentToolExecutionLog.objects.filter(started_at__gte=last_hour)
    total_recent = recent.count()
    errors_recent = recent.filter(status="error").count()
    error_rate = round(errors_recent / total_recent * 100, 1) if total_recent else 0
    health["checks"]["recent_errors"] = {
        "period": "last_hour",
        "total_calls": total_recent,
        "errors": errors_recent,
        "error_rate_pct": error_rate,
        "status": "ok" if error_rate < 20 else "warning" if error_rate < 50 else "critical",
    }
    if error_rate >= 50:
        health["overall"] = "critical"
    elif error_rate >= 20:
        health["overall"] = "degraded"

    # 7. Global Context stats
    try:
        from apps.ai_agent.models import GlobalContext
        health["checks"]["global_context"] = {
            "total_facts": GlobalContext.objects.count(),
            "with_embeddings": GlobalContext.objects.exclude(embedding=[]).count(),
        }
    except Exception:
        pass

    return health


# ---------------------------------------------------------------------------
#  Error Diagnosis
# ---------------------------------------------------------------------------

def agent_diagnose_errors(
    hours: int = 24,
    tool_name: str = "",
    limit: int = 20,
) -> dict[str, Any]:
    """
    Retrieve recent tool execution errors with full details for debugging.
    Shows error messages, input payloads, and timestamps.

    hours: Look-back window (default 24).
    tool_name: Optional filter to a specific tool.
    limit: Max errors to return (default 20).
    """
    from apps.ai_agent.models import AgentToolExecutionLog

    since = timezone.now() - timedelta(hours=hours)
    qs = AgentToolExecutionLog.objects.filter(
        started_at__gte=since,
        status="error",
    ).order_by("-started_at")

    if tool_name:
        qs = qs.filter(tool_name=tool_name)

    errors = []
    for entry in qs[:limit]:
        duration = None
        if entry.finished_at and entry.started_at:
            duration = round((entry.finished_at - entry.started_at).total_seconds() * 1000)
        errors.append({
            "tool_name": entry.tool_name,
            "error": entry.error_message[:500],
            "input": entry.input_payload,
            "timestamp": str(entry.started_at),
            "duration_ms": duration,
            "session_id": entry.session_id,
        })

    # Error frequency by tool
    error_by_tool = (
        qs.values("tool_name")
        .annotate(count=Count("id"))
        .order_by("-count")
    )

    return {
        "period_hours": hours,
        "total_errors": qs.count(),
        "errors_by_tool": [{"tool": e["tool_name"], "count": e["count"]} for e in error_by_tool],
        "recent_errors": errors,
    }


# ---------------------------------------------------------------------------
#  Slow Tool Detection
# ---------------------------------------------------------------------------

def agent_slow_tools(
    hours: int = 24,
    threshold_ms: int = 2000,
    limit: int = 20,
) -> dict[str, Any]:
    """
    Find the slowest tool executions over a time window.
    Useful for identifying performance bottlenecks.

    hours: Look-back window (default 24).
    threshold_ms: Only show tools slower than this (default 2000ms).
    limit: Max entries to return (default 20).
    """
    from apps.ai_agent.models import AgentToolExecutionLog

    since = timezone.now() - timedelta(hours=hours)
    qs = AgentToolExecutionLog.objects.filter(
        started_at__gte=since,
        finished_at__isnull=False,
        status="success",
    )

    slow_entries = []
    for entry in qs.order_by("-started_at")[:500]:
        dur = (entry.finished_at - entry.started_at).total_seconds() * 1000
        if dur >= threshold_ms:
            slow_entries.append({
                "tool_name": entry.tool_name,
                "duration_ms": round(dur),
                "input_summary": str(entry.input_payload)[:200],
                "timestamp": str(entry.started_at),
            })
        if len(slow_entries) >= limit:
            break

    # Sort by duration descending
    slow_entries.sort(key=lambda x: -x["duration_ms"])

    return {
        "period_hours": hours,
        "threshold_ms": threshold_ms,
        "slow_executions": slow_entries,
        "count": len(slow_entries),
    }


# ---------------------------------------------------------------------------
#  Tool schemas
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "name": "agent_tool_performance",
        "description": (
            "Analyse tool execution performance — call counts, success rates, "
            "avg/p95 latency, and error rates per tool. Use to identify which tools "
            "are slow, failing, or heavily used."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hours": {"type": "integer", "description": "Look-back window in hours (default 24)"},
                "tool_name": {"type": "string", "description": "Optional: filter to a specific tool"},
            },
        },
    },
    {
        "name": "agent_session_analytics",
        "description": (
            "Analyse agent session trends — sessions per day, avg messages/tool calls per session, "
            "peak usage hours, and top tools. Use for usage reporting and capacity planning."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Look-back window in days (default 7)"},
            },
        },
    },
    {
        "name": "agent_health_check",
        "description": (
            "Run a comprehensive health check on all agent subsystems: TM1 connection, "
            "cache hit rates, RAG retriever, API keys, AI config, and recent error rates. "
            "Use to diagnose why the agent might be slow or failing."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "agent_diagnose_errors",
        "description": (
            "Retrieve recent tool execution errors with full details — error messages, "
            "input payloads, timestamps, and error frequency by tool. "
            "Use to debug specific failures or investigate recurring issues."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hours": {"type": "integer", "description": "Look-back window in hours (default 24)"},
                "tool_name": {"type": "string", "description": "Optional: filter to a specific tool"},
                "limit": {"type": "integer", "description": "Max errors to return (default 20)"},
            },
        },
    },
    {
        "name": "agent_slow_tools",
        "description": (
            "Find the slowest tool executions — identifies performance bottlenecks. "
            "Shows tool name, duration, input summary, and timestamp for each slow call."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hours": {"type": "integer", "description": "Look-back window in hours (default 24)"},
                "threshold_ms": {"type": "integer", "description": "Only show tools slower than this (default 2000ms)"},
                "limit": {"type": "integer", "description": "Max entries (default 20)"},
            },
        },
    },
]

TOOL_FUNCTIONS = {
    "agent_tool_performance": agent_tool_performance,
    "agent_session_analytics": agent_session_analytics,
    "agent_health_check": agent_health_check,
    "agent_diagnose_errors": agent_diagnose_errors,
    "agent_slow_tools": agent_slow_tools,
}
