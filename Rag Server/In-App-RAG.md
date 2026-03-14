# In-App RAG (Django ai_agent)

The RAG used by this project runs inside the Django app `apps.ai_agent`. It vectorizes system documents, stores embeddings in PostgreSQL, and performs semantic search to augment the AI agent with relevant context.

---

## Settings to connect

All of these are read from the environment or Django settings (e.g. `development.py`). Set them in your shell or `.env` so the app can call the embeddings API and use the correct model.

| Setting | Description | Example |
|--------|-------------|--------|
| **`AI_AGENT_OPENAI_API_KEY`** | API key for the embeddings (and chat) API. Required for vectorization and search. | Set in env or `development.py` |
| **`AI_AGENT_EMBEDDING_MODEL`** | Embedding model name. Used for both indexing and query embedding. | `text-embedding-3-small` (default) |

**Embedding endpoint:** The app currently calls a **fixed** URL: `https://api.openai.com/v1/embeddings`. There is no configurable base URL yet. To use a local or alternate embedding server, the code would need to support an optional setting (e.g. `AI_AGENT_EMBEDDING_BASE_URL`).

**Where defined:** `klikk_business_intelligence/settings/development.py` (and equivalent in other env modules). The vector store reads them via `django.conf.settings` and `os.environ` in `apps/ai_agent/services/vector_store.py`.

---

## What it is used for

- **Knowledge corpus vectorization** – System documents (markdown) are chunked and embedded; vectors are stored in `KnowledgeChunkEmbedding`. Used when you run “vectorize” for a corpus (e.g. via GlossaryRefreshView or KnowledgeCorpusVectorizeView).
- **Semantic search for the AI agent** – At query time, the user question is embedded and compared to stored chunks (cosine similarity). Top-k chunks are returned as context for the LLM. Used by `semantic_search_chunks()` and by the agent run (e.g. AgentSessionRunWithToolsView) for RAG.
- **Glossary / Xero-derived docs** – Account and contact glossary documents can be refreshed after Xero metadata changes; when vectorization runs, those documents are included in the corpus and searchable.

**Views / flows that use this RAG:** GlossaryRefreshView, KnowledgeCorpusVectorizeView, KnowledgeCorpusSearchView, and the agent session run that uses tools (RAG retrieval).

---

## What it contains

**Data model (PostgreSQL, no pgvector):**

- **`KnowledgeCorpus`** – Named collection (slug, name, description). Groups system documents and their chunks.
- **`SystemDocument`** – A single document: belongs to a corpus and optionally an `AgentProject`; has `content_markdown`, `slug`, `title`, and can be pinned / ordered for context.
- **`KnowledgeChunkEmbedding`** – One row per chunk of a system document:
  - Links: `corpus`, `project`, `system_document`
  - `embedding_model` – e.g. `text-embedding-3-small`
  - `source_hash` – hash of source text for change detection
  - `chunk_index`, `chunk_text`, `embedding` (JSON list of floats)
  - `embedded_at`

**Chunking:** Done in `vector_store.chunk_text()`: character-based, default `chunk_size=1200`, `overlap=150`. Same chunking is used for vectorization and for keeping stored chunks in sync when documents are re-vectorized.

**Storage:** Embeddings are stored as JSON (list of floats) in the `embedding` field of `KnowledgeChunkEmbedding`. The database is the same PostgreSQL instance used by the rest of the app; there is no separate vector DB or pgvector extension required.
