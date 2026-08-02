# Examples — Agent Guide

Instructions for running the examples in this directory.

## Prerequisites

```bash
source .venv/bin/activate
uv pip install -e ".[all]"
```

Ensure `errorworks` is installed with its entry points (`chaosllm`, `chaosweb` are standalone commands, NOT `elspeth` subcommands).

## Example Categories

### Standalone (no external services)

These run immediately with no setup:

| Example | Rows | Notes |
|---------|------|-------|
| `audit_export` | 8 | Demonstrates audit data export |
| `batch_aggregation` | 15 | Batch accumulation and trigger |
| `report_assemble` | 5 (3 reports) | Paginated report aggregation with count and end-of-source flushes |
| `statistical_batch_plugins` | 8 each | Statistical batch plugin examples; run one `settings_*.yaml` file at a time |
| `boolean_routing` | 10 | True/false gate routing |
| `checkpoint_resume` | 20 | Checkpoint/resume on interruption |
| `database_sink` | 8 (4 to DB) | SQLite database output — durable exactly-once sink; run via `./examples/database_sink/run.sh` (seeds the operator-owned target + `_elspeth_*` effect ledger first; a bare `elspeth run` fails preflight by design) |
| `deaggregation` | 6 | Expanding aggregated rows (6→11 output) |
| `deep_routing` | 20 | Multi-level cascading gates; fixture ends PARTIAL/exit 1 with 2 blocked rows quarantined by design |
| `error_routing` | 17 | Error-triggered routing; fixture ends PARTIAL/exit 1 with 4 blocked rows quarantined by design |
| `explicit_routing` | 10 | Named route destinations |
| `fork_coalesce` | 5 | Parallel path fork/join DAG pattern |
| `row_union_ab_experiment` | 8 (16 unioned, 1 comparison row) | Fork-based A/B: `row_union` releases both variant branches as one correlated group; run one `settings*.yaml` at a time. `settings_screened.yaml` ends PARTIAL **by design** (3 tickets screened out, their orphaned siblings fail closed) |
| `json_explode` | 3 | JSON source with array expansion (3→6 output) |
| `transform_pipeline` | 5 | Type coercion followed by dependent derived-field calculations |
| `landscape_journal` | 2 | JSON source, audit journal |
| `multi_flow` | 4 | Two independent named source flows in one run |
| `multi_source_queue` | 3 | Multiple named sources fan into a queue |
| `large_scale_test` | 10,000 | Performance test — committed `input.csv` is 10k rows (observed ~4 min locally); regenerate larger via `generate_data.py` (default 50k) |
| `retention_purge` | 5 | Payload retention policy demo |
| `blob_transforms` | 200 offline expansion rows | Run `./examples/blob_transforms/run.sh`; it packages local fixtures into the payload store before executing. The hosted HTML fetch is opt-in via `./examples/blob_transforms/run_hosted_fetch.sh`. |
| `schema_contracts_demo` | 5 | Schema validation contracts |
| `threshold_gate` | 8 | Numeric threshold routing |

Run pattern:
```bash
elspeth run --settings examples/<name>/settings.yaml --execute
```

### Container-only

| Example | Notes |
|---------|-------|
| `threshold_gate_container` | Uses `/app/pipeline/` paths — Docker only |

### ChaosLLM (mock LLM server required)

These need a ChaosLLM server running. Start it BEFORE the pipeline:

```bash
# Local configs contain a fake api_key, which ELSPETH still fingerprints before
# writing the audit-safe config. This is not a provider credential.
export ELSPETH_FINGERPRINT_KEY="$(
  .venv/bin/python -c 'import secrets; print(secrets.token_hex(32))'
)"

# Start server (must use --workers=1 due to errorworks bug with multi-worker presets)
chaosllm serve --port 8199 --preset=realistic --workers=1 &
sleep 3

# Run examples
elspeth run --settings examples/chaosllm_sentiment/settings.yaml --execute
elspeth run --settings examples/rate_limited_llm/settings.yaml --execute
elspeth run --settings examples/chaosllm_endurance/settings.yaml --execute  # 10K rows, slow
```

Do not gate dogfood completion on the full `chaosllm_endurance` workload. It
expands 10,000 rows into 100,000 mock LLM calls before retries. For ordinary
examples dogfood, use a bounded smoke input such as:

```bash
CHAOSLLM_ENDURANCE_ROWS=20 ./examples/chaosllm_endurance/run.sh
```

Alternatively, a few minutes of successful retry/quarantine/interruption
behavior against the ChaosLLM server is enough evidence for this endurance
category.

**Known issue:** The `realistic` preset sets `workers: 4` but errorworks 0.1.1 passes the app as a Python object to uvicorn, which only supports `workers=1` in that mode. Always pass `--workers=1` explicitly. See `docs/bugs/errorworks-workers-bug.md`.

| Example | Rows | Notes |
|---------|------|-------|
| `chaosllm_sentiment` | 10 | Basic sentiment with fault injection |
| `rate_limited_llm` | 8 | Rate limiter with ChaosLLM |
| `chaosllm_endurance` | 10,000 | Long-running endurance test |

### ChaosWeb (mock web server required)

```bash
# Start server (same --workers=1 workaround)
chaosweb serve --port 8200 --preset=realistic --workers=1 &
sleep 3

elspeth run --settings examples/chaosweb/settings.yaml --execute
```

| Example | Rows | Notes |
|---------|------|-------|
| `chaosweb` | 10 | Web scraping with fault injection |

### Chroma RAG (embedded, no external server)

ChromaDB runs embedded — no server setup needed, but requires `chromadb` package.

| Example | Rows | Notes |
|---------|------|-------|
| `chroma_rag` | 8 | Vector retrieval only; run `./examples/chroma_rag/run.sh` to seed and query |
| `chroma_rag_indexed` | 10 index + 5 query | Dependency-managed indexing and retrieval; run `elspeth run --settings examples/chroma_rag_indexed/query_pipeline.yaml --execute` |
| `chroma_rag_qa` | 8 | RAG + OpenRouter LLM QA; run `./examples/chroma_rag_qa/run.sh` (~19s, uses API credits) |

### OpenRouter (real API, costs money)

These call real LLM APIs via OpenRouter. Requires `OPENROUTER_API_KEY` in `.env`.

**Cost control:** Use `timeout 20` to cap execution time:
```bash
timeout 20 elspeth run --settings examples/openrouter_sentiment/settings.yaml --execute
```

| Example | Rows | Typical time | Notes |
|---------|------|-------------|-------|
| `llm_source` | 1 | one request | One static authored prompt emitted as one generated row |
| `openrouter_sentiment` | 5 | ~6s | GPT-4o-mini sentiment |
| `template_lookups` | 5 | ~8s | Claude Haiku with templates |
| `openrouter_multi_query_assessment` | 3 | ~18s | Claude Sonnet multi-query |
| `schema_contracts_llm_assessment` | 3 | >20s | Claude Sonnet — may need longer timeout or resume |

If a pipeline is interrupted, resume with the command shown in the output.

### Azure (skip unless Azure credentials configured)

| Example | Notes |
|---------|-------|
| `azure_blob_sentiment` | Azure Blob Storage source |
| `azure_keyvault_secrets` | Azure Key Vault secrets |
| `azure_openai_sentiment` | Azure OpenAI endpoint |
| `multi_query_assessment` | Azure OpenAI multi-query (settings say `provider: azure`) |

### Not a runnable pipeline

| Directory | Contents |
|-----------|----------|
| `chaosllm/` | Sample response data (`responses.jsonl`), not a pipeline |

## Troubleshooting

- **Port already in use:** `fuser -k 8199/tcp` (or 8200 for chaosweb)
- **Workers error on chaosllm/chaosweb:** Always pass `--workers=1`
- **OpenRouter timeout:** Use `timeout <seconds>` wrapper, then `elspeth resume <run_id> --execute`
- **Permission denied on `/app/`:** You're running a container example outside Docker
- **Missing errorworks commands:** Run `uv pip install --force-reinstall errorworks` to regenerate entry points

### 0.6.0 — Multi-Worker & Concurrent Scheduling

`concurrent_scheduler` is pure-data (no server). `multi_worker` and
`multi_worker_showcase` start their own ChaosLLM server inside `run.sh` (with
`--workers 1`), establish a process-scoped fingerprint key when needed, and
orchestrate a leader + `elspeth join` follower(s); run them via their `run.sh`,
not a bare `elspeth run`. Their default ChaosLLM profiles add latency without
terminal faults so the self-verifying launchers have a deterministic clean-run
contract; each README documents a separate opt-in fault profile. (`elspeth
join` takes no `--execute` flag — only `elspeth run` does.)

```bash
.venv/bin/elspeth run --settings examples/concurrent_scheduler/settings.yaml --execute
./examples/multi_worker/run.sh                      # leader + 1 follower (self-verifying)
WORKERS=3 ./examples/multi_worker_showcase/run.sh   # 4-way, asserts shared work
```

Do not gate dogfood completion on `multi_worker_showcase` (~200 rows × 4
workers — the heaviest of the three). For a bounded smoke, run `multi_worker`
(leader + 1 follower) instead.

| Example | Rows / Work units | Notes |
|---------|-------------------|-------|
| `concurrent_scheduler` | 6 (2×3 CSV rows) | Count-6 rendezvous; proves concurrent scheduling; `elspeth run` only |
| `multi_worker` | 120 (1 JSONL row → 120 exploded items) | `elspeth join` leader+follower; asserts ≥2 workers shared rows; `WORKERS` env |
| `multi_worker_showcase` | 200 (10×20 exploded items) | 4-worker swarm + stats card; asserts ≥2 workers shared outcomes; NOT for dogfood gate |
