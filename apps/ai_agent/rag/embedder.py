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

import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)

_model = None
_model_lock = threading.Lock()

MODEL_NAME = "BAAI/bge-m3"
EMBEDDING_DIM = 1024

# Batch size for encode(). bge-m3 is ~2.3 GB of weights; on an 8 GB GPU (e.g. the
# staging RTX A1000) a batch of 16 leaves comfortable headroom for activations.
# Override via env when running on a larger/smaller device; we also back off
# automatically on a CUDA OOM (see embed_texts).
#   - CUDA default: 16  (safe on 8 GB; raise to 32 on >=16 GB)
#   - CPU default:  32  (no VRAM ceiling; CPU is throughput-bound not memory-bound)
_DEFAULT_BATCH_CUDA = 16
_DEFAULT_BATCH_CPU = 32

# Device actually selected for the loaded model. Set in _get_model(); read by
# embed_texts() to pick the default batch size and to drive OOM back-off.
_device: str | None = None


def _resolve_device() -> str:
    """Pick the inference device.

    Order of preference: explicit RAG_EMBED_DEVICE env override -> CUDA (NVIDIA
    GPU, e.g. the staging A1000) -> Apple MPS -> CPU. Importing torch here keeps
    the module importable on hosts without torch installed (the retriever still
    degrades to "" — see retriever.py).
    """
    override = os.environ.get("RAG_EMBED_DEVICE", "").strip().lower()
    if override:
        return override
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
        if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


def _default_batch_size() -> int:
    env = os.environ.get("RAG_EMBED_BATCH_SIZE", "").strip()
    if env.isdigit() and int(env) > 0:
        return int(env)
    return _DEFAULT_BATCH_CUDA if _device == "cuda" else _DEFAULT_BATCH_CPU


def _get_model():
    global _model, _device
    if _model is None:
        with _model_lock:
            if _model is None:
                from sentence_transformers import SentenceTransformer

                _device = _resolve_device()
                # First call downloads ~2.3 GB to the HF cache; subsequent calls
                # reuse the cached weights. Loading is done once per process.
                _model = SentenceTransformer(MODEL_NAME, device=_device)
                # Log the device the model actually landed on (sentence-transformers
                # may fall back internally if the requested device is unusable).
                actual = str(getattr(_model, "device", _device))
                logger.info(
                    "Loaded embedding model %s on device=%s (requested=%s)",
                    MODEL_NAME, actual, _device,
                )
                if _device == "cuda" and not actual.startswith("cuda"):
                    logger.warning(
                        "Requested CUDA but model loaded on %s — GPU not in use. "
                        "Check torch CUDA build / driver on this host.", actual,
                    )
    return _model


def embed_texts(texts: list[str], input_type: str = "document") -> list[list[float]]:
    """Embed a list of texts, returns list of 1024-dim normalised float vectors.

    bge-m3 needs no instruction prefix, so `input_type` is accepted for API
    compatibility with the previous embedder but does not alter the encoding.

    Vectors are L2-normalised (normalize_embeddings=True) so pgvector cosine
    distance (`<=>`) is the correct metric — do not change this without also
    revisiting the retriever's distance operator and the HNSW index opclass.

    On CUDA, an out-of-memory error halves the batch size and retries (down to
    1) rather than crashing the whole reindex.
    """
    model = _get_model()
    batch_size = _default_batch_size()
    while True:
        try:
            vectors = model.encode(
                texts,
                batch_size=batch_size,
                show_progress_bar=False,
                normalize_embeddings=True,
            )
            return [v.tolist() for v in vectors]
        except RuntimeError as exc:
            is_oom = "out of memory" in str(exc).lower()
            if _device == "cuda" and is_oom and batch_size > 1:
                try:
                    import torch

                    torch.cuda.empty_cache()
                except Exception:
                    pass
                new_batch = max(1, batch_size // 2)
                logger.warning(
                    "CUDA OOM at batch_size=%d — backing off to %d and retrying.",
                    batch_size, new_batch,
                )
                batch_size = new_batch
                continue
            raise


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
