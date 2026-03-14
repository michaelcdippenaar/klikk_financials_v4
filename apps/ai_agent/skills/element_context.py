"""
Skill: Element Context — vectorise dimension elements, save and retrieve
accumulated context per element.

How it works:
1. index_dimension_elements() pulls every element + attributes from TM1,
   builds a rich text profile, embeds it via VoyageAI, and stores in pgvector.
2. save_element_context() lets the agent (or user) attach a context note
   to any element. The note is embedded and stored alongside the element profile.
3. get_element_context() retrieves all stored context for an element.
4. After each agent turn, core.py calls auto_extract_context() which asks
   the LLM to identify elements mentioned and insights learned, then saves them.
"""
from __future__ import annotations

import sys
import os
import json
import time
from typing import Any
from pathlib import Path

import numpy as np
import psycopg2
import psycopg2.extras

from apps.ai_agent.agent.config import TM1_CONFIG, settings
from TM1py import TM1Service

try:
    from apps.ai_agent.rag.embedder import embed_texts as _embed_texts_raw
    from pgvector.psycopg2 import register_vector
    _RAG_AVAILABLE = True
except ImportError:
    _RAG_AVAILABLE = False


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def _get_pg_conn():
    conn = psycopg2.connect(
        host=settings.pg_bi_host, port=settings.pg_bi_port,
        dbname=settings.pg_bi_db, user=settings.pg_bi_user,
        password=settings.pg_bi_password,
    )
    register_vector(conn)
    return conn


def _embed_texts(texts: list[str]) -> list[list[float]]:
    results = []
    for i in range(0, len(texts), 64):
        results.extend(_embed_texts_raw(texts[i:i + 64]))
    return results


def _upsert_doc(conn, doc_id: str, source_path: str, doc_type: str,
                title: str, content: str, embedding: list[float],
                metadata: dict) -> None:
    sql = f"""
        INSERT INTO {settings.rag_schema}.documents
            (doc_id, source_path, doc_type, title, content, embedding, metadata)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (doc_id) DO UPDATE SET
            content    = EXCLUDED.content,
            embedding  = EXCLUDED.embedding,
            metadata   = EXCLUDED.metadata,
            indexed_at = NOW()
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            doc_id, source_path, doc_type, title,
            content, np.array(embedding), json.dumps(metadata),
        ))
    conn.commit()


def _insert_doc(conn, doc_id: str, source_path: str, doc_type: str,
                title: str, content: str, embedding: list[float],
                metadata: dict) -> None:
    """Insert without ON CONFLICT — allows multiple context notes per element."""
    sql = f"""
        INSERT INTO {settings.rag_schema}.documents
            (doc_id, source_path, doc_type, title, content, embedding, metadata)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            doc_id, source_path, doc_type, title,
            content, np.array(embedding), json.dumps(metadata),
        ))
    conn.commit()


# ---------------------------------------------------------------------------
#  Build rich text profile for a dimension element
# ---------------------------------------------------------------------------

def _build_element_profile(
    dim_name: str,
    element_name: str,
    element_type: str,
    attributes: dict[str, Any],
    parents: list[str],
    children: list[str],
) -> str:
    """Build a rich text description of a single dimension element.

    Aliases and name attributes are placed prominently at the top so that
    vector embeddings capture them for semantic search (e.g. searching
    'Absa' matches element 'ABG' via its name/alias attribute).
    """
    # Extract alias/name attributes to put them prominently in the profile
    alias_keys = {"name", "alias", "caption", "description", "company",
                  "share_name", "long_name", "display_name"}
    alias_parts = []
    for attr_name, attr_val in (attributes or {}).items():
        if attr_val is not None and str(attr_val).strip():
            if attr_name.lower() in alias_keys or "alias" in attr_name.lower():
                alias_parts.append(str(attr_val))

    lines = [
        f"Dimension: {dim_name}",
        f"Element: {element_name}",
    ]
    # Put aliases right after the element name for embedding prominence
    if alias_parts:
        lines.append(f"Also known as: {', '.join(alias_parts)}")
    lines.append(f"Type: {element_type}")

    if attributes:
        lines.append("Attributes:")
        for attr_name, attr_val in attributes.items():
            if attr_val is not None and str(attr_val).strip():
                lines.append(f"  {attr_name}: {attr_val}")
    if parents:
        lines.append(f"Parent consolidations: {', '.join(parents)}")
    if children:
        lines.append(f"Children: {', '.join(children[:20])}")
        if len(children) > 20:
            lines.append(f"  ... and {len(children) - 20} more")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
#  Tool functions
# ---------------------------------------------------------------------------

def index_dimension_elements(dimension_name: str) -> dict[str, Any]:
    """
    Vectorise ALL elements of a dimension with their full attribute profiles.
    Stores each element as a document in pgvector for RAG retrieval.
    This connects to TM1 to read elements + attributes, then embeds via VoyageAI.

    dimension_name: e.g. 'account', 'entity', 'listed_share', 'cashflow_activity'
    """
    if not _RAG_AVAILABLE:
        return {"error": "RAG not available (sentence-transformers or pgvector not installed)"}

    try:
        with TM1Service(**TM1_CONFIG) as tm1:
            # Get elements
            elements = tm1.elements.get_elements(dimension_name, dimension_name)

            # Get attribute names
            attr_defs = tm1.elements.get_element_attributes(dimension_name, dimension_name)
            attr_names = [a.name for a in attr_defs]

            # Get hierarchy edges for parent/child info
            hierarchy = tm1.hierarchies.get(dimension_name, dimension_name)
            parent_map: dict[str, list[str]] = {}
            child_map: dict[str, list[str]] = {}
            for el in hierarchy.elements.values():
                for child_name in el.components:
                    parent_map.setdefault(child_name, []).append(el.name)
                    child_map.setdefault(el.name, []).append(child_name)

            # Build profiles
            profiles: list[tuple[str, str, str, dict]] = []  # (element_name, profile_text, el_type, attrs)
            for el in elements:
                el_name = el.name
                el_type = el.element_type.value

                # Fetch attributes
                attrs = {}
                for attr_name in attr_names:
                    try:
                        val = tm1.elements.get_attribute_value(
                            dimension_name, dimension_name, el_name, attr_name
                        )
                        attrs[attr_name] = val
                    except Exception:
                        pass

                profile = _build_element_profile(
                    dim_name=dimension_name,
                    element_name=el_name,
                    element_type=el_type,
                    attributes=attrs,
                    parents=parent_map.get(el_name, []),
                    children=child_map.get(el_name, []),
                )
                profiles.append((el_name, profile, el_type, attrs))

        # Embed all profiles
        texts = [p[1] for p in profiles]
        embeddings = _embed_texts(texts)

        # Store in pgvector
        conn = _get_pg_conn()
        for (el_name, profile, el_type, attrs), emb in zip(profiles, embeddings):
            doc_id = f"element::{dimension_name}::{el_name}"
            _upsert_doc(
                conn,
                doc_id=doc_id,
                source_path=f"tm1_api::element::{dimension_name}::{el_name}",
                doc_type="element_profile",
                title=f"{dimension_name}:{el_name}",
                content=profile,
                embedding=emb,
                metadata={
                    "dimension": dimension_name,
                    "element": el_name,
                    "element_type": el_type,
                    "attributes": {k: str(v) for k, v in attrs.items() if v},
                },
            )
        conn.close()

        return {
            "dimension": dimension_name,
            "elements_indexed": len(profiles),
            "attributes_per_element": len(attr_names),
            "attribute_names": attr_names,
        }

    except Exception as e:
        return {"error": str(e)}


def save_element_context(
    dimension_name: str,
    element_name: str,
    context_note: str,
) -> dict[str, Any]:
    """
    Save a context note about a specific dimension element.
    The note is embedded and stored in pgvector for future RAG retrieval.
    Use this to accumulate knowledge about elements as you learn about them.

    dimension_name: e.g. 'account'
    element_name: e.g. 'acc_001'
    context_note: Free-text insight, e.g. 'This is the main office rent account for Klikk HQ,
                  typically R45K/month, mapped to operating_payments in cashflow'
    """
    if not _RAG_AVAILABLE:
        return {"error": "RAG not available"}

    try:
        content = (
            f"Context for {dimension_name}:{element_name}\n\n"
            f"{context_note}"
        )
        embeddings = _embed_texts([content])

        conn = _get_pg_conn()
        # Use timestamp in doc_id to allow multiple notes per element
        ts = int(time.time() * 1000)
        doc_id = f"element_context::{dimension_name}::{element_name}::{ts}"

        _upsert_doc(
            conn,
            doc_id=doc_id,
            source_path=f"element_context::{dimension_name}::{element_name}",
            doc_type="element_context",
            title=f"Context: {dimension_name}:{element_name}",
            content=content,
            embedding=embeddings[0],
            metadata={
                "dimension": dimension_name,
                "element": element_name,
                "timestamp": ts,
            },
        )
        conn.close()

        return {
            "status": "saved",
            "dimension": dimension_name,
            "element": element_name,
            "context_preview": context_note[:200],
        }
    except Exception as e:
        return {"error": str(e)}


def get_element_context(
    dimension_name: str,
    element_name: str,
) -> dict[str, Any]:
    """
    Retrieve all stored context notes for a specific dimension element.
    Returns the element profile (if indexed) plus any accumulated context notes.

    dimension_name: e.g. 'account'
    element_name: e.g. 'acc_001'
    """
    try:
        conn = psycopg2.connect(
            host=settings.pg_bi_host, port=settings.pg_bi_port,
            dbname=settings.pg_bi_db, user=settings.pg_bi_user,
            password=settings.pg_bi_password,
        )
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                f"""
                SELECT doc_id, doc_type, title, content, metadata, indexed_at
                FROM {settings.rag_schema}.documents
                WHERE source_path LIKE %s
                ORDER BY indexed_at
                """,
                (f"%::{dimension_name}::{element_name}%",),
            )
            rows = cur.fetchall()
        conn.close()

        if not rows:
            return {
                "dimension": dimension_name,
                "element": element_name,
                "profile": None,
                "context_notes": [],
                "message": "No context stored for this element. Use index_dimension_elements or save_element_context.",
            }

        profile = None
        notes = []
        for row in rows:
            if row["doc_type"] == "element_profile":
                profile = row["content"]
            elif row["doc_type"] == "element_context":
                notes.append({
                    "content": row["content"],
                    "indexed_at": str(row["indexed_at"]),
                })

        return {
            "dimension": dimension_name,
            "element": element_name,
            "profile": profile,
            "context_notes": notes,
            "total_notes": len(notes),
        }
    except Exception as e:
        return {"error": str(e)}


def index_all_key_dimensions() -> dict[str, Any]:
    """
    Vectorise elements from all key dimensions in one go.
    Indexes: account, entity, cashflow_activity, listed_share, month, version.
    This can take a few minutes for large dimensions.
    """
    key_dims = ["account", "entity", "cashflow_activity", "listed_share", "month", "version"]
    results = {}
    for dim in key_dims:
        result = index_dimension_elements(dim)
        results[dim] = {
            "elements_indexed": result.get("elements_indexed", 0),
            "error": result.get("error"),
        }
    total = sum(r.get("elements_indexed", 0) for r in results.values())
    return {"dimensions_indexed": len(key_dims), "total_elements": total, "details": results}


# ---------------------------------------------------------------------------
#  Tool schemas
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "name": "index_dimension_elements",
        "description": (
            "Vectorise ALL elements of a TM1 dimension with their full attribute profiles "
            "and hierarchy info. Stores in pgvector for RAG. "
            "Run this to make the agent aware of element details."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dimension_name": {
                    "type": "string",
                    "description": "Dimension to index, e.g. 'account', 'entity', 'cashflow_activity'",
                },
            },
            "required": ["dimension_name"],
        },
    },
    {
        "name": "save_element_context",
        "description": (
            "Save a context note about a specific dimension element. "
            "Use this to store insights discovered during analysis — "
            "e.g. 'acc_001 is the main office rent account, typically R45K/month'. "
            "These notes are embedded and available for future RAG retrieval."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dimension_name": {"type": "string", "description": "e.g. 'account'"},
                "element_name": {"type": "string", "description": "e.g. 'acc_001'"},
                "context_note": {
                    "type": "string",
                    "description": "Free-text insight or description about this element",
                },
            },
            "required": ["dimension_name", "element_name", "context_note"],
        },
    },
    {
        "name": "get_element_context",
        "description": "Retrieve the stored profile and all context notes for a dimension element.",
        "input_schema": {
            "type": "object",
            "properties": {
                "dimension_name": {"type": "string"},
                "element_name": {"type": "string"},
            },
            "required": ["dimension_name", "element_name"],
        },
    },
    {
        "name": "index_all_key_dimensions",
        "description": (
            "Vectorise elements from all key dimensions in one go: "
            "account, entity, cashflow_activity, listed_share, month, version. "
            "Takes a few minutes for large dimensions."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]

TOOL_FUNCTIONS = {
    "index_dimension_elements": index_dimension_elements,
    "save_element_context": save_element_context,
    "get_element_context": get_element_context,
    "index_all_key_dimensions": index_all_key_dimensions,
}
