# CRAP baseline — `main` @ 72fad2c23 (2026-11-22)

Baseline of ELSPETH against the CRAP metric (Savoia & Evans, crap4j):

    CRAP(m) = comp(m)² × (1 − cov(m))³ + comp(m)

A function scores above **30** ("crappy", risky to change) when its complexity is
high relative to its test coverage. At 100% coverage CRAP equals complexity, so
a function with complexity above 30 is over the line however well it is tested;
at 0% coverage the limit is complexity 5.

Full per-function data (`crap.csv`, 14,924 rows sorted by CRAP) is not committed:
at 1.5 MB it exceeds the repository's 1,000 KB file limit. Regenerate it with the
recipe under [Reproduce](#reproduce), from the scripts in
[`crap-baseline-2026-11-22/`](crap-baseline-2026-11-22/).

## Method and provenance

| Input | Source | Notes |
|---|---|---|
| Commit | `72fad2c23` (`main`, merge of #375, release 0.8.15) | Every shard manifest pins `tested_commit` to this SHA. |
| Coverage | CI run 44477162225: the 8 ordinary shards, the timing shard and the 3 Landscape-proof shards (Python 3.14), combined locally with [`coveragerc`](crap-baseline-2026-11-22/coveragerc) | Branch coverage. Per-function `percent_covered` (statements + branches) from coverage.py 7.13.1 function regions. |
| Complexity (primary) | radon 6.0.1 `cc` (run via `uvx`, isolated from the project venv) | Adds 1 per boolean operator, closest to crap4j's bytecode branch count. Nested functions are separate regions, the same as coverage.py. |
| Complexity (sensitivity) | ruff C901 (McCabe) with `max-complexity=0` | Does not count `and`/`or`. Folds nested function bodies into the enclosing function, so it reads high for functions that contain closures (e.g. `_check_schema_contracts`). |
| Churn | `git log 72fad2c23 --since=2026-08-24 -- src/elspeth`, commits per file | File-level, not function-level. |

Nothing ran on the shared box beyond radon, ruff, `coverage combine` and `git log`
over a `git archive` export. No test suite was started.

### Instrument controls

- Formula: `crap.py` asserts the crap4j reference values (cc 10 @ 0% = 110,
  cc 10 @ 100% = 10, cc 5 @ 0% = 30) before scoring.
- Combine: all 1,013 source files are present with local paths; TOTAL 196,335
  statements / 20,497 missed / 88%, against CI's own combined report for the
  previous `main` run (936b627fe, 2 commits earlier): 194,597 / 20,322 / 87%.
- Join: 14,924 functions matched. Unmatched: 36 radon blocks (`@overload` stubs
  sharing a name with the implementation) and 50 coverage regions (methods of
  classes defined inside functions, which radon does not visit). 324 regions are
  fully excluded (`pragma: no cover`, `@abstractmethod`) and are not scored.
- Cross-check: ruff and radon agree on function boundaries for 14,862 of the
  14,924 functions; their complexity values differ by definition (see above).

### Known blind spots (read before acting on a 0% row)

1. **Subprocess code reads 0%.** CI's coverage config has no subprocess
   patching, so code that only runs in a child process is never measured.
   Confirmed for `web/process_watchdog_helper.py::supervise` (spawned by path
   in `test_portable_supervisor.py`) and `templates.py::_serve_request` (listed
   as a process boundary in `test_executors.py`). The `composer_mcp` supervisor,
   bootstrap and control functions, and the `mcp` worker/server entry points,
   are likely in the same class but not individually confirmed.
2. **PostgreSQL-only code reads 0%.** The `testcontainer` job is not an input to
   `Combined coverage`, so code exercised only against PostgreSQL is
   unmeasured. `composer_progress_authority.py` is a likely case.
3. **One test failed** in shard 6
   (`test_textract_document_analysis::test_shutdown_during_poll_backoff_releases_no_output`,
   `IndexError: pop from empty list`). The rest of that shard's coverage is
   included.
4. Coverage measures execution, not assertion. A high-coverage, high-complexity
   function is "tested" only in the sense that its lines ran.

## Headline

| Measure | radon (counts `and`/`or`) | McCabe (ruff C901) |
|---|---:|---:|
| Functions scored | 14924 | 14862 |
| CRAP > 30 | 438 | 177 |
| Share over threshold | 2.93% | 1.19% |
| Over threshold at 0% coverage | 39 | 24 |
| Over threshold even at 100% coverage (complexity > 30) | 270 | 99 |

radon CRAP distribution: median 3.0, p90 14.0, p99 60.6, max 3080.0.

| radon CRAP band | Functions |
|---|---:|
| ≤ 5 | 10377 |
| 5–15 | 3227 |
| 15–30 | 882 |
| 30–100 | 361 |
| > 100 | 77 |

## By area (radon), ranked by total excess over 30

| Area | Functions | Over 30 | Share | Σ(CRAP − 30) | Over 30 (McCabe) |
|---|---:|---:|---:|---:|---:|
| `web/composer` | 2242 | 92 | 4.1% | 3837 | 30 |
| `composer_mcp` | 99 | 8 | 8.1% | 3462 | 6 |
| `web/_aws_ecs_acceptance` | 268 | 27 | 10.1% | 2361 | 12 |
| `web` | 945 | 28 | 3.0% | 2059 | 14 |
| `web/sessions` | 1263 | 71 | 5.6% | 1972 | 31 |
| `web/coordination` | 649 | 17 | 2.6% | 1390 | 4 |
| `web/execution` | 557 | 19 | 3.4% | 1202 | 8 |
| `plugins/infrastructure` | 705 | 23 | 3.3% | 1092 | 8 |
| `mcp` | 100 | 9 | 9.0% | 833 | 7 |
| `plugins/transforms` | 1374 | 16 | 1.2% | 787 | 6 |
| `cli.py` | 76 | 10 | 13.2% | 683 | 8 |
| `contracts` | 1234 | 16 | 1.3% | 567 | 6 |
| `web/blobs` | 197 | 11 | 5.6% | 419 | 10 |
| `core/landscape` | 1180 | 17 | 1.4% | 408 | 5 |
| `core` | 619 | 14 | 2.3% | 343 | 4 |
| `core/dag` | 194 | 6 | 3.1% | 330 | 1 |
| `engine` | 400 | 7 | 1.8% | 313 | 2 |
| `plugins/sinks` | 422 | 10 | 2.4% | 201 | 4 |
| `engine/orchestrator` | 372 | 10 | 2.7% | 144 | 4 |
| `engine/executors` | 288 | 4 | 1.4% | 131 | 1 |

## Top 25 by radon CRAP

| CRAP | CRAP (McCabe) | CC | Cov % | Lines | File commits 90d | Function |
|---:|---:|---:|---:|---:|---:|---|
| 3080.0 | 1640.0 | 55 | 0.0 | 159 | 2 | `composer_mcp/portable_supervisor.py:129` `_Supervisor._run_owned` |
| 1260.0 | 702.0 | 35 | 0.0 | 112 | 2 | `web/process_watchdog_helper.py:32` `supervise` |
| 756.0 | 272.0 | 27 | 0.0 | 38 | 6 | `web/coordination/composer_progress_authority.py:83` `_read_snapshot` |
| 704.9 | 268.8 | 131 | 67.8 | 255 | 1 | `web/_aws_ecs_acceptance/manifest_schema.py:102` `_validate_control_manifest` |
| 423.2 | 108.5 | 55 | 50.4 | 92 | 103 | `web/composer/state.py:4660` `_bind_composer_value_transforms.field_type` |
| 380.0 | 240.0 | 19 | 0.0 | 83 | 31 | `plugins/infrastructure/templates.py:710` `_serve_request` |
| 371.5 | 75.1 | 120 | 74.1 | 273 | 2 | `web/_aws_ecs_acceptance/scenario_inventory.py:796` `_validate_scenario_inventory` |
| 342.0 | 90.0 | 18 | 0.0 | 61 | 1 | `web/execution/run_progress_reader.py:25` `RepositoryRunProgressReader.read_after` |
| 322.3 | 195.1 | 322 | 98.5 | 1673 | 103 | `web/composer/state.py:8482` `CompositionState._validate_with_probe_cache` |
| 316.0 | 217.9 | 314 | 97.3 | 1955 | 24 | `core/dag/builder.py:319` `build_execution_graph` |
| 306.0 | 156.0 | 17 | 0.0 | 59 | 7 | `mcp/server.py:823` `_find_audit_databases` |
| 282.8 | 63.9 | 138 | 80.3 | 273 | 8 | `web/composer/advisor_transcript.py:127` `AdvisorTranscriptHistory.from_records` |
| 280.3 | 106.5 | 211 | 88.4 | 947 | 37 | `engine/barrier_coordination.py:1679` `BarrierRecoveryCoordinator.restore_from_journal` |
| 270.6 | 176.6 | 191 | 87.0 | 1563 | 52 | `web/execution/service.py:3641` `ExecutionServiceImpl._run_pipeline` |
| 265.8 | 174.8 | 255 | 94.5 | 2738 | 71 | `web/composer/tool_batch.py:1050` `run_tool_batch` |
| 253.6 | 72.2 | 137 | 81.6 | 270 | 1 | `web/_aws_ecs_acceptance/task_definition.py:307` `validate_task_definition_policy_binding` |
| 230.7 | 38.4 | 21 | 22.0 | 82 | 5 | `web/composer/advisor_abandonment.py:105` `build_abandonment_review` |
| 228.4 | 150.9 | 126 | 81.4 | 582 | 37 | `plugins/transforms/web_scrape.py:1204` `WebScrapeTransform.process` |
| 226.3 | 114.1 | 26 | 33.3 | 127 | 52 | `web/execution/service.py:2975` `ExecutionServiceImpl.recover_run` |
| 226.3 | 56.7 | 120 | 80.5 | 463 | 1 | `web/_aws_ecs_acceptance/control_service.py:148` `_control_manifest_update_locked` |
| 226.0 | 199.3 | 58 | 63.2 | 483 | 49 | `cli.py:3426` `resume` |
| 223.4 | 64.3 | 78 | 71.2 | 199 | 27 | `plugins/infrastructure/clients/http.py:786` `AuditedHTTPClient._replay_ssrf_request` |
| 222.0 | 177.1 | 18 | 14.3 | 59 | 30 | `web/blobs/service.py:2575` `BlobServiceImpl._reconcile_blob_deletions_locked` |
| 214.7 | 420.1 | 207 | 94.4 | 3362 | 103 | `web/composer/state.py:4818` `_check_schema_contracts` |
| 211.3 | 61.2 | 73 | 70.4 | 134 | 25 | `plugins/transforms/pdf_rasterize.py:808` `PDFRasterize._restore_render_receipt` |

## Change hotspots: over 30 and in frequently changed files

Ranked by (CRAP − 30) × commits touching the file since 2026-08-24.

| CRAP | CC | Cov % | File commits 90d | Function |
|---:|---:|---:|---:|---|
| 423.2 | 55 | 50.4 | 103 | `web/composer/state.py:4660` `_bind_composer_value_transforms.field_type` |
| 322.3 | 322 | 98.5 | 103 | `web/composer/state.py:8482` `CompositionState._validate_with_probe_cache` |
| 214.7 | 207 | 94.4 | 103 | `web/composer/state.py:4818` `_check_schema_contracts` |
| 210.0 | 176 | 89.7 | 97 | `web/composer/pipeline_planner.py:3799` `_plan_pipeline_inner` |
| 265.8 | 255 | 94.5 | 71 | `web/composer/tool_batch.py:1050` `run_tool_batch` |
| 114.6 | 85 | 84.0 | 153 | `web/composer/service.py:2844` `ComposerServiceImpl._compose_loop` |
| 270.6 | 191 | 87.0 | 52 | `web/execution/service.py:3641` `ExecutionServiceImpl._run_pipeline` |
| 104.7 | 89 | 87.4 | 153 | `web/composer/service.py:2142` `ComposerServiceImpl._classify_and_budget_turn` |
| 380.0 | 19 | 0.0 | 31 | `plugins/infrastructure/templates.py:710` `_serve_request` |
| 226.3 | 26 | 33.3 | 52 | `web/execution/service.py:2975` `ExecutionServiceImpl.recover_run` |
| 226.0 | 58 | 63.2 | 49 | `cli.py:3426` `resume` |
| 280.3 | 211 | 88.4 | 37 | `engine/barrier_coordination.py:1679` `BarrierRecoveryCoordinator.restore_from_journal` |
| 189.2 | 53 | 63.5 | 49 | `cli.py:4402` `join` |
| 146.7 | 145 | 95.6 | 64 | `web/composer/tools/sessions.py:656` `build_set_pipeline_candidate` |
| 228.4 | 126 | 81.4 | 37 | `plugins/transforms/web_scrape.py:1204` `WebScrapeTransform.process` |
| 316.0 | 314 | 97.3 | 24 | `core/dag/builder.py:319` `build_execution_graph` |
| 3080.0 | 55 | 0.0 | 2 | `composer_mcp/portable_supervisor.py:129` `_Supervisor._run_owned` |
| 222.0 | 18 | 14.3 | 30 | `web/blobs/service.py:2575` `BlobServiceImpl._reconcile_blob_deletions_locked` |
| 149.7 | 74 | 76.0 | 47 | `web/coordination/repository.py:1176` `_RepositoryInterpretationMutations._create_pending` |
| 223.4 | 78 | 71.2 | 27 | `plugins/infrastructure/clients/http.py:786` `AuditedHTTPClient._replay_ssrf_request` |

## Worst coverage gaps (complexity ≤ 30, so tests alone would clear them)

| CRAP | CC | Cov % | Function |
|---:|---:|---:|---|
| 756.0 | 27 | 0.0 | `web/coordination/composer_progress_authority.py:83` `_read_snapshot` |
| 380.0 | 19 | 0.0 | `plugins/infrastructure/templates.py:710` `_serve_request` |
| 342.0 | 18 | 0.0 | `web/execution/run_progress_reader.py:25` `RepositoryRunProgressReader.read_after` |
| 306.0 | 17 | 0.0 | `mcp/server.py:823` `_find_audit_databases` |
| 230.7 | 21 | 22.0 | `web/composer/advisor_abandonment.py:105` `build_abandonment_review` |
| 226.3 | 26 | 33.3 | `web/execution/service.py:2975` `ExecutionServiceImpl.recover_run` |
| 222.0 | 18 | 14.3 | `web/blobs/service.py:2575` `BlobServiceImpl._reconcile_blob_deletions_locked` |
| 210.0 | 14 | 0.0 | `web/coordination/composer_progress_authority.py:394` `SessionComposerProgressAuthority.replay` |
| 182.0 | 13 | 0.0 | `mcp/query_worker.py:219` `_read_request` |
| 156.0 | 12 | 0.0 | `composer_mcp/portable_bootstrap.py:112` `connect_supervisor` |
| 156.0 | 12 | 0.0 | `composer_mcp/portable_control.py:68` `PortableProcessWatchdog._read_frames` |
| 156.0 | 12 | 0.0 | `web/process_watchdog.py:143` `ProcessWatchdog._exchange_sync` |
| 143.8 | 25 | 42.5 | `plugins/transforms/aws/textract_client.py:130` `_replay_error` |
| 132.0 | 11 | 0.0 | `plugins/infrastructure/templates.py:1358` `_template_literals` |
| 132.0 | 11 | 0.0 | `mcp/server.py:933` `main` |
| 132.0 | 11 | 0.0 | `web/execution/fanout_guard.py:603` `_count_json_source_rows` |
| 126.6 | 12 | 7.3 | `core/landscape/data_flow/outcomes.py:167` `TokenOutcomeRepository._lock_cross_table_witnesses` |
| 113.1 | 27 | 50.9 | `contracts/blobs.py:783` `BlobDeletionPlan.__post_init__` |
| 110.0 | 10 | 0.0 | `mcp/query_control.py:46` `QueryControl.monitor` |
| 110.0 | 10 | 0.0 | `mcp/analyzers/diagnostics.py:396` `get_recent_activity` |

## Reading the baseline

- **The share over 30 is low** (2.9% radon / 1.2% McCabe; crap4j's rule of thumb
  treats more than 5% as a problem). The median function scores 3.
- **Most of the excess is complexity, not missing tests.** 270 of the 438
  radon-crappy functions have complexity above 30, so no amount of testing
  brings them under the line; only decomposition does. The largest are single
  functions of 1,500–3,400 lines with 94%+ coverage
  (`_check_schema_contracts`, `run_tool_batch`, `_validate_with_probe_cache`,
  `build_execution_graph`).
- **Change risk sits in the composer.** `web/composer` and `web/sessions` hold
  163 of the 438, and the hotspot table is dominated by `state.py`,
  `pipeline_planner.py`, `tool_batch.py` and `service.py`, the files with the
  most commits in the last 90 days.
- **The pure coverage gaps are short and fixable.** The "worst coverage gaps"
  table lists functions whose complexity is 30 or below, so tests alone would
  clear them. Check the blind spots above first: several are subprocess or
  PostgreSQL code that is tested but not measured.

## Reproduce

```bash
# 1. download coverage shards for a CI run on main
gh run download <run-id> -p 'ordinary-shard-3.14-*' -p 'timing-shard-3.14-*' -p 'landscape-proof-*'
# 2. export the tested commit and combine (paths in coveragerc are relative to the export root)
git archive <sha> src pyproject.toml | tar -x -C export/
cd export && COVERAGE_FILE=../combined.cov coverage combine --rcfile=../coveragerc --keep <shard .coverage files>
COVERAGE_FILE=../combined.cov coverage json --rcfile=../coveragerc -o ../coverage.json
# 3. complexity, churn, score
uvx --python 3.14 --from radon==6.0.1 radon cc -j -s src/elspeth > ../radon.json
ruff check --isolated --select C901 --config 'lint.mccabe.max-complexity=0' --output-format json src/elspeth > ../ruff_cc.json
git log <sha> --since=<date> --name-only --pretty=format:@@ -- src/elspeth   # → churn.json {path: commits}
python3 crap.py radon.json coverage.json churn.json crap.csv
python3 analyse.py crap.csv ruff_cc.json > tables.md
```
