"""
Local embedding backend using sentence-transformers (free, no API key).

Model: BAAI/bge-m3 — 1024-dim, multilingual, strong retrieval quality.
- Works WITHOUT instruction prefixes (no query/document prompt templates needed).
- Vectors are L2-normalised, so pgvector cosine distance (`<=>`) is the correct metric.
- The model is downloaded once (~2.3 GB) to ~/.cache/huggingface/ on first use.
  First load is therefore slow; the lazy `_get_model()` lock below caches the
  loaded model for the process lifetime so subsequent calls are fast.

Previous model: all-MiniLM-L6-v2 (384-dim). Upgrading to bge-m3 changes the
embedding dimension 384 -> 1024, which requires the pgvector `documents.embedding`
column to be vector(1024). See migration script:
  apps/ai_agent/rag/sql/0001_documents_bge_m3_1024.sql
"""
from __future__ import annotations

import threading
from typing import Any

_model = None
_model_lock = threading.Lock()

MODEL_NAME = "BAAI/bge-m3"
EMBEDDING_DIM = 1024


def _get_model():
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                from sentence_transformers import SentenceTransformer
                # First call downloads ~2.3 GB to the HF cache; subsequent calls
                # reuse the cached weights. Loading is done once per process.
                _model = SentenceTransformer(MODEL_NAME)
    return _model


def embed_texts(texts: list[str], input_type: str = "document") -> list[list[float]]:
    """Embed a list of texts, returns list of 1024-dim normalised float vectors.

    bge-m3 needs no instruction prefix, so `input_type` is accepted for API
    compatibility with the previous embedder but does not alter the encoding.
    """
    model = _get_model()
    vectors = model.encode(texts, batch_size=64, show_progress_bar=False, normalize_embeddings=True)
    return [v.tolist() for v in vectors]


def embed_one(text: str, input_type: str = "query") -> list[float]:
    """Embed a single text."""
    return embed_texts([text], input_type=input_type)[0]


class _CompatResult:
    def __init__(self, embeddings: list[list[float]]):
        self.embeddings = embeddings


class LocalEmbedder:
    """Embedder client for indexer/element_context — returns _CompatResult with .embeddings list."""

    def embed(self, texts: list[str], model: str = "", input_type: str = "document") -> Any:
        return _CompatResult(embed_texts(texts, input_type=input_type))
