"""
Skill: Context Memory — Global Context and Conversation Context.

Global Context: Persistent facts across all sessions.
  "Absa is a South African bank listed on JSE as ABG" → saved once, retrievable everywhere.

Conversation Context: Every chat turn is embedded. Search past conversations by meaning.

Uses Django ORM for storage and VoyageAI for embeddings.
"""
from __future__ import annotations

import logging
from typing import Any

import numpy as np

from apps.ai_agent.agent.config import settings

log = logging.getLogger('ai_agent')


# ---------------------------------------------------------------------------
#  Embedding helper — reuses RAG retriever singleton
# ---------------------------------------------------------------------------

def _embed(text: str) -> list[float] | None:
    """Embed text using local sentence-transformers model."""
    try:
        from apps.ai_agent.rag.embedder import embed_one
        return embed_one(text)
    except Exception as e:
        log.debug("Embedding failed: %s", e)
        return None


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two embedding vectors."""
    a_arr, b_arr = np.array(a), np.array(b)
    norm = np.linalg.norm(a_arr) * np.linalg.norm(b_arr)
    if norm == 0:
        return 0.0
    return float(np.dot(a_arr, b_arr) / norm)


# ---------------------------------------------------------------------------
#  Tool implementations
# ---------------------------------------------------------------------------

def save_global_fact(content: str, tags: str = "") -> dict[str, Any]:
    """
    Save a fact or explanation to Global Context (persists across all sessions).
    Use when the user explains something or you learn a useful fact.
    Examples: "Absa is a South African bank", "acc_001 is office rent ~R45K/month".

    content: The fact or explanation to remember.
    tags: Optional comma-separated tags for metadata (e.g. "share,JSE,banking").
    """
    try:
        from apps.ai_agent.models import GlobalContext

        meta = {}
        if tags:
            meta["tags"] = [t.strip() for t in tags.split(",") if t.strip()]

        embedding = _embed(content) or []

        obj = GlobalContext.objects.create(
            content=content,
            metadata=meta,
            embedding=embedding,
        )
        return {"status": "saved", "id": obj.id, "content_preview": content[:200]}
    except Exception as e:
        log.error("save_global_fact error: %s", e, extra={"tool": "context_memory"})
        return {"error": str(e)}


def search_global_facts(query: str, top_k: int = 5) -> dict[str, Any]:
    """
    Semantic search over Global Context — finds facts saved across all sessions.
    Use to recall what the user has explained before or facts you stored.

    query: Natural language query, e.g. "What is Absa?" or "office rent account".
    top_k: Max results (default 5).
    """
    try:
        from apps.ai_agent.models import GlobalContext

        query_emb = _embed(query)
        if not query_emb:
            # Fallback to text search
            qs = GlobalContext.objects.filter(content__icontains=query)[:top_k]
            results = [{"content": obj.content, "id": obj.id, "score": None} for obj in qs]
            return {"query": query, "results": results, "count": len(results), "method": "text_search"}

        # Semantic search: score all entries with embeddings
        all_facts = GlobalContext.objects.exclude(embedding=[]).values_list("id", "content", "embedding", "metadata")
        scored = []
        for fact_id, content, embedding, meta in all_facts:
            if not embedding:
                continue
            score = _cosine_similarity(query_emb, embedding)
            if score >= (settings.rag_min_score or 0.3):
                scored.append({"id": fact_id, "content": content, "score": round(score, 4), "metadata": meta})

        scored.sort(key=lambda x: -x["score"])
        results = scored[:min(int(top_k), 20)]
        return {"query": query, "results": results, "count": len(results)}
    except Exception as e:
        log.error("search_global_facts error: %s", e, extra={"tool": "context_memory"})
        return {"error": str(e)}


def list_global_facts(limit: int = 20) -> dict[str, Any]:
    """
    List recent Global Context entries (most recent first, no search).
    """
    try:
        from apps.ai_agent.models import GlobalContext

        qs = GlobalContext.objects.order_by("-created_at")[:min(int(limit), 100)]
        entries = [{"id": obj.id, "content": obj.content, "metadata": obj.metadata} for obj in qs]
        return {"entries": entries, "count": len(entries)}
    except Exception as e:
        log.error("list_global_facts error: %s", e, extra={"tool": "context_memory"})
        return {"error": str(e)}


def search_past_conversations(query: str, session_id: str = "", top_k: int = 5) -> dict[str, Any]:
    """
    Semantic search over past conversation turns (all sessions or one specific session).
    Finds what was discussed before, even in other chat sessions.

    query: Natural language query, e.g. "Absa dividend" or "cashflow mapping".
    session_id: Optional. Restrict to a specific session, or leave empty for all sessions.
    top_k: Max results (default 5).
    """
    try:
        from apps.ai_agent.models import ConversationContext

        query_emb = _embed(query)
        if not query_emb:
            # Fallback to text search
            qs = ConversationContext.objects.filter(content__icontains=query)
            if session_id:
                qs = qs.filter(session_external_id=session_id)
            qs = qs.order_by("-created_at")[:top_k]
            results = [{"content": obj.content, "role": obj.role, "score": None} for obj in qs]
            return {"query": query, "session_id": session_id or "all", "results": results, "count": len(results)}

        # Semantic search
        qs = ConversationContext.objects.exclude(embedding=[])
        if session_id:
            qs = qs.filter(session_external_id=session_id)
        turns = qs.values_list("id", "content", "embedding", "role", "session_external_id", "created_at")

        scored = []
        for turn_id, content, embedding, role, sess_id, created_at in turns:
            if not embedding:
                continue
            score = _cosine_similarity(query_emb, embedding)
            if score >= (settings.rag_min_score or 0.3):
                scored.append({
                    "id": turn_id,
                    "content": content[:500],
                    "role": role,
                    "session_id": sess_id,
                    "score": round(score, 4),
                    "created_at": str(created_at),
                })

        scored.sort(key=lambda x: -x["score"])
        results = scored[:min(int(top_k), 20)]
        return {"query": query, "session_id": session_id or "all", "results": results, "count": len(results)}
    except Exception as e:
        log.error("search_past_conversations error: %s", e, extra={"tool": "context_memory"})
        return {"error": str(e)}


# ---------------------------------------------------------------------------
#  Tool schemas
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "name": "save_global_fact",
        "description": (
            "Save a fact or explanation to Global Context (persists across all sessions). "
            "Use when the user explains something or you learn a useful fact — "
            "e.g. 'Absa is a South African bank listed on JSE as ABG'. "
            "Stored with embeddings for semantic search."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The fact or explanation to remember."},
                "tags": {"type": "string", "description": "Optional comma-separated tags, e.g. 'share,JSE,banking'."},
            },
            "required": ["content"],
        },
    },
    {
        "name": "search_global_facts",
        "description": (
            "Semantic search over Global Context — finds facts saved across all sessions. "
            "Use to recall what the user has explained or facts previously stored. "
            "e.g. 'What is Absa?', 'office rent account'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural language query."},
                "top_k": {"type": "integer", "description": "Max results (default 5)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_global_facts",
        "description": "List recent Global Context entries (most recent first).",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max entries (default 20)."},
            },
        },
    },
    {
        "name": "search_past_conversations",
        "description": (
            "Semantic search over past conversation turns from all chat sessions or one session. "
            "Finds what was discussed before — e.g. 'Absa dividend', 'cashflow mapping'. "
            "Each turn is stored with PAW variables (cube, server, view) as metadata."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural language query."},
                "session_id": {"type": "string", "description": "Optional. Restrict to a specific session."},
                "top_k": {"type": "integer", "description": "Max results (default 5)."},
            },
            "required": ["query"],
        },
    },
]

TOOL_FUNCTIONS = {
    "save_global_fact": save_global_fact,
    "search_global_facts": search_global_facts,
    "list_global_facts": list_global_facts,
    "search_past_conversations": search_past_conversations,
}
