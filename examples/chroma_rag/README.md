# Chroma RAG Retrieval Example

Demonstrates offline vector retrieval with an embedded, persistent ChromaDB
collection. The pipeline enriches each question with relevant reference
material; it does not call an LLM or use OpenRouter or other API credentials.

## Prerequisite

ChromaDB is included in ELSPETH's base dependencies. Use the already-prepared
environment, or install the locked base dependencies from the main checkout:

```bash
uv sync --frozen
```

Worktrees share the main checkout's `.venv`; use the already-prepared
environment there rather than running the install command from a worktree.

## Run

From the repository root:

```bash
./examples/chroma_rag/run.sh
```

The launcher clears prior artifacts, seeds 10 science and health documents
into `examples/chroma_rag/chroma_data/`, and runs eight query rows. Each output
row is expected to contain the three highest-scoring results and numbered
retrieval context.

## Outputs

- Results: `examples/chroma_rag/output/results.jsonl` (8 rows)
- Quarantined rows, if any: `examples/chroma_rag/output/quarantined.jsonl`
- Audit trail: `examples/chroma_rag/runs/audit.db`
- Embedded Chroma data: `examples/chroma_rag/chroma_data/`
