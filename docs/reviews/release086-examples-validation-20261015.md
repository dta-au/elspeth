# Release 0.8.6 local example validation — 2026-10-15

**PASS for the eligible local corpus:** 81 pipeline configurations executed with their documented outcomes; the canonical shipped-example module passed **37 tests**. No engine regression or example configuration defect was observed in this scope.

Host execution commit: `62410c8f03507ef1c277d45cf591b7ac6fd211f3`; final bridge: `f4d9fcc44db7c3c472107b613dcfdd7a26821461`. Run window: `2026-10-14T21:36:13.263362+00:00` to `2026-10-14T21:52:51.584936+00:00` (UTC; 15 October AEDT).

Discovery used `TestShippedExamples._find_example_settings()` from `tests/e2e/examples/test_shipped_examples.py`, including its explicit auxiliary YAML and no-settings classifications, then checked all example directories against that inventory. Measured inventory: **58 directories, 104 shipped pipeline configs, 81 eligible, 23 excluded configs, and 2 data/generated-only directories**. Discovery controls included a real LLM-source pipeline and both indexed pipeline names, and excluded an auxiliary ChaosLLM YAML.

Host examples used a fresh immutable Git export and a separate disposable directory per invocation. Both `elspeth` and `elspeth_lints` imported from the export, using the existing private Python 3.13.15 / ELSPETH 0.8.6 environment without installing dependencies. Mock/fixture sockets were local only; the sanitized child environment contained no provider credentials. A lane-only Python socket guard passed an allowed loopback connection and blocked external-hostname/address controls. Chroma used a copy of an already cached MiniLM model with telemetry disabled.

The container example separately used existing image `elspeth-086-local-check:85e00d6`, pinned as `sha256:625f17e8235560bb09aff4042d3612db5b3c7734d39fc1d1e8bd7fad6af0e585`, originally built from `85e00d616f8b32fe762c28704e183909a3a6e23d`. It was not built at the final bridge commit. All **1,917 Docker input hashes** from the prior image receipt were independently rechecked against final bridge `f4d9fcc44db7c3c472107b613dcfdd7a26821461` with no changes. It ran with `--pull=never --network=none`, the host UID/GID, and only its fresh example directory mounted at `/app/pipeline`. No rebuild or registry operation occurred. Its actual runtime was Python **3.13.5**, interpreter `/opt/venv/bin/python`, installed ELSPETH **0.8.6** under `/opt/venv/lib/python3.13/site-packages`; `elspeth_lints` is absent from the runtime image. These imports were measured in the same pinned image with networking and writes disabled and recorded in `container-provenance.log`; this runtime is distinct from the host private environment.

Runs were serial; `multi_worker` used one leader plus one follower. The focused pytest invocation used `-n 0`. Host capacity was checked outside the sandbox namespace before execution and refreshed before the final phase (24 CPUs; load 5.53/8.79/8.71 initially, 10.67/10.21/9.51 later); active CI and other project suites were recorded. No broad/default/native/PostgreSQL suite ran in this lane.

Every record has the exact commit, argv, working directory, exit, UTC start/end, original-input check, before/after input hash, audit statuses and row counts in `.claude/lanes/release086-examples-20261015/results.jsonl`; individual stdout/stderr is in that lane's `logs/`. All 76 records retained frozen input checks. An independent final comparison against `git archive` verified all **6,542 exported files byte-for-byte**. Row-count controls matched known nonempty/empty fixtures and rejected a row-loss mutant.

Three initial staging attempts (`blob_transforms`, `database_sink`, `pdf_rasterize`) are retained as **harness failures**, not example results: a synthetic Python symlink lost private-venv discovery, causing missing-package imports. A lane-only exec wrapper restored the exact interpreter; positive controls verified `sqlalchemy`, `rfc8785`, both source roots, and the private venv prefix. Fresh retries produced the validated results below. The initial PDF staging failure also exited 1, demonstrating why exit code alone is insufficient.

## Observed behavior

- All 50 direct configs matched expected exit, terminal status and source-row counts. Intentional PARTIAL/FAILED fixtures were assessed against their documented outcomes.
- Rank passthrough variants each wrote 7 shortlisted and 5 remaining rows; lineage was 12 tokens/0 parent links versus 24/12. Collector policies wrote 2 versus 3 rows. Deaggregation wrote 11 from 6; JSON explosion wrote 6 from 3; paired comparison returned 6 wins, 2 losses and 0.75 win rate. Batch error routes, reference misses, blocked-row counts and the 10 collision-failed branch holds matched their contracts.
- The five blob variants wrote 200/3/5/5/3 rows. PDF rasterization wrote 3 page rows and quarantined the malformed document. SQLite publication wrote 4 deals with 1 target-side effect-ledger entry. Embedded Chroma retrieval wrote 8 results; dependency-managed indexing ingested 10 documents before 5 queries.
- A/B and review-panel launchers verified every shipped variant, including fail-closed arm/reviewer loss. The bounded worker launcher attributed 60 terminal outcomes to each of two workers; join admission accepted the matching follower and refused the mismatched settings for the expected reason.
- Replay/verify asserted offline replay, matching verification, missing-source refusal (exit 1), settings-drift refusal (exit 4), and changed-page mismatch (exit 2). All six fixed-search variants asserted live/replay/verify, including pagination and multipart negative controls.
- Rate-limited mock LLM conserved 8/8 successful rows. ChaosWeb conserved 10 rows: 8 scraped results and 2 failure-sink rows, PARTIAL/exit 1.
- The extra checkpoint walkthrough interrupted after 2 rows (exit 3), then refused execute-resume with `source_not_exhausted` (exit 1).
- `tests/e2e/examples/test_shipped_examples.py`: **37 passed, 2 Click deprecation warnings**, pytest-reported 44.08 seconds, process exit 0.
- The local Docker threshold example completed 8 rows with 4 high-value and 4 normal outputs, exit 0. Its audit and 11 payload files survived container removal in the owned bind mount; audit ownership matched the host UID/GID.

## Guide drift and final input assessment

The tested `examples/AGENTS.md:176` still said changed-page verify exits 4 and repeated obsolete OpenRouter/volatile-field limitations. The observed changed-page exit was 2, matching `examples/replay_verify/run.sh:18` and `examples/replay_verify/README.md:174,216`. This was guide drift; the launcher and runtime contract agreed. The integrator corrected the paragraph in final bridge `f4d9fcc44db7c3c472107b613dcfdd7a26821461`.

The committed delta from execution commit `62410c8f03507ef1c277d45cf591b7ac6fd211f3` to final bridge `f4d9fcc44db7c3c472107b613dcfdd7a26821461` was exactly the guide paragraph and three proof/test/recipe files. No production source, example pipeline, fixture data, launcher, dependency or runtime input changed. The immutable-export results remain applicable; no repeat of green examples was needed. Raw comparisons: `final-input-check.log`, `final-bridge-check.json`, and `docker-input-check.json` in the private lane.

## Commands and result inventory

Commands below are portable renderings; exact absolute argv and cwd are in `results.jsonl`. `$EXPORT` is the immutable candidate export; `$PYTHON` is the existing private environment interpreter; `$ELSPETH` is a lane wrapper executing that interpreter with `from elspeth.cli import app; app()`; `$LANE` is the private evidence directory. Each invocation ran in its own disposable case root. Both export source roots and the local-only guard were on `PYTHONPATH`.

`D`: `$ELSPETH --json-logs run --settings examples/<config> --execute --format json`.

For launchers, the exit column is the outer invocation; documented internal failures are asserted by the shipped launcher. The config table also reports persisted run status. Times below are UTC on 2026-10-14; precise clocks are retained in the raw records.

| ID | Invocation | UTC start–end | Exit |
|---|---|---|---:|
| L1 | `bash examples/ab_llm_experiment/run.sh` | 21:40:39–21:41:03 | 0 |
| L2 | `bash examples/document_review_panel/run.sh` | 21:41:04–21:41:25 | 0 |
| L3 | `bash examples/reference_join_fork_llm/run.sh` | 21:41:26–21:41:34 | 0 |
| L4 | `bash examples/chaosllm_sentiment/run.sh` | 21:41:34–21:41:44 | 0 |
| L5 | `bash examples/multi_worker/run.sh` | 21:41:44–21:43:08 | 0 |
| L6 | `bash examples/join_refused/run.sh` | 21:43:08–21:43:40 | 0 |
| L7 | `bash examples/replay_verify/run.sh` | 21:43:41–21:44:09 | 0 |
| L8 | `bash examples/fixed_web_search/run_fixture.sh directory` | 21:44:09–21:44:22 | 0 |
| L9 | `bash examples/fixed_web_search/run_fixture.sh registry` | 21:44:23–21:44:36 | 0 |
| L10 | `bash examples/fixed_web_search/run_fixture.sh form` | 21:44:37–21:44:51 | 0 |
| L11 | `bash examples/fixed_web_search/run_fixture.sh multipart` | 21:44:51–21:45:05 | 0 |
| L12 | `bash examples/fixed_web_search/run_fixture.sh pagination` | 21:45:06–21:45:19 | 0 |
| L13 | `bash examples/fixed_web_search/run_detail_fixture.sh` | 21:45:19–21:45:34 | 0 |
| L14 | `bash examples/chroma_rag/run.sh` | 21:45:34–21:45:42 | 0 |
| L15 | `$ELSPETH --json-logs run --settings examples/chroma_rag_indexed/query_pipeline.yaml --execute --format json` | 21:45:42–21:45:51 | 0 |
| L16 | `bash examples/blob_transforms/run.sh` | 21:46:03–21:46:28 | 0 |
| L17 | `bash examples/database_sink/run.sh` | 21:46:29–21:46:34 | 0 |
| L18 | `bash examples/pdf_rasterize/run.sh` | 21:46:34–21:46:40 | 1 |
| L19 | `$PYTHON $LANE/local_fixture.py rate_limited_llm` | 21:46:40–21:46:50 | 0 |
| L20 | `$PYTHON $LANE/local_fixture.py chaosweb` | 21:46:50–21:47:02 | 1 |
| L21 | `$PYTHON $LANE/checkpoint_walk.py` | 21:47:02–21:47:09 | 0 |
| L22 | `$PYTHON -m pytest $EXPORT/tests/e2e/examples/test_shipped_examples.py -n 0 -o "pythonpath=$EXPORT/src $EXPORT/elspeth-lints/src" -o cache_dir=$LANE/pytest-cache --basetemp=$LANE/pytest-tmp` | 21:47:10–21:47:57 | 0 |
| L23 | `docker run --rm --pull=never --network=none --user 1000:1000 --mount type=bind,src=$LANE/cases/threshold_gate_container__docker/examples/threshold_gate_container,dst=/app/pipeline sha256:625f17e8235560bb09aff4042d3612db5b3c7734d39fc1d1e8bd7fad6af0e585 --json-logs run --settings /app/pipeline/settings.yaml --execute --format json` | 21:52:42–21:52:51 | 0 |

### Eligible configurations

`C` = completed; `P` = completed_with_failures; `F` = failed. Multiple statuses describe recorded workflow arms. Source rows are per recorded run, not a sum across replay/verify.

| Config under `examples/` | Command | UTC start–end | Exit | Persisted status; source rows |
|---|---|---|---:|---|
| `ab_llm_experiment/settings.yaml` | L1 | 21:40:39–21:41:03 | 0 | C; 8 |
| `ab_llm_experiment/settings_arm_loss.yaml` | L1 | 21:40:39–21:41:03 | 0 | P; 24 |
| `ab_llm_experiment/settings_models.yaml` | L1 | 21:40:39–21:41:03 | 0 | C; 8 |
| `audit_export/settings.yaml` | D | 21:36:13–21:36:18 | 0 | C; 8 |
| `batch_aggregation/settings.yaml` | D | 21:36:18–21:36:23 | 0 | C; 15 |
| `batch_error_routing/settings.yaml` | D | 21:36:23–21:36:28 | 1 | P; 12 |
| `batch_error_routing/settings_declared.yaml` | D | 21:36:28–21:36:33 | 1 | P; 12 |
| `batch_error_routing/settings_discard.yaml` | D | 21:36:33–21:36:38 | 1 | P; 12 |
| `batch_rank_passthrough/settings.yaml` | D | 21:36:39–21:36:44 | 0 | C; 12 |
| `batch_rank_passthrough/settings_transform.yaml` | D | 21:36:44–21:36:50 | 0 | C; 12 |
| `blob_transforms/settings_expand_csv_blobs.yaml` | L16 | 21:46:03–21:46:28 | 0 | C; 2 |
| `blob_transforms/settings_expand_inline_csv.yaml` | L16 | 21:46:03–21:46:28 | 0 | C; 2 |
| `blob_transforms/settings_expand_inline_json.yaml` | L16 | 21:46:03–21:46:28 | 0 | C; 2 |
| `blob_transforms/settings_expand_json_blobs.yaml` | L16 | 21:46:03–21:46:28 | 0 | C; 2 |
| `blob_transforms/settings_expand_text_blobs.yaml` | L16 | 21:46:03–21:46:28 | 0 | C; 2 |
| `boolean_routing/settings.yaml` | D | 21:36:50–21:36:56 | 0 | C; 10 |
| `chaosllm_sentiment/settings.yaml` | L4 | 21:41:34–21:41:44 | 0 | C; 10 |
| `chaosweb/settings.yaml` | L20 | 21:46:50–21:47:02 | 1 | P; 10 |
| `checkpoint_resume/settings.yaml` | D | 21:36:56–21:37:04 | 0 | C; 20 |
| `chroma_rag/settings.yaml` | L14 | 21:45:34–21:45:42 | 0 | C; 8 |
| `chroma_rag_indexed/index_pipeline.yaml` | L15 | 21:45:42–21:45:51 | 0 | C; 10 |
| `chroma_rag_indexed/query_pipeline.yaml` | L15 | 21:45:42–21:45:51 | 0 | C; 5 |
| `concurrent_scheduler/settings.yaml` | D | 21:37:05–21:37:13 | 0 | C; 6 |
| `database_sink/settings.yaml` | L17 | 21:46:29–21:46:34 | 0 | C; 8 |
| `deaggregation/settings.yaml` | D | 21:37:13–21:37:19 | 0 | C; 6 |
| `deep_routing/settings.yaml` | D | 21:37:19–21:37:25 | 1 | P; 20 |
| `document_review_panel/settings.yaml` | L2 | 21:41:04–21:41:25 | 0 | C; 4 |
| `document_review_panel/settings_incomplete.yaml` | L2 | 21:41:04–21:41:25 | 0 | P; 4 |
| `document_review_panel/settings_run_as_row.yaml` | L2 | 21:41:04–21:41:25 | 0 | P; 1 |
| `error_routing/settings.yaml` | D | 21:37:25–21:37:31 | 1 | P; 17 |
| `explicit_routing/settings.yaml` | D | 21:37:31–21:37:36 | 0 | C; 10 |
| `fixed_web_search/settings_fixture.yaml` | L8 | 21:44:09–21:44:22 | 0 | C/C/C; 3/3/3 |
| `fixed_web_search/settings_fixture_detail.yaml` | L13 | 21:45:19–21:45:34 | 0 | C/C/C; 3/3/3 |
| `fixed_web_search/settings_fixture_form.yaml` | L10 | 21:44:37–21:44:51 | 0 | C/C/C; 3/3/3 |
| `fixed_web_search/settings_fixture_multipart.yaml` | L11 | 21:44:51–21:45:05 | 0 | P/P/P; 4/4/4 |
| `fixed_web_search/settings_fixture_pagination.yaml` | L12 | 21:45:06–21:45:19 | 0 | C/C/C; 3/3/3 |
| `fixed_web_search/settings_fixture_registry.yaml` | L9 | 21:44:23–21:44:36 | 0 | C/C/C; 3/3/3 |
| `fork_coalesce/settings.yaml` | D | 21:37:37–21:37:42 | 0 | C; 5 |
| `fork_coalesce/settings_per_branch.yaml` | D | 21:37:42–21:37:47 | 0 | C; 5 |
| `fork_coalesce/settings_union_fail.yaml` | D | 21:37:47–21:37:51 | 2 | F; 5 |
| `fork_coalesce/settings_union_first_wins.yaml` | D | 21:37:52–21:37:56 | 0 | C; 5 |
| `fork_coalesce/settings_union_last_wins.yaml` | D | 21:37:57–21:38:01 | 0 | C; 5 |
| `join_refused/settings.yaml` | L6 | 21:43:08–21:43:40 | 0 | C; 1 |
| `join_refused/settings_mismatched.yaml` | L6 | 21:43:08–21:43:40 | 0 | mismatch refused internally (exit 1); leader C/1 source row, 80 work units |
| `json_explode/settings.yaml` | D | 21:38:02–21:38:06 | 0 | C; 3 |
| `landscape_journal/settings.yaml` | D | 21:38:07–21:38:11 | 0 | C; 2 |
| `multi_flow/settings.yaml` | D | 21:38:12–21:38:16 | 0 | C; 4 |
| `multi_source_queue/settings.yaml` | D | 21:38:16–21:38:21 | 0 | C; 3 |
| `multi_worker/settings.yaml` | L5 | 21:41:44–21:43:08 | 0 | C; 1 |
| `pdf_rasterize/settings.yaml` | L18 | 21:46:34–21:46:40 | 1 | P; 2 |
| `rate_limited_llm/settings.yaml` | L19 | 21:46:40–21:46:50 | 0 | C; 8 |
| `reference_join/settings.yaml` | D | 21:38:21–21:38:26 | 0 | C; 5 |
| `reference_join/settings_chained.yaml` | D | 21:38:26–21:38:31 | 1 | P; 4 |
| `reference_join/settings_missing_product.yaml` | D | 21:38:31–21:38:36 | 1 | P; 3 |
| `reference_join/settings_multi.yaml` | D | 21:38:36–21:38:41 | 1 | P; 5 |
| `reference_join/settings_nested.yaml` | D | 21:38:41–21:38:46 | 0 | C; 5 |
| `reference_join_fork_llm/settings.yaml` | L3 | 21:41:26–21:41:34 | 0 | C; 5 |
| `replay_verify/settings.yaml` | L7 | 21:43:41–21:44:09 | 0 | C/C/C/F; 3/3/3/3 |
| `report_assemble/settings.yaml` | D | 21:38:46–21:38:51 | 0 | C; 5 |
| `retention_purge/settings.yaml` | D | 21:38:51–21:38:56 | 0 | C; 5 |
| `row_union_ab_experiment/settings.yaml` | D | 21:38:56–21:39:02 | 0 | C; 8 |
| `row_union_ab_experiment/settings_identity_branches.yaml` | D | 21:39:02–21:39:07 | 0 | C; 8 |
| `row_union_ab_experiment/settings_paired_preference.yaml` | D | 21:39:07–21:39:12 | 0 | C; 8 |
| `row_union_ab_experiment/settings_screened.yaml` | D | 21:39:12–21:39:17 | 0 | C; 8 |
| `row_union_ab_experiment/settings_screened_at_settlement.yaml` | D | 21:39:18–21:39:23 | 1 | P; 8 |
| `schema_contracts_demo/settings.yaml` | D | 21:39:23–21:39:27 | 0 | C; 5 |
| `scope_collector/settings.yaml` | D | 21:39:28–21:39:33 | 1 | P; 3 |
| `scope_collector/settings_best_effort.yaml` | D | 21:39:33–21:39:37 | 1 | P; 3 |
| `statistical_batch_plugins/settings_classifier_metrics.yaml` | D | 21:39:37–21:39:42 | 0 | C; 8 |
| `statistical_batch_plugins/settings_data_quality_report.yaml` | D | 21:39:43–21:39:47 | 0 | C; 8 |
| `statistical_batch_plugins/settings_distribution_profile.yaml` | D | 21:39:48–21:39:52 | 0 | C; 8 |
| `statistical_batch_plugins/settings_drift_compare.yaml` | D | 21:39:52–21:39:57 | 0 | C; 8 |
| `statistical_batch_plugins/settings_effect_size.yaml` | D | 21:39:57–21:40:01 | 0 | C; 8 |
| `statistical_batch_plugins/settings_experiment_compare.yaml` | D | 21:40:01–21:40:05 | 0 | C; 8 |
| `statistical_batch_plugins/settings_outlier_annotator.yaml` | D | 21:40:06–21:40:11 | 0 | C; 8 |
| `statistical_batch_plugins/settings_paired_preference.yaml` | D | 21:40:11–21:40:15 | 0 | C; 8 |
| `statistical_batch_plugins/settings_threshold_summary.yaml` | D | 21:40:16–21:40:21 | 0 | C; 8 |
| `statistical_batch_plugins/settings_top_k.yaml` | D | 21:40:21–21:40:26 | 0 | C; 8 |
| `threshold_gate/settings.yaml` | D | 21:40:26–21:40:32 | 0 | C; 8 |
| `threshold_gate_container/settings.yaml` | L23 | 21:52:42–21:52:51 | 0 | C; 8 |
| `transform_pipeline/settings.yaml` | D | 21:40:32–21:40:37 | 0 | C; 5 |

### Explicit exclusions

| Config / directory under `examples/` | Reason |
|---|---|
| `azure_blob_sentiment/settings.yaml` | Azure Blob Storage / Azure OpenAI resources and credentials |
| `azure_blob_sentiment/settings_pooled.yaml` | Azure Blob Storage / Azure OpenAI resources and credentials |
| `azure_keyvault_secrets/settings.yaml` | Azure Key Vault resource and credentials |
| `azure_openai_sentiment/settings.yaml` | Azure OpenAI endpoint and credentials |
| `azure_openai_sentiment/settings_pooled.yaml` | Azure OpenAI endpoint and credentials |
| `azure_search_rag/settings.yaml` | Azure AI Search resource and credentials |
| `blob_transforms/settings_fetch_tutorial_html.yaml` | External hosted tutorial HTML websites |
| `chaosllm_endurance/settings.yaml` | 10,000 rows / 100,000 mock calls endurance exclusion |
| `chroma_rag_qa/settings.yaml` | Real OpenRouter LLM calls and API credits |
| `fixed_web_search/settings_abn_page.yaml` | External public ABN Lookup website |
| `large_scale_test/settings.yaml` | 10,000-row performance/endurance exclusion |
| `llm_source/settings.yaml` | Real OpenRouter LLM API and credentials |
| `multi_query_assessment/settings.yaml` | Azure OpenAI endpoint and credentials |
| `multi_worker_showcase/settings.yaml` | 200 child outcomes across 4 workers; explicitly outside the dogfood gate in `examples/AGENTS.md:266-274` and its `README.md:128-131`; bounded `multi_worker` ran instead |
| `openrouter_multi_query_assessment/settings.yaml` | Real OpenRouter LLM API and credentials (all stress/overflow variants included in this exclusion) |
| `openrouter_multi_query_assessment/settings_journal.yaml` | Real OpenRouter LLM API and credentials (all stress/overflow variants included in this exclusion) |
| `openrouter_multi_query_assessment/settings_overflow.yaml` | Real OpenRouter LLM API and credentials (all stress/overflow variants included in this exclusion) |
| `openrouter_multi_query_assessment/settings_stress.yaml` | Real OpenRouter LLM API and credentials (all stress/overflow variants included in this exclusion) |
| `openrouter_sentiment/settings.yaml` | Real OpenRouter LLM API and credentials |
| `openrouter_sentiment/settings_pooled.yaml` | Real OpenRouter LLM API and credentials |
| `power_automate/settings.yaml` | External Power Automate HTTP endpoints and credentials |
| `schema_contracts_llm_assessment/settings.yaml` | Real OpenRouter LLM API and credentials |
| `template_lookups/settings.yaml` | Real OpenRouter LLM API and credentials |
| `chaosllm/` | Data-only; no runnable pipeline |
| `textract_inline/` | AWS Textract billable API plus operator-supplied documents/generated settings |

The exclusions delimit this result: provider/cloud/public-network and endurance behavior remains outside this local validation. Full release qualification and integration are owned separately.
