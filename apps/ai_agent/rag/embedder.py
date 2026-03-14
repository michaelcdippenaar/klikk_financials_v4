"""
Local embedding backend using sentence-transformers (free, no API key).

Model: all-MiniLM-L6-v2  — 384-dim, fast, good quality for semantic search.
The model is downloaded once (~80 MB) to ~/.cache/huggingface/ on first use.
"""
from __future__ import annotations

import threading
from typing import Any

_model = None
_model_lock = threading.Lock()

MODEL_NAME = "all-MiniLM-L6-v2"
EMBEDDING_DIM = 384


def _get_model():
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                from sentence_transformers import SentenceTransformer
                _model = SentenceTransformer(MODEL_NAME)
    return _model


def embed_texts(texts: list[str], input_type: str = "document") -> list[list[float]]:
    """Embed a list of texts, returns list of 384-dim float vectors."""
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
