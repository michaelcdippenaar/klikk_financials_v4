"""
RAG Indexer — reads documentation files, TM1 live metadata, PostgreSQL schemas,
and data context, generates embeddings via local sentence-transformers,
and stores vectors in pgvector.

Usage:
    cd /home/mc/apps/klikk_ai_portal/backend
    python rag/indexer.py --full        # re-index everything
    python rag/indexer.py --docs-only   # only documentation markdown files
    python rag/indexer.py --tm1-only    # only live TM1 dimension metadata
    python rag/indexer.py --schema      # PostgreSQL table schemas + data context
"""
from __future__ import annotations

import argparse
import json
import sys
import os
from pathlib import Path

import numpy as np
import psycopg2
import psycopg2.extras
from apps.ai_agent.rag.embedder import LocalEmbedder
from pgvector.psycopg2 import register_vector
from TM1py import TM1Service

from apps.ai_agent.agent.config import settings, TM1_CONFIG
from .chunker import (
    Chunk, chunk_markdown, chunk_model_state, chunk_tm1_dimension,
    chunk_pg_table_schema,
    chunk_share_data_relationships, chunk_gl_data_relationships,
    chunk_column_dimension_map, chunk_transaction_processing,
)

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent  # klikk_financials_v4/

# Directories to scan for .md files (relative to PROJECT_ROOT)
# Keep broad docs scanning limited to known knowledge folders.
_MD_SCAN_DIRS = ["documentation/knowledge_base"]
# In addition to knowledge_base, include explicitly-authored RAG seed docs.
_MD_RAG_FILE_PATTERNS = ("*_FOR_RAG.md",)
# High-value root files to always index
_MD_ROOT_FILES = [
    "DATABASE_SCHEMA.md",
    "KLIKK_V4.md",
    # Generated model reference used by the in-repo RAG server.
    "Rag Server/Django-Klikk-Financials-Models.md",
]
# Additional external markdown files to include in RAG indexing.
_MD_EXTERNAL_FILES = [
    "/home/mc/.claude/projects/klikk_rag_context.md",
]
# Directories to exclude from recursive scanning
_MD_EXCLUDE_DIRS = {"agent", ".git", "datafiles", "backup", "logfiles", ".venv", "node_modules"}


def discover_md_files() -> list[Path]:
    """Auto-discover markdown files intended for RAG indexing."""
    found: list[Path] = []

    # 1) Core curated knowledge-base folders
    for scan_dir in _MD_SCAN_DIRS:
        dir_path = PROJECT_ROOT / scan_dir
        if dir_path.is_dir():
            for md_file in sorted(dir_path.rglob("*.md")):
                # Skip files in excluded directories
                if not any(part in _MD_EXCLUDE_DIRS for part in md_file.parts):
                    found.append(md_file)

    # 2) Explicitly-marked RAG seed files anywhere under /documentation
    docs_root = PROJECT_ROOT / "documentation"
    if docs_root.is_dir():
        for pattern in _MD_RAG_FILE_PATTERNS:
            for md_file in sorted(docs_root.rglob(pattern)):
                if not any(part in _MD_EXCLUDE_DIRS for part in md_file.parts):
                    found.append(md_file)

    # 3) High-value top-level docs
    for root_file in _MD_ROOT_FILES:
        path = PROJECT_ROOT / root_file
        if path.is_file():
            found.append(path)

    # 4) Explicit external files (outside PROJECT_ROOT) used for shared context.
    for external_file in _MD_EXTERNAL_FILES:
        path = Path(external_file)
        if path.is_file():
            found.append(path)
    return sorted(set(found))


def get_pg_conn():
    conn = psycopg2.connect(
        host=settings.pg_bi_host,
        port=settings.pg_bi_port,
        dbname=settings.pg_bi_db,
        user=settings.pg_bi_user,
        password=settings.pg_bi_password,
    )
    register_vector(conn)
    return conn


def embed_chunks(
    chunks: list[Chunk], client: LocalEmbedder
) -> list[tuple[Chunk, list[float]]]:
    """Batch-embed chunks using local sentence-transformers model."""
    results: list[tuple[Chunk, list[float]]] = []
    batch_size = 64
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i : i + batch_size]
        texts = [f"{c.title}\n\n{c.content}" for c in batch]
        response = client.embed(texts, input_type="document")
        for chunk, emb in zip(batch, response.embeddings):
            results.append((chunk, emb))
        print(f"  Embedded {min(i + batch_size, len(chunks))}/{len(chunks)} chunks")
    return results


def upsert_chunks(conn, embedded: list[tuple[Chunk, list[float]]]) -> int:
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
        for chunk, emb in embedded:
            cur.execute(
                sql,
                (
                    chunk.doc_id,
                    chunk.source_path,
                    chunk.doc_type,
                    chunk.title,
                    chunk.content,
                    np.array(emb),
                    json.dumps(chunk.metadata),
                ),
            )
    conn.commit()
    return len(embedded)


def collect_doc_chunks() -> list[Chunk]:
    chunks: list[Chunk] = []
    md_files = discover_md_files()
    print(f"  Discovered {len(md_files)} markdown files")
    for doc_path in md_files:
        if not doc_path.exists():
            print(f"  SKIP (not found): {doc_path.name}")
            continue
        text = doc_path.read_text(encoding="utf-8", errors="replace")
        try:
            relative = str(doc_path.relative_to(PROJECT_ROOT))
        except ValueError:
            # Allow indexing explicit external files while keeping source paths stable.
            relative = str(doc_path)
        before = len(chunks)
        if doc_path.name == "current_model_state.md":
            chunks.extend(chunk_model_state(relative, text))
        else:
            chunks.extend(chunk_markdown(relative, text))
        added = len(chunks) - before
        print(f"  {doc_path.name}: {added} chunks")
    return chunks


# Key dimensions whose elements get full attribute values in RAG chunks
_KEY_DIMS_WITH_ATTRS = {
    "account", "entity", "cashflow_activity", "listed_share",
    "month", "version", "contact", "tracking_1", "tracking_2",
}


def collect_tm1_chunks() -> list[Chunk]:
    chunks: list[Chunk] = []
    print("  Connecting to TM1...")
    with TM1Service(**TM1_CONFIG) as tm1:
        dim_names = [
            d
            for d in tm1.dimensions.get_all_names()
            if not d.startswith("}")
        ]
        print(f"  Found {len(dim_names)} user dimensions")
        for dim_name in dim_names:
            try:
                elements = tm1.elements.get_elements(dim_name, dim_name)
                el_list = [
                    {"name": e.name, "element_type": e.element_type.value}
                    for e in elements
                ]
                attrs = [
                    a.name
                    for a in tm1.elements.get_element_attributes(dim_name, dim_name)
                ]

                # For key dimensions, also fetch attribute values and hierarchy
                attr_values = None
                hierarchy_edges = None

                if dim_name in _KEY_DIMS_WITH_ATTRS and attrs:
                    print(f"    Fetching attributes for {dim_name} ({len(el_list)} elements)...")
                    attr_values = {}
                    for el in elements:
                        el_attrs = {}
                        for attr_name in attrs:
                            try:
                                val = tm1.elements.get_attribute_value(
                                    dim_name, dim_name, el.name, attr_name,
                                )
                                if val is not None and str(val).strip():
                                    el_attrs[attr_name] = str(val)
                            except Exception:
                                pass
                        if el_attrs:
                            attr_values[el.name] = el_attrs

                # Always fetch hierarchy edges for richer structure info
                try:
                    hierarchy = tm1.hierarchies.get(dim_name, dim_name)
                    hierarchy_edges = {}
                    for el in hierarchy.elements.values():
                        if el.components:
                            hierarchy_edges[el.name] = list(el.components.keys())
                except Exception:
                    pass

                chunks.append(chunk_tm1_dimension(
                    dim_name, el_list, attrs,
                    attribute_values=attr_values,
                    hierarchy_edges=hierarchy_edges,
                ))
                print(f"  ✓ {dim_name}: {len(el_list)} elements"
                      f"{f', {len(attr_values)} with attrs' if attr_values else ''}"
                      f"{f', {len(hierarchy_edges)} consolidations' if hierarchy_edges else ''}")
            except Exception as e:
                print(f"  WARNING: could not index dimension {dim_name}: {e}")
    print(f"  TM1 dimension chunks: {len(chunks)}")
    return chunks


def _get_financials_conn():
    """Connect to klikk_financials_v4 PostgreSQL database."""
    return psycopg2.connect(
        host=settings.pg_financials_host,
        port=settings.pg_financials_port,
        dbname=settings.pg_financials_db,
        user=settings.pg_financials_user,
        password=settings.pg_financials_password,
    )


def collect_pg_schema_chunks() -> list[Chunk]:
    """Collect PostgreSQL table schema chunks from klikk_financials_v4."""
    chunks: list[Chunk] = []
    print("  Connecting to klikk_financials_v4...")
    try:
        conn = _get_financials_conn()
    except Exception as e:
        print(f"  ERROR: Could not connect to klikk_financials_v4: {e}")
        return chunks

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # Get all tables with row counts (pg_stat_user_tables uses 'relname', not 'tablename')
        cur.execute("""
            SELECT schemaname, relname AS tablename,
                   n_live_tup AS approx_rows
            FROM pg_stat_user_tables
            WHERE schemaname = 'public'
            ORDER BY relname
        """)
        tables = cur.fetchall()
        print(f"  Found {len(tables)} tables")

        for table in tables:
            table_name = table["tablename"]

            # Skip Django internal tables
            if table_name.startswith("django_") or table_name.startswith("auth_"):
                continue

            # Get columns
            cur.execute("""
                SELECT column_name, data_type, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = %s
                ORDER BY ordinal_position
            """, (table_name,))
            columns = [dict(row) for row in cur.fetchall()]

            # Get foreign keys
            cur.execute("""
                SELECT
                    kcu.column_name AS column,
                    ccu.table_name AS references_table,
                    ccu.column_name AS references_column
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                    ON tc.constraint_name = kcu.constraint_name
                    AND tc.table_schema = kcu.table_schema
                JOIN information_schema.constraint_column_usage ccu
                    ON tc.constraint_name = ccu.constraint_name
                    AND tc.table_schema = ccu.table_schema
                WHERE tc.constraint_type = 'FOREIGN KEY'
                    AND tc.table_schema = 'public'
                    AND tc.table_name = %s
            """, (table_name,))
            foreign_keys = [dict(row) for row in cur.fetchall()]

            # Get sample rows (3 rows, safely)
            sample_rows = []
            try:
                cur.execute(
                    f'SELECT * FROM "{table_name}" LIMIT 3'
                )
                for row in cur.fetchall():
                    sample_rows.append(dict(row))
            except Exception:
                conn.rollback()

            chunks.append(chunk_pg_table_schema(
                table_name=table_name,
                database=settings.pg_financials_db,
                columns=columns,
                foreign_keys=foreign_keys,
                row_count=table["approx_rows"] or 0,
                sample_rows=sample_rows,
            ))
            fk_str = f", {len(foreign_keys)} FKs" if foreign_keys else ""
            print(f"  ✓ {table_name}: {len(columns)} cols, ~{table['approx_rows'] or 0} rows{fk_str}")

    conn.close()
    print(f"  PostgreSQL schema chunks: {len(chunks)}")
    return chunks


def collect_data_context_chunks() -> list[Chunk]:
    """Collect hardcoded data context chunks (relationships, pipelines, mappings)."""
    chunks = [
        chunk_share_data_relationships(),
        chunk_gl_data_relationships(),
        chunk_column_dimension_map(),
        chunk_transaction_processing(),
    ]
    print(f"  Data context chunks: {len(chunks)}")
    for c in chunks:
        print(f"  ✓ {c.title}")
    return chunks


def run_element_indexing(dimensions: list[str] | None = None) -> None:
    """
    Per-element vectorization for specified dimensions (or all key dimensions).
    Each element gets its own document with full attribute profile + hierarchy info.
    Uses the element_context skill's index_dimension_elements function.
    """
    from apps.ai_agent.skills.element_context import index_dimension_elements, index_all_key_dimensions

    if dimensions:
        for dim in dimensions:
            print(f"\n  Indexing elements of '{dim}'...")
            result = index_dimension_elements(dim)
            if "error" in result:
                print(f"    ERROR: {result['error']}")
            else:
                print(f"    ✓ {result.get('elements_indexed', 0)} elements indexed, "
                      f"{result.get('attributes_per_element', 0)} attributes each")
    else:
        print("\n  Indexing all key dimensions (account, entity, cashflow_activity, listed_share, month, version)...")
        result = index_all_key_dimensions()
        print(f"  ✓ {result.get('total_elements', 0)} total elements indexed across "
              f"{result.get('dimensions_indexed', 0)} dimensions")
        for dim, detail in result.get("details", {}).items():
            if detail.get("error"):
                print(f"    {dim}: ERROR — {detail['error']}")
            else:
                print(f"    {dim}: {detail.get('elements_indexed', 0)} elements")


def main() -> None:
    parser = argparse.ArgumentParser(description="Index documents into pgvector RAG store")
    parser.add_argument("--full", action="store_true", help="Re-index everything (docs + TM1 dims + elements)")
    parser.add_argument("--docs-only", action="store_true", help="Only index documentation files")
    parser.add_argument("--tm1-only", action="store_true", help="Only index TM1 dimension-level metadata")
    parser.add_argument("--schema", action="store_true",
                        help="Index PostgreSQL table schemas and data context (relationships, pipelines)")
    parser.add_argument("--elements", action="store_true",
                        help="Index per-element profiles for key dimensions (account, entity, etc.)")
    parser.add_argument("--element-dims", nargs="*", metavar="DIM",
                        help="Index per-element profiles for specific dimension(s)")
    parser.add_argument("--pg-host", metavar="HOST",
                        help="Override PostgreSQL host (useful when running outside Docker, e.g. 'localhost')")
    args = parser.parse_args()

    # Override PG host if provided (bypasses host-gateway when running from CLI on host machine)
    if args.pg_host:
        import apps.ai_agent.agent.config as _cfg
        _orig_getattr = _cfg.settings.__class__.__getattr__
        def _patched_getattr(self, name):
            if name in ('pg_bi_host', 'pg_financials_host'):
                return args.pg_host
            return _orig_getattr(self, name)
        _cfg.settings.__class__.__getattr__ = _patched_getattr

    # Per-element indexing mode
    if args.elements or args.element_dims:
        print("\n=== Per-Element Vectorization ===")
        run_element_indexing(args.element_dims or None)
        if not args.full:
            return

    embedder = LocalEmbedder()
    conn = get_pg_conn()

    chunks: list[Chunk] = []

    if not args.tm1_only and not args.schema:
        print("\nCollecting documentation chunks...")
        chunks.extend(collect_doc_chunks())

    if not args.docs_only and not args.schema:
        print("\nCollecting TM1 metadata chunks...")
        chunks.extend(collect_tm1_chunks())

    if args.schema or args.full:
        print("\nCollecting PostgreSQL schema chunks...")
        chunks.extend(collect_pg_schema_chunks())
        print("\nCollecting data context chunks...")
        chunks.extend(collect_data_context_chunks())

    if not chunks:
        print("No chunks collected. Exiting.")
        return

    print(f"\nTotal chunks to embed: {len(chunks)}")
    print("Embedding via local sentence-transformers...")
    embedded = embed_chunks(chunks, embedder)

    print("\nUpserting into pgvector...")
    count = upsert_chunks(conn, embedded)
    conn.close()
    print(f"\nDone. {count} chunks indexed into {settings.rag_schema}.documents")

    # If --full, also run per-element indexing
    if args.full:
        print("\n=== Per-Element Vectorization (key dimensions) ===")
        run_element_indexing(None)


if __name__ == "__main__":
    main()
