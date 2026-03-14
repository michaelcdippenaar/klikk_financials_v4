"""
Skill: Google Drive Integration
Access business documents from Google Drive — read, list, and index into RAG.

Setup:
1. Create a Google Cloud project and enable the Drive API
2. Create a service account and download the JSON key file
3. Share your Google Drive folder with the service account email
4. Set GOOGLE_DRIVE_CREDENTIALS_PATH, GOOGLE_DRIVE_FOLDER_IDS, GOOGLE_DRIVE_ENABLED in .env
"""
from __future__ import annotations

import io
import json
import sys
import os
import time
from typing import Any
from pathlib import Path

from apps.ai_agent.agent.config import settings


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def _get_drive_service():
    """Build and return a Google Drive API v3 service object."""
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    creds_path = settings.google_drive_credentials_path
    if not creds_path or not Path(creds_path).exists():
        raise FileNotFoundError(
            f"Google Drive credentials file not found at '{creds_path}'. "
            "Set GOOGLE_DRIVE_CREDENTIALS_PATH in .env"
        )

    creds = Credentials.from_service_account_file(
        creds_path,
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    return build("drive", "v3", credentials=creds)


def _extract_text_from_bytes(content_bytes: bytes, mime_type: str, file_name: str) -> str:
    """Extract plain text from file bytes based on MIME type."""
    if mime_type == "application/pdf" or file_name.lower().endswith(".pdf"):
        try:
            from PyPDF2 import PdfReader
            reader = PdfReader(io.BytesIO(content_bytes))
            pages = [page.extract_text() or "" for page in reader.pages]
            return "\n\n".join(pages)
        except Exception as e:
            return f"[PDF extraction error: {e}]"

    elif mime_type in (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ) or file_name.lower().endswith(".docx"):
        try:
            from docx import Document
            doc = Document(io.BytesIO(content_bytes))
            return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())
        except Exception as e:
            return f"[DOCX extraction error: {e}]"

    elif mime_type in (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ) or file_name.lower().endswith(".xlsx"):
        try:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(content_bytes), read_only=True)
            lines = []
            for ws in wb.worksheets:
                lines.append(f"Sheet: {ws.title}")
                for row in ws.iter_rows(max_row=200, values_only=True):
                    vals = [str(v) if v is not None else "" for v in row]
                    if any(vals):
                        lines.append("\t".join(vals))
            return "\n".join(lines)
        except Exception as e:
            return f"[XLSX extraction error: {e}]"

    elif mime_type == "text/csv" or file_name.lower().endswith(".csv"):
        return content_bytes.decode("utf-8", errors="replace")

    elif mime_type.startswith("text/") or file_name.lower().endswith((".txt", ".md")):
        return content_bytes.decode("utf-8", errors="replace")

    else:
        return f"[Unsupported file type: {mime_type}]"


def _get_default_folder_ids() -> list[str]:
    """Parse comma-separated folder IDs from settings."""
    raw = settings.google_drive_folder_ids
    if not raw:
        return []
    return [fid.strip() for fid in raw.split(",") if fid.strip()]


# ---------------------------------------------------------------------------
#  Tool functions
# ---------------------------------------------------------------------------

def gdrive_list_files(
    folder_id: str = "",
    file_type: str = "all",
) -> dict[str, Any]:
    """
    List files in a Google Drive folder.

    folder_id: Google Drive folder ID. Leave empty to use the default from settings.
    file_type: 'all', 'doc', 'sheet', 'pdf', 'presentation'
    """
    if not settings.google_drive_enabled:
        return {"error": "Google Drive is disabled. Set GOOGLE_DRIVE_ENABLED=true in .env"}

    try:
        service = _get_drive_service()

        folder_ids = [folder_id] if folder_id else _get_default_folder_ids()
        if not folder_ids:
            return {"error": "No folder_id provided and GOOGLE_DRIVE_FOLDER_IDS not set in .env"}

        # Build MIME type filter
        mime_filter = ""
        if file_type == "doc":
            mime_filter = " and (mimeType='application/vnd.google-apps.document' or mimeType='application/vnd.openxmlformats-officedocument.wordprocessingml.document')"
        elif file_type == "sheet":
            mime_filter = " and (mimeType='application/vnd.google-apps.spreadsheet' or mimeType='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')"
        elif file_type == "pdf":
            mime_filter = " and mimeType='application/pdf'"
        elif file_type == "presentation":
            mime_filter = " and mimeType='application/vnd.google-apps.presentation'"

        all_files = []
        for fid in folder_ids:
            query = f"'{fid}' in parents and trashed=false{mime_filter}"
            results = service.files().list(
                q=query,
                fields="files(id, name, mimeType, modifiedTime, size)",
                pageSize=100,
                orderBy="modifiedTime desc",
            ).execute()
            all_files.extend(results.get("files", []))

        return {
            "files": [
                {
                    "id": f["id"],
                    "name": f["name"],
                    "mime_type": f["mimeType"],
                    "modified": f.get("modifiedTime", ""),
                    "size": f.get("size", "unknown"),
                }
                for f in all_files
            ],
            "count": len(all_files),
            "folder_ids": folder_ids,
        }
    except FileNotFoundError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Google Drive error: {e}"}


def gdrive_read_document(file_id: str) -> dict[str, Any]:
    """
    Read and extract text content from a Google Drive file.
    Supports: Google Docs, Sheets, PDFs, DOCX, XLSX, TXT, MD, CSV.

    file_id: The Google Drive file ID (from gdrive_list_files results)
    """
    if not settings.google_drive_enabled:
        return {"error": "Google Drive is disabled."}

    try:
        service = _get_drive_service()

        # Get file metadata
        file_meta = service.files().get(
            fileId=file_id,
            fields="id,name,mimeType,size",
        ).execute()

        name = file_meta["name"]
        mime = file_meta["mimeType"]

        # Google native formats need export
        if mime == "application/vnd.google-apps.document":
            content = service.files().export(
                fileId=file_id, mimeType="text/plain"
            ).execute()
            text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else str(content)

        elif mime == "application/vnd.google-apps.spreadsheet":
            content = service.files().export(
                fileId=file_id, mimeType="text/csv"
            ).execute()
            text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else str(content)

        elif mime == "application/vnd.google-apps.presentation":
            content = service.files().export(
                fileId=file_id, mimeType="text/plain"
            ).execute()
            text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else str(content)

        else:
            # Binary files — download and extract
            content = service.files().get_media(fileId=file_id).execute()
            text = _extract_text_from_bytes(content, mime, name)

        # Truncate very large documents
        max_chars = 50000
        truncated = len(text) > max_chars
        text = text[:max_chars]

        return {
            "file_id": file_id,
            "name": name,
            "mime_type": mime,
            "content": text,
            "char_count": len(text),
            "truncated": truncated,
        }
    except FileNotFoundError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Failed to read document: {e}"}


def gdrive_index_folder(folder_id: str = "") -> dict[str, Any]:
    """
    Index all documents from a Google Drive folder into the RAG vector store.
    Downloads each file, extracts text, chunks it, embeds via VoyageAI,
    and stores in pgvector for future retrieval.

    folder_id: Google Drive folder ID. Leave empty to use defaults from settings.
    """
    if not settings.google_drive_enabled:
        return {"error": "Google Drive is disabled."}

    try:
        import numpy as np
        import psycopg2
        from pgvector.psycopg2 import register_vector
        from apps.ai_agent.rag.embedder import LocalEmbedder

        # List files
        files_result = gdrive_list_files(folder_id)
        if "error" in files_result:
            return files_result

        files = files_result["files"]
        if not files:
            return {"message": "No files found in the folder.", "files_indexed": 0}

        # Connect to pgvector
        conn = psycopg2.connect(
            host=settings.pg_bi_host, port=settings.pg_bi_port,
            dbname=settings.pg_bi_db, user=settings.pg_bi_user,
            password=settings.pg_bi_password,
        )
        register_vector(conn)
        embedder = LocalEmbedder()

        indexed = 0
        errors = []
        total_chunks = 0

        for file_info in files:
            try:
                # Read the document
                doc_result = gdrive_read_document(file_info["id"])
                if "error" in doc_result:
                    errors.append({"file": file_info["name"], "error": doc_result["error"]})
                    continue

                text = doc_result["content"]
                if not text.strip() or text.startswith("[Unsupported"):
                    continue

                # Chunk the text
                from apps.ai_agent.rag.chunker import chunk_markdown, chunk_plain_text
                source_path = f"gdrive::{file_info['name']}"
                if file_info["name"].lower().endswith(".md"):
                    chunks = list(chunk_markdown(source_path, text))
                else:
                    chunks = list(chunk_plain_text(source_path, text))

                if not chunks:
                    continue

                # Override doc_type to google_drive
                for chunk in chunks:
                    chunk.doc_type = "google_drive"
                    chunk.metadata["gdrive_file_id"] = file_info["id"]
                    chunk.metadata["gdrive_file_name"] = file_info["name"]

                # Embed in batches
                for i in range(0, len(chunks), 128):
                    batch = chunks[i:i + 128]
                    texts = [f"{c.title}\n\n{c.content}" for c in batch]
                    resp = embedder.embed(texts, input_type="document")
                    for chunk, emb in zip(batch, resp.embeddings):
                        sql = f"""
                            INSERT INTO {settings.rag_schema}.documents
                                (doc_id, source_path, doc_type, title, content, embedding, metadata)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (doc_id) DO UPDATE SET
                                content = EXCLUDED.content,
                                embedding = EXCLUDED.embedding,
                                metadata = EXCLUDED.metadata,
                                indexed_at = NOW()
                        """
                        with conn.cursor() as cur:
                            cur.execute(sql, (
                                chunk.doc_id, chunk.source_path, chunk.doc_type,
                                chunk.title, chunk.content,
                                np.array(emb), json.dumps(chunk.metadata),
                            ))
                    conn.commit()

                total_chunks += len(chunks)
                indexed += 1

            except Exception as e:
                errors.append({"file": file_info["name"], "error": str(e)})

        conn.close()

        return {
            "status": "complete",
            "files_scanned": len(files),
            "files_indexed": indexed,
            "total_chunks": total_chunks,
            "errors": errors if errors else None,
        }
    except Exception as e:
        return {"error": f"Indexing failed: {e}"}


# ---------------------------------------------------------------------------
#  Tool schemas (conditional on google_drive_enabled)
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: list[dict] = []
TOOL_FUNCTIONS: dict[str, Any] = {}

if settings.google_drive_enabled:
    TOOL_SCHEMAS = [
        {
            "name": "gdrive_list_files",
            "description": (
                "List files in a Google Drive folder. "
                "Returns file names, IDs, types, and modification dates. "
                "Use to discover what business documents are available."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "folder_id": {
                        "type": "string",
                        "description": "Google Drive folder ID. Leave empty for default folder.",
                    },
                    "file_type": {
                        "type": "string",
                        "description": "'all', 'doc', 'sheet', 'pdf', 'presentation'",
                    },
                },
            },
        },
        {
            "name": "gdrive_read_document",
            "description": (
                "Read and extract text content from a Google Drive file. "
                "Supports Google Docs, Sheets, PDFs, DOCX, XLSX, TXT, MD, CSV."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "file_id": {
                        "type": "string",
                        "description": "Google Drive file ID (from gdrive_list_files)",
                    },
                },
                "required": ["file_id"],
            },
        },
        {
            "name": "gdrive_index_folder",
            "description": (
                "Index all documents from a Google Drive folder into the RAG vector store. "
                "Downloads, extracts text, chunks, embeds via VoyageAI, stores in pgvector. "
                "Run this once to make Google Drive content searchable via RAG."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "folder_id": {
                        "type": "string",
                        "description": "Folder ID. Leave empty for default.",
                    },
                },
            },
        },
    ]

    TOOL_FUNCTIONS = {
        "gdrive_list_files": gdrive_list_files,
        "gdrive_read_document": gdrive_read_document,
        "gdrive_index_folder": gdrive_index_folder,
    }
