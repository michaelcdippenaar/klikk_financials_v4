# RAG Server

This folder documents the RAG (Retrieval-Augmented Generation) setup used in this project.

## What RAG exists here

- **In-app RAG** – The Django `ai_agent` app provides its own RAG: knowledge corpora, system documents, chunking, and semantic search using OpenAI embeddings. See [In-App-RAG.md](In-App-RAG.md) for connection settings, what it is used for, and what it contains.

- **External RAG server** – Optional. If you add an external RAG server (e.g. pgEdge RAG Server, Ragie), document its connection and contents in [External-RAG-Server.md](External-RAG-Server.md).

## Reference docs (Klikk AI Portal RAG)

The **Klikk AI Portal** backend runs its own RAG indexer (`backend/rag/indexer.py`) that indexes markdown under `instructions`, `applications`, and **`documentation`**. Reference documents included there (and retrievable by the portal agent) include:

| Doc | Description |
|-----|-------------|
| **PAW embedding** | `documentation/PAW_EMBED.md` in the portal repo — iframe + postMessage, CSP, CORS, sync pattern, and distinction from Cognos “Embedding visualization code”. Indexed when running `python rag/indexer.py --docs-only` (or `--full`). |

## Doc index

| File | Description |
|------|-------------|
| [In-App-RAG.md](In-App-RAG.md) | Settings to connect, use cases, and data model for the in-app ai_agent RAG |
| [External-RAG-Server.md](External-RAG-Server.md) | Placeholder for external RAG server connection and contents |
