"""
Skill: AI Setup — refresh the RAG vector store by querying all databases and TM1 live.

The agent calls these tools to keep its own knowledge up to date:
  - ai_setup_refresh_rag:  Full or partial RAG re-index (PG schemas, TM1 dims, docs, elements)
  - ai_setup_rag_status:   Show what's currently indexed and when it was last refreshed
  - ai_setup_seed_facts:   Seed/refresh global business facts in pgvector
"""
from __future__ import annotations

import json
import sys
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import logging

from apps.ai_agent.agent.config import settings

log = logging.getLogger('ai_agent')

# ---------------------------------------------------------------------------
#  Lazy imports (heavy dependencies only loaded when actually called)
# ---------------------------------------------------------------------------

def _import_indexer():
    """Import indexer components lazily to avoid startup overhead."""
    from apps.ai_agent.rag.indexer import (
        get_pg_conn, embed_chunks, upsert_chunks,
        collect_doc_chunks, collect_tm1_chunks,
        collect_pg_schema_chunks, collect_data_context_chunks,
        run_element_indexing,
    )
    from apps.ai_agent.rag.chunker import Chunk
    from apps.ai_agent.rag.embedder import LocalEmbedder
    return {
        "get_pg_conn": get_pg_conn,
        "embed_chunks": embed_chunks,
        "upsert_chunks": upsert_chunks,
        "collect_doc_chunks": collect_doc_chunks,
        "collect_tm1_chunks": collect_tm1_chunks,
        "collect_pg_schema_chunks": collect_pg_schema_chunks,
        "collect_data_context_chunks": collect_data_context_chunks,
        "run_element_indexing": run_element_indexing,
        "Chunk": Chunk,
        "LocalEmbedder": LocalEmbedder,
    }


# ---------------------------------------------------------------------------
#  Global facts (same as scripts/seed_global_facts.py but callable as tool)
# ---------------------------------------------------------------------------

_GLOBAL_FACTS = [
    ("TM1 listed_share element names are short codes (ABG, SBK, NED). "
     "The attribute 'share_name' has the full name (ABSAGROUP, STANBANK). "
     "Use pg_get_share_data(symbol_search) to fuzzy-match by company name, symbol, or share code.",
     {"tags": ["share", "TM1", "lookup"]}),

    ("Absa Group Limited is listed on JSE as ABG.JO. TM1 element: ABG. "
     "Investec share_name: ABSAGROUP. Company: ABSA GROUP LIMITED.",
     {"tags": ["share", "JSE", "Absa"]}),

    ("Standard Bank Group Limited is listed on JSE as SBK.JO. TM1 element: SBK. "
     "Investec share_name: STANBANK. Company: STANDARD BANK GROUP LTD.",
     {"tags": ["share", "JSE", "StandardBank"]}),

    ("Dividend yield = annual dividends / share price. "
     "TTM yield = sum of last 12 months dividends / current price. "
     "Use build_dividend_yield_chart(symbol) for yield over time visualization. "
     "Investec MonthlyPerformance table has pre-calculated dividend_yield (as decimal).",
     {"tags": ["share", "dividend", "yield", "formula"]}),

    ("Investec Portfolio is a holdings export (point-in-time snapshots: quantity, cost, value, P&L). "
     "Investec Transaction is an activity export (buys, sells, dividends, fees). "
     "Transaction types: Buy, Sell, Dividend, Special Dividend, Foreign Dividend, Dividend Tax, Fee, Broker Fee.",
     {"tags": ["Investec", "data", "portfolio", "transaction"]}),

    ("JSE share prices are in ZAR cents (e.g. 23992 = R239.92). "
     "When displaying prices to the user, consider dividing by 100 for rand values. "
     "US shares have exchange_rate applied for ZAR conversion.",
     {"tags": ["share", "JSE", "price", "currency"]}),

    ("For share reports, use: build_dividend_report(symbol) for Google Finance-style dividend report, "
     "build_dividend_yield_chart(symbol) for yield over time, "
     "build_holdings_report() for full portfolio, "
     "build_transaction_summary(symbol) for buy/sell/dividend activity. "
     "These return multiple widgets that render as a dashboard.",
     {"tags": ["report", "share", "tools"]}),

    ("NEVER create chart widgets (BarChart, LineChart, PieChart) with empty props. "
     "Always query the data first, then pass results as xAxis + series with data arrays. "
     "An empty chart renders blank and is useless to the user.",
     {"tags": ["widget", "chart", "rule"]}),
]


# ---------------------------------------------------------------------------
#  Tool: ai_setup_refresh_rag
# ---------------------------------------------------------------------------

_refresh_lock = threading.Lock()
_refresh_status: dict[str, Any] = {"running": False, "last_result": None}


def ai_setup_refresh_rag(
    scope: str = "full",
    element_dims: list[str] | None = None,
) -> dict[str, Any]:
    """
    Refresh the RAG vector store by querying PostgreSQL schemas, TM1 dimensions,
    documentation files, and optionally per-element profiles.

    scope: What to re-index. Options:
      - 'full' — everything: docs + TM1 dims + PG schemas + data context + elements
      - 'docs' — documentation markdown files only
      - 'tm1' — TM1 dimension metadata only
      - 'schema' — PostgreSQL table schemas + data context (relationships, pipelines)
      - 'elements' — per-element profiles for key dimensions
    element_dims: Optional list of specific dimension names for element indexing.
                  Only used when scope='elements'. Default: all key dimensions.
    """
    # No API key required — uses local sentence-transformers model

    if not _refresh_lock.acquire(blocking=False):
        return {"error": "RAG refresh already in progress. Try again later.",
                "status": _refresh_status}

    try:
        _refresh_status["running"] = True
        _refresh_status["started_at"] = datetime.now(timezone.utc).isoformat()
        t0 = time.monotonic()

        ix = _import_indexer()
        result: dict[str, Any] = {"scope": scope, "chunks_indexed": 0, "sections": {}}

        # Element-only mode (uses its own embedding pipeline)
        if scope == "elements":
            log.info("ai_setup: indexing per-element profiles")
            ix["run_element_indexing"](element_dims or None)
            result["sections"]["elements"] = {"status": "ok", "dims": element_dims or "all key dims"}
            result["chunks_indexed"] = -1  # element indexer tracks its own count
            _finish_refresh(result, t0)
            return result

        # Chunk collection
        embedder = ix["LocalEmbedder"]()
        conn = ix["get_pg_conn"]()
        chunks = []

        if scope in ("full", "docs"):
            log.info("ai_setup: collecting doc chunks")
            doc_chunks = ix["collect_doc_chunks"]()
            chunks.extend(doc_chunks)
            result["sections"]["docs"] = len(doc_chunks)

        if scope in ("full", "tm1"):
            log.info("ai_setup: collecting TM1 dimension chunks")
            tm1_chunks = ix["collect_tm1_chunks"]()
            chunks.extend(tm1_chunks)
            result["sections"]["tm1_dimensions"] = len(tm1_chunks)

        if scope in ("full", "schema"):
            log.info("ai_setup: collecting PostgreSQL schema chunks")
            pg_chunks = ix["collect_pg_schema_chunks"]()
            chunks.extend(pg_chunks)
            result["sections"]["pg_schemas"] = len(pg_chunks)

            log.info("ai_setup: collecting data context chunks")
            ctx_chunks = ix["collect_data_context_chunks"]()
            chunks.extend(ctx_chunks)
            result["sections"]["data_context"] = len(ctx_chunks)

        if not chunks:
            result["warning"] = "No chunks collected"
            _finish_refresh(result, t0)
            conn.close()
            return result

        # Embed and upsert
        log.info("ai_setup: embedding %d chunks via sentence-transformers", len(chunks))
        embedded = ix["embed_chunks"](chunks, embedder)

        log.info("ai_setup: upserting into pgvector")
        count = ix["upsert_chunks"](conn, embedded)
        conn.close()
        result["chunks_indexed"] = count

        # If full, also do per-element indexing
        if scope == "full":
            log.info("ai_setup: indexing per-element profiles (key dimensions)")
            ix["run_element_indexing"](None)
            result["sections"]["elements"] = "all key dimensions"

        _finish_refresh(result, t0)
        return result

    except Exception as e:
        log.error("ai_setup_refresh_rag failed: %s", e, exc_info=True)
        _refresh_status["running"] = False
        _refresh_status["last_result"] = {"error": str(e)}
        return {"error": f"RAG refresh failed: {e}"}
    finally:
        _refresh_lock.release()


def _finish_refresh(result: dict, t0: float) -> None:
    duration = round(time.monotonic() - t0, 1)
    result["duration_seconds"] = duration
    _refresh_status["running"] = False
    _refresh_status["last_result"] = result
    _refresh_status["finished_at"] = datetime.now(timezone.utc).isoformat()
    log.info("ai_setup: RAG refresh complete in %.1fs — %s chunks", duration, result.get("chunks_indexed", 0))


# ---------------------------------------------------------------------------
#  Tool: ai_setup_rag_status
# ---------------------------------------------------------------------------

def ai_setup_rag_status() -> dict[str, Any]:
    """
    Show what's currently indexed in the RAG store: document counts by type,
    last indexed timestamps, and total chunk count.
    """
    import psycopg2
    import psycopg2.extras

    try:
        conn = psycopg2.connect(
            host=settings.pg_bi_host,
            port=settings.pg_bi_port,
            dbname=settings.pg_bi_db,
            user=settings.pg_bi_user,
            password=settings.pg_bi_password,
        )
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Count by doc_type
            cur.execute(f"""
                SELECT doc_type, COUNT(*) as count,
                       MIN(indexed_at) as oldest,
                       MAX(indexed_at) as newest
                FROM {settings.rag_schema}.documents
                GROUP BY doc_type
                ORDER BY doc_type
            """)
            by_type = [
                {
                    "doc_type": row["doc_type"],
                    "count": row["count"],
                    "oldest": row["oldest"].isoformat() if row["oldest"] else None,
                    "newest": row["newest"].isoformat() if row["newest"] else None,
                }
                for row in cur.fetchall()
            ]

            # Total count
            cur.execute(f"SELECT COUNT(*) FROM {settings.rag_schema}.documents")
            total = cur.fetchone()[0]

            # Global context facts count
            global_facts = 0
            try:
                cur.execute(f"SELECT COUNT(*) FROM {settings.rag_schema}.global_context")
                global_facts = cur.fetchone()[0]
            except Exception:
                conn.rollback()

            # Conversation context count
            conversation_chunks = 0
            try:
                cur.execute(f"SELECT COUNT(*) FROM {settings.rag_schema}.conversation_context")
                conversation_chunks = cur.fetchone()[0]
            except Exception:
                conn.rollback()

        conn.close()

        return {
            "total_documents": total,
            "by_type": by_type,
            "global_facts": global_facts,
            "conversation_context_chunks": conversation_chunks,
            "refresh_status": _refresh_status,
        }

    except Exception as e:
        return {"error": f"Could not query RAG status: {e}"}


# ---------------------------------------------------------------------------
#  Tool: ai_setup_seed_facts
# ---------------------------------------------------------------------------

def ai_setup_seed_facts() -> dict[str, Any]:
    """
    Seed or refresh global business facts in the pgvector store.
    These are pre-defined facts about share lookups, data models, widget rules, etc.
    Idempotent — safe to run multiple times.
    """
    try:
        # TODO: Replace with Django ORM calls for context store
        raise ImportError("context_store not yet migrated to Django")
    except ImportError:
        return {"error": "context_store not yet migrated to Django"}

    ensure_tables()
    results = []
    for content, meta in _GLOBAL_FACTS:
        try:
            row_id = save_global_context(content, metadata=meta)
            results.append({"id": row_id, "preview": content[:80], "status": "ok"})
        except Exception as e:
            results.append({"preview": content[:80], "status": "error", "error": str(e)})

    ok_count = sum(1 for r in results if r["status"] == "ok")
    return {
        "total": len(_GLOBAL_FACTS),
        "seeded": ok_count,
        "failed": len(_GLOBAL_FACTS) - ok_count,
        "details": results,
    }


# ---------------------------------------------------------------------------
#  Tool schemas
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "name": "ai_setup_refresh_rag",
        "description": (
            "Refresh the RAG vector store by querying all PostgreSQL database schemas, "
            "TM1 dimension metadata, documentation files, and per-element profiles. "
            "Use this after database changes, new tables, new TM1 dimensions, or new instruction files. "
            "Scope options: 'full' (everything), 'docs', 'tm1', 'schema', 'elements'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["full", "docs", "tm1", "schema", "elements"],
                    "description": (
                        "What to re-index: 'full' = everything, 'docs' = markdown files, "
                        "'tm1' = TM1 dimensions, 'schema' = PG table schemas + data context, "
                        "'elements' = per-element profiles for key dimensions"
                    ),
                },
                "element_dims": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Specific dimensions for element indexing (only used with scope='elements'). Default: all key dims.",
                },
            },
        },
    },
    {
        "name": "ai_setup_rag_status",
        "description": (
            "Show what's currently indexed in the RAG vector store: document counts by type, "
            "last indexed timestamps, global facts count, and conversation context count."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "ai_setup_seed_facts",
        "description": (
            "Seed or refresh global business facts in the pgvector store. "
            "Facts cover share lookups, data models, widget rules, and common patterns. "
            "Idempotent — safe to run multiple times."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
]

TOOL_FUNCTIONS = {
    "ai_setup_refresh_rag": ai_setup_refresh_rag,
    "ai_setup_rag_status": ai_setup_rag_status,
    "ai_setup_seed_facts": ai_setup_seed_facts,
}
