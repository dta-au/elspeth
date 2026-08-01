# ELSPETH Examples

This directory contains runnable pipeline examples demonstrating ELSPETH's
features. Most examples have a `settings.yaml` entry point; launchers and named
pipeline entry points are listed below and in each example's README.

## Quick Start

```bash
# Standard case: run from the repository root
elspeth run --settings examples/<name>/settings.yaml --execute

# Explore the audit trail using the URL configured by that example
elspeth explain --run latest --database examples/<name>/runs/audit.db
```

Some examples need setup or use multiple configurations:

| Example | Canonical entry point |
|---------|-----------------------|
| `database_sink` | `./examples/database_sink/run.sh` |
| `blob_transforms` (offline) | `./examples/blob_transforms/run.sh` |
| `blob_transforms` (hosted fetch) | `./examples/blob_transforms/run_hosted_fetch.sh` |
| `chroma_rag` | `./examples/chroma_rag/run.sh` |
| `chroma_rag_qa` (OpenRouter) | `./examples/chroma_rag_qa/run.sh` |
| `chroma_rag_indexed` | `elspeth run --settings examples/chroma_rag_indexed/query_pipeline.yaml --execute` |
| `multi_worker` | `./examples/multi_worker/run.sh` |
| `multi_worker_showcase` | `./examples/multi_worker_showcase/run.sh` |
| `statistical_batch_plugins` | Run one `settings_*.yaml` file at a time |

Landscape database names vary between variants. Read the selected YAML or the
individual README before using `elspeth explain`.

## Example Index

### Pure Data Processing (no external APIs)

These examples run locally with no credentials or external services.

| Example | What It Demonstrates |
|---------|---------------------|
| [`threshold_gate`](threshold_gate/) | Simplest gate — one numeric threshold, two sinks |
| [`boolean_routing`](boolean_routing/) | Gate routing based on a string field value |
| [`explicit_routing`](explicit_routing/) | Declarative `on_success`/`input` wiring pattern |
| [`error_routing`](error_routing/) | `on_error` diversion to quarantine sinks |
| [`deep_routing`](deep_routing/) | 5 chained gates, 3 transforms, 7 sinks — complex decision tree |
| [`fork_coalesce`](fork_coalesce/) | Fork/join DAG pattern — parallel paths merged with configurable policy (includes ARCH-15 per-branch transforms variant) |
| [`row_union_ab_experiment`](row_union_ab_experiment/) | Fork-based A/B experiment — `row_union` releases both variant branches as one correlated group in long format; variants cover pooled and paired statistics, screening with reject routing, and list-form branches |
| [`batch_aggregation`](batch_aggregation/) | Count-triggered aggregation with group-by statistics |
| [`report_assemble`](report_assemble/) | Assemble text rows into paginated markdown reports with flush metadata |
| [`statistical_batch_plugins`](statistical_batch_plugins/) | Statistical batch QA: distributions, experiments, classifier metrics, paired preferences, drift, outliers, data quality, top-k, thresholds, and effect sizes |
| [`deaggregation`](deaggregation/) | 1-to-N row expansion via `batch_replicate` |
| [`json_explode`](json_explode/) | Expand nested JSON arrays into individual rows |
| [`transform_pipeline`](transform_pipeline/) | Normalize CSV field types, then compute derived values with `type_coerce` and `value_transform` |
| [`database_sink`](database_sink/) | Write pipeline output to SQLite via `./examples/database_sink/run.sh`, which provisions the operator-owned target and effect ledger |
| [`checkpoint_resume`](checkpoint_resume/) | Crash recovery via checkpointing and `elspeth resume` |
| [`retention_purge`](retention_purge/) | Payload retention lifecycle and `elspeth purge` |
| [`blob_transforms`](blob_transforms/) | Blob-backed ingestion: offline CSV expansion via `run.sh`, plus opt-in hosted tutorial HTML fetch via `run_hosted_fetch.sh` |
| [`audit_export`](audit_export/) | Export the Landscape audit trail to JSON |
| [`landscape_journal`](landscape_journal/) | Event journaling for real-time audit monitoring |
| [`multi_flow`](multi_flow/) | Two independent named source flows in one run |
| [`multi_source_queue`](multi_source_queue/) | Multiple named sources fan into a durable pass-through queue |
| [`schema_contracts_demo`](schema_contracts_demo/) | DAG-time schema validation (`guaranteed_fields` / `required_input_fields`) |
| [`large_scale_test`](large_scale_test/) | Performance testing with large datasets |

### Container Deployment

| Example | What It Demonstrates |
|---------|---------------------|
| [`threshold_gate_container`](threshold_gate_container/) | The threshold-gate pipeline packaged with `/app/pipeline/` paths for Docker |

### RAG / ChromaDB (requires `chromadb` — no API keys for retrieval-only)

These examples demonstrate Retrieval-Augmented Generation using ChromaDB as a vector store. Install ChromaDB first: `uv pip install chromadb`.

| Example | What It Demonstrates |
|---------|---------------------|
| [`chroma_rag`](chroma_rag/) | Basic RAG retrieval — `./examples/chroma_rag/run.sh` seeds the collection, then runs retrieval |
| [`chroma_rag_qa`](chroma_rag_qa/) | RAG + LLM via `./examples/chroma_rag_qa/run.sh` (requires `OPENROUTER_API_KEY`) |
| [`chroma_rag_indexed`](chroma_rag_indexed/) | **Pipeline dependencies** — `depends_on` runs an indexing pipeline first, commencement gate verifies the collection, then query pipeline retrieves context. Entry point: `query_pipeline.yaml` |

### 0.6.0 — Multi-Worker & Concurrent Scheduling

New in 0.6.0: examples that demonstrate concurrent in-process token scheduling
(ADR-026) and multi-worker packs (ADR-030, `elspeth join`).

| Example | What It Demonstrates |
|---------|---------------------|
| [`concurrent_scheduler`](concurrent_scheduler/) | Count-6 two-source rendezvous — proves the scheduler holds multiple token lifecycles open at once (pure-data, self-verifying) |
| [`multi_worker`](multi_worker/) | `elspeth join` — leader + follower(s) share one RUNNING run; asserts ≥2 workers shared the rows (ChaosLLM, self-verifying) |
| [`multi_worker_showcase`](multi_worker_showcase/) | 4-worker swarm with live stats and a shared-work assertion (ChaosLLM, self-verifying) |

### OpenRouter LLM (real API — requires `OPENROUTER_API_KEY`)

These examples call the real OpenRouter API. Set your API key first:

```bash
export OPENROUTER_API_KEY="your-key-from-openrouter.ai"
```

| Example | What It Demonstrates |
|---------|---------------------|
| [`openrouter_sentiment`](openrouter_sentiment/) | Single-query sentiment analysis (sequential and pooled modes) |
| [`openrouter_multi_query_assessment`](openrouter_multi_query_assessment/) | Multi-query matrix (case studies x criteria) with stress/overflow variants |
| [`schema_contracts_llm_assessment`](schema_contracts_llm_assessment/) | LLM pipeline with DAG-time schema contract validation |
| [`template_lookups`](template_lookups/) | Jinja2 template-driven prompts with field extraction |

### Azure (requires Azure credentials)

| Example | What It Demonstrates |
|---------|---------------------|
| [`azure_openai_sentiment`](azure_openai_sentiment/) | Azure OpenAI endpoint (sequential and pooled) |
| [`azure_blob_sentiment`](azure_blob_sentiment/) | Azure Blob Storage source with LLM processing |
| [`azure_keyvault_secrets`](azure_keyvault_secrets/) | Secret resolution from Azure Key Vault |
| [`multi_query_assessment`](multi_query_assessment/) | Azure-backed multi-query assessment matrix |

### ChaosLLM / ChaosWeb (local fault injection — no provider API keys needed)

These examples use local fault injection servers to test pipeline resilience
without real API credentials or OpenRouter traffic. The ChaosLLM settings still
contain a fake `api_key` field required by the OpenAI-compatible client.
Convenience launchers generate a process-scoped `ELSPETH_FINGERPRINT_KEY` so
ELSPETH can safely fingerprint that field; manual commands must set one as
shown in the individual READMEs.

| Example | What It Demonstrates |
|---------|---------------------|
| [`chaosllm_sentiment`](chaosllm_sentiment/) | Sentiment analysis against ChaosLLM (mirrors `openrouter_sentiment`) |
| [`chaosllm_endurance`](chaosllm_endurance/) | Multi-query endurance test with fault injection |
| [`rate_limited_llm`](rate_limited_llm/) | LLM pipeline with rate limiting (30 req/min cap) |
| [`chaosweb`](chaosweb/) | Web scraping resilience with ChaosWeb fault injection |
| [`chaosllm`](chaosllm/) | Response data used by ChaosLLM server (not a runnable pipeline) |

### Expected Non-Complete Demonstrations

Some examples deliberately exercise failure accounting:

| Example or variant | Expected result |
|--------------------|-----------------|
| `deep_routing`, `error_routing` | `PARTIAL`, exit 1; packaged blocked-content rows reach quarantine |
| `fork_coalesce/settings_union_fail.yaml` | `FAILED`, non-zero exit; the first field collision aborts the run |
| `row_union_ab_experiment/settings_screened.yaml` | `PARTIAL`, exit 0; screened pairs fail closed and remain audited |
| ChaosLLM / ChaosWeb realistic fault profiles | Stochastic `COMPLETED`, `PARTIAL`, or preflight failure depending on injected faults; verify every ingested row reached a result or error sink |

## Resetting examples

Examples write a local audit trail to `runs/audit.db`. ELSPETH is pre-1.0 and
does not migrate audit databases in place, so re-running an example after
upgrading ELSPETH can fail with `SchemaCompatibilityError`. Clear the
accumulated artifacts with:

```bash
./examples/reset.sh
```

A fresh checkout has no such artifacts and needs no reset.

---

## If You Want to See...

| You want to learn about... | Look at... |
|---------------------------|-----------|
| **How wiring works** | [`explicit_routing`](explicit_routing/) — the canonical minimal example |
| **Named source roots** | [`multi_source_queue`](multi_source_queue/) — two sources feeding one queue |
| **Independent flows in one run** | [`multi_flow`](multi_flow/) — two source→transform→sink branches with shared audit |
| **Simple routing** | [`threshold_gate`](threshold_gate/) or [`boolean_routing`](boolean_routing/) |
| **Complex decision trees** | [`deep_routing`](deep_routing/) — 5 gates, 7 sinks, 8-node-deep DAG |
| **Fork/join patterns** | [`fork_coalesce`](fork_coalesce/) — parallel paths with merge policies |
| **Fork/union patterns (A/B)** | [`row_union_ab_experiment`](row_union_ab_experiment/) — both branches kept as separate correlated rows for cross-variant statistics |
| **Error handling / quarantine** | [`error_routing`](error_routing/) — `on_error` diversion pattern |
| **Aggregation (N to 1)** | [`batch_aggregation`](batch_aggregation/) — count triggers, group-by stats; [`report_assemble`](report_assemble/) — paginated markdown reports |
| **Statistical batch QA** | [`statistical_batch_plugins`](statistical_batch_plugins/) — prompt/model score comparisons, classifier metrics, drift, outlier annotation, data quality, top-k, thresholds, and effect sizes |
| **Deaggregation (1 to N)** | [`deaggregation`](deaggregation/), [`json_explode`](json_explode/), or [`blob_transforms`](blob_transforms/) |
| **Type normalization and derived fields** | [`transform_pipeline`](transform_pipeline/) — coerce CSV strings to typed values, then compute dependent fields |
| **LLM integration (quick start)** | [`openrouter_sentiment`](openrouter_sentiment/) — simplest real LLM pipeline |
| **LLM without API keys** | [`chaosllm_sentiment`](chaosllm_sentiment/) — same pipeline, local ChaosLLM server |
| **Multi-query LLM matrices** | [`openrouter_multi_query_assessment`](openrouter_multi_query_assessment/) — case studies x criteria |
| **Pooled/concurrent execution** | [`openrouter_sentiment`](openrouter_sentiment/) — has `settings_pooled.yaml` variant |
| **Rate limiting** | [`rate_limited_llm`](rate_limited_llm/) — throttled API calls with ChaosLLM |
| **Schema contracts** | [`schema_contracts_demo`](schema_contracts_demo/) (pure data) or [`schema_contracts_llm_assessment`](schema_contracts_llm_assessment/) (with LLM) |
| **Jinja2 templates** | [`template_lookups`](template_lookups/) — field extraction and template-driven prompts |
| **Web scraping** | [`chaosweb`](chaosweb/) — fault-injected scraping with content gates |
| **Database output** | [`database_sink`](database_sink/) — write to SQLite or PostgreSQL |
| **Crash recovery / resume** | [`checkpoint_resume`](checkpoint_resume/) — checkpoint + Ctrl-C + `elspeth resume` |
| **Graceful shutdown** | [`checkpoint_resume`](checkpoint_resume/) — covers Ctrl-C shutdown behaviour |
| **Payload retention / blob refs** | [`retention_purge`](retention_purge/) — payload lifecycle and `elspeth purge`; [`blob_transforms`](blob_transforms/) — fetch/store blobs and expand CSV blobs |
| **Audit trail export** | [`audit_export`](audit_export/) — JSON export with optional signing |
| **Event journaling** | [`landscape_journal`](landscape_journal/) — real-time audit event stream |
| **Azure integration** | [`azure_openai_sentiment`](azure_openai_sentiment/), [`azure_blob_sentiment`](azure_blob_sentiment/), [`azure_keyvault_secrets`](azure_keyvault_secrets/) |
| **Docker deployment** | [`threshold_gate_container`](threshold_gate_container/) — containerised pipeline |
| **Retry under faults** | [`chaosllm_endurance`](chaosllm_endurance/) — 5 retries with exponential backoff against ChaosLLM |
| **Stress testing** | [`large_scale_test`](large_scale_test/) or [`chaosllm_endurance`](chaosllm_endurance/) |
| **RAG retrieval** | [`chroma_rag`](chroma_rag/) — basic vector search against ChromaDB |
| **RAG + LLM** | [`chroma_rag_qa`](chroma_rag_qa/) — retrieval then LLM-generated answers |
| **Pipeline dependencies (`depends_on`)** | [`chroma_rag_indexed`](chroma_rag_indexed/) — index → gate → query in one command |
| **Commencement gates** | [`chroma_rag_indexed`](chroma_rag_indexed/) — go/no-go check before pipeline starts |
