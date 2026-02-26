from __future__ import annotations

import hashlib
import math
import time
import os
from dataclasses import dataclass
from typing import Any

import requests
from django.conf import settings
from django.db import transaction

from apps.ai_agent.models import KnowledgeChunkEmbedding, KnowledgeCorpus, SystemDocument


DEFAULT_EMBEDDING_MODEL = 'text-embedding-ada-002'
FALLBACK_EMBEDDING_MODELS = [
    'text-embedding-3-small',
    'text-embedding-3-large',
    'text-embedding-ada-002',
]


def _resolve_openai_key():
    key = getattr(settings, 'AI_AGENT_OPENAI_API_KEY', None)
    if key:
        return key
    return os.environ.get('AI_AGENT_OPENAI_API_KEY')


def _sha256(text: str) -> str:
    return hashlib.sha256((text or '').encode('utf-8')).hexdigest()


def chunk_text(text: str, *, chunk_size: int = 1200, overlap: int = 150) -> list[str]:
    """
    Simple char-based chunker with overlap.
    """
    t = (text or '').strip()
    if not t:
        return []
    if chunk_size < 200:
        chunk_size = 200
    if overlap < 0:
        overlap = 0
    if overlap >= chunk_size:
        overlap = max(0, chunk_size // 4)

    out = []
    i = 0
    n = len(t)
    while i < n:
        j = min(n, i + chunk_size)
        out.append(t[i:j])
        if j >= n:
            break
        i = max(0, j - overlap)
    return out


def embed_texts_openai(
    texts: list[str],
    *,
    model: str | None = None,
    timeout: int = 60,
    fallback_models: list[str] | None = None,
) -> tuple[str, list[list[float]]]:
    """
    Uses OpenAI embeddings API (no openai SDK dependency).
    """
    key = _resolve_openai_key()
    if not key:
        raise RuntimeError('AI_AGENT_OPENAI_API_KEY not configured.')

    primary = model or getattr(settings, 'AI_AGENT_EMBEDDING_MODEL', DEFAULT_EMBEDDING_MODEL)
    tried = []
    models_to_try = [primary]
    for m in (fallback_models or FALLBACK_EMBEDDING_MODELS):
        if m not in models_to_try:
            models_to_try.append(m)

    last_error = None
    for m in models_to_try:
        tried.append(m)
        resp = requests.post(
            'https://api.openai.com/v1/embeddings',
            headers={
                'Authorization': f'Bearer {key}',
                'Content-Type': 'application/json',
            },
            json={
                'model': m,
                'input': texts,
            },
            timeout=timeout,
        )
        if not resp.ok:
            last_error = f'OpenAI embeddings error ({resp.status_code}) for model {m}: {resp.text[:2000]}'
            # Try next model on "model not found"/access issues; otherwise bail early.
            if resp.status_code in (400, 401, 403, 404):
                continue
            raise RuntimeError(last_error)

        data = resp.json()
        items = data.get('data') or []
        vectors = []
        for it in items:
            vec = (it or {}).get('embedding')
            if not isinstance(vec, list):
                vec = []
            vectors.append([float(x) for x in vec])
        return m, vectors

    raise RuntimeError((last_error or 'OpenAI embeddings request failed.') + f' Tried: {tried}')


@dataclass(frozen=True)
class VectorizeResult:
    corpus_id: int
    embedding_model: str
    documents_seen: int
    chunks_written: int
    chunks_deleted: int


def vectorize_corpus_documents(
    *,
    corpus: KnowledgeCorpus,
    project_id: int | None = None,
    embedding_model: str | None = None,
    chunk_size: int = 1200,
    overlap: int = 150,
    force: bool = False,
) -> VectorizeResult:
    embedding_model = embedding_model or getattr(settings, 'AI_AGENT_EMBEDDING_MODEL', DEFAULT_EMBEDDING_MODEL)

    qs = SystemDocument.objects.filter(is_active=True, corpus=corpus).select_related('project')
    if project_id is not None:
        qs = qs.filter(project_id=project_id)

    docs = list(qs.order_by('-updated_at', '-id')[:2000])
    chunks_written = 0
    chunks_deleted = 0

    for doc in docs:
        text = (doc.content_markdown or '').strip()
        if not text:
            continue
        source_hash = _sha256(text)
        existing = list(
            KnowledgeChunkEmbedding.objects.filter(
                system_document=doc,
                embedding_model=embedding_model,
            ).order_by('chunk_index')
        )
        if existing and (not force) and all(e.source_hash == source_hash for e in existing):
            continue

        chunks = chunk_text(text, chunk_size=chunk_size, overlap=overlap)
        if not chunks:
            continue

        used_model, vectors = embed_texts_openai(chunks, model=embedding_model)
        if len(vectors) != len(chunks):
            raise RuntimeError('Embedding count mismatch.')

        with transaction.atomic():
            # Delete old chunks for this doc/model to keep it consistent.
            deleted, _ = KnowledgeChunkEmbedding.objects.filter(
                system_document=doc,
                embedding_model=used_model,
            ).delete()
            chunks_deleted += int(deleted)

            rows = []
            for idx, (chunk, vec) in enumerate(zip(chunks, vectors)):
                rows.append(KnowledgeChunkEmbedding(
                    corpus=corpus,
                    project=doc.project,
                    system_document=doc,
                    embedding_model=used_model,
                    source_hash=source_hash,
                    chunk_index=idx,
                    chunk_text=chunk,
                    embedding=vec,
                ))
            KnowledgeChunkEmbedding.objects.bulk_create(rows, batch_size=200)
            chunks_written += len(rows)

    return VectorizeResult(
        corpus_id=corpus.id,
        embedding_model=embedding_model or getattr(settings, 'AI_AGENT_EMBEDDING_MODEL', DEFAULT_EMBEDDING_MODEL),
        documents_seen=len(docs),
        chunks_written=chunks_written,
        chunks_deleted=chunks_deleted,
    )


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return -1.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return -1.0
    return dot / math.sqrt(na * nb)


def semantic_search_chunks(
    *,
    corpus: KnowledgeCorpus,
    query: str,
    project_id: int | None = None,
    embedding_model: str | None = None,
    top_k: int = 6,
) -> list[dict[str, Any]]:
    embedding_model = embedding_model or getattr(settings, 'AI_AGENT_EMBEDDING_MODEL', DEFAULT_EMBEDDING_MODEL)
    used_model, vecs = embed_texts_openai([query], model=embedding_model)
    qvec = vecs[0]

    qs = KnowledgeChunkEmbedding.objects.filter(corpus=corpus, embedding_model=used_model).select_related('system_document')
    if project_id is not None:
        qs = qs.filter(project_id=project_id)

    # JSON embeddings means we must score in Python; keep it bounded.
    candidates = list(qs.order_by('-embedded_at')[:2000])
    scored = []
    for c in candidates:
        sim = cosine_similarity(qvec, c.embedding or [])
        scored.append((sim, c))
    scored.sort(key=lambda t: t[0], reverse=True)

    out = []
    for sim, c in scored[: max(1, min(int(top_k), 20))]:
        out.append({
            'similarity': sim,
            'doc_id': c.system_document_id,
            'doc_slug': c.system_document.slug,
            'doc_title': c.system_document.title,
            'chunk_index': c.chunk_index,
            'chunk_text': c.chunk_text,
        })
    return out

