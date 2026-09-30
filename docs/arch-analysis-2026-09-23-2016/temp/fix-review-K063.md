# K063 fix review (red team): batch-aware plugins abort the run on a wrong-type row value

- Reviewed: HEAD `ffd704d1a` on `release/0.8.1`. The batch/aggregation fixes arrived in merge `bc0f251c4` (67135a9db..282d7a0a6) plus the post-merge commits b605550a5, 69c57f943, 2ceeba488, ffd704d1a and b653a81d4.
- Tracker rows: elspeth-5887fb7928 (K063) and elspeth-d2e3f29d10 (aggregation on_error).
- Posture: read-only. Mutations ran only in the throwaway detached worktree `.claude/worktrees/rt-k063`. That worktree was removed with `git worktree remove --force`, and `git worktree list | grep -c rt-k063` then returned 0. Nothing in the main checkout was modified.

## Verdict: CLOSED

K063 as filed is fixed, with one low-severity adjacent gap listed under Findings. None of the 13 batch-aware plugins raises out of the run on a wrong-type row value any more. `raise TypeError` has 0 hits in `plugins/transforms/batch_*.py` and `report_assemble.py`. The original end-to-end reproducer now ends in a controlled PARTIAL result instead of exit 4. Every mutant I applied to the fix was killed by named tests.

## 1. Coverage

Literal Q1 grep: `grep -rn "raise TypeError" src/elspeth/plugins/transforms/` finds 0 hits in the batch plugins. The remaining hits are:

- blob_fetch.py:481 and web_scrape.py:796. These are per-row plugins, half 1 of the ticket and outside K063, and the TypeError is caught at blob_fetch.py:521 and web_scrape.py:836.
- The textract, guardrails, llm/multi_query, llm/provider and rag/query sites. These are config, owned-type or internal checks, except rag/query, which b653a81d4 routes. All are outside K063.

The registry (`discover_all_plugins()`, `is_batch_aware`) lists 13 batch-aware plugins: batch_classifier_metrics, batch_data_quality_report, batch_distribution_profile, batch_drift_compare, batch_effect_size, batch_experiment_compare, batch_outlier_annotator, batch_paired_preference, batch_replicate, batch_stats, batch_threshold_summary, batch_top_k and report_assemble. `grep is_batch_aware = True` finds no others under src (only test doubles in engine/contracts docstrings).

The only remaining `raise ValueError` sites in these files are:

- pydantic config validators, all at construction time;
- `batch_outlier_annotator.py:527` and `batch_replicate.py:373`, which fire on heterogeneous contract modes across the batch. That is engine metadata, not a row value, so it is correctly Tier 1.

`report_assemble.py:277` raises RuntimeError when `aggregation_batch` is missing. That is an engine invariant, and 312de6af8 refuses report_assemble as a collector.

Fuzz run: `scratchpad/fuzz.py`. I called each plugin's real `process()` with 14 adversarial values in every field it reads (str, numeric str, bool, None, int of 10**400 and -10**400, NaN, Decimal, dict, list, bytes, datetime, float, int), 336 cases in all. Every wrong-type case returned `TransformResult.error` with `invalid_input/wrong_type`. The distinctive marker value did not appear in any reason (0 LEAK). The only exceptions were `OverflowError` from batch_stats on a 10**400 int (see "Attacks tried and rejected").

The presence check (23c78dd3d): each plugin's `base_required` covers exactly the fields its `row[...]` reads, which I compared per plugin. So `validate_batch_inputs` turns a missing field into a routed PluginContractViolation before any KeyError can occur.

End-to-end at HEAD, CLI `run --execute`:

- Original reproducer (observed-schema CSV into batch_threshold_summary with on_error: discard): exit 1, "Run PARTIAL: 2 rows processed | 0 succeeded | 2 failed | 2 quarantined", no traceback. At the pin this was exit 4.
- batch_outlier_annotator, passthrough, count trigger, on_error quarantine: exit 2, 4 rows routed to quarantine, token_outcomes `(failure, on_error_routed, quarantine) x4`, 2 FAILED batches, and 4 transform_errors rows with a value-free reason.
- Timeout trigger (`timeout_seconds`, handle_timeout_flush path): the same routing, with 4 FAILED batches whose trigger_type is `timeout`.
- Collector (json_explode opener, batch_replicate collector with an observed schema, require_all; both documents carry a str `copies`, so the PLUGIN's BatchRowTypeError fires rather than the preflight): exit 2, no traceback, "2 collector groups failed".
  - token_outcomes: `(failure, unrouted) x5` for the 5 member pages and `(transient, expand_parent) x2`. The count of tokens without a completed outcome is 0.
  - `collector_group_failures` holds 2 rows with reason `collector_transform_error`. All 7 collector node_states are FAILED, and the sentinel value appears in none of their error_json.

## 2. Trust tiers

- `_invoke_batch_transform` (aggregation.py:437-456) still records FAILED and re-raises any exception that is not a PluginContractViolation. A plugin bug such as RuntimeError, KeyError or a BatchRowTypeError the plugin failed to catch still aborts the run.
- `_run_flush_transform` (aggregation.py:516-541) converts only PluginContractViolation, and only after `except TIER_1_ERRORS: raise`. The same shape is used at the collector (collector.py:1160-1177). The only PluginContractViolation subclasses are ZeroEmissionSuccessContractViolation and SinkTransactionalInvariantError. A runtime check showed that `TIER_1_ERRORS` (12 entries) contains SinkTransactionalInvariantError, which is re-raised. It does not contain ZeroEmissionSuccessContractViolation or the base PluginContractViolation, so both of those are routed. PluginConfigError is a plain Exception, not a subclass.
- The fix does route some plugin-contract bugs as batch failures: success with no rows, output-schema failure, non-canonical output and an expected_output_count mismatch. This is operator ruling B2 (elspeth-5887fb7928) and matches the existing per-row policy (elspeth-181db83da7; PluginContractViolation is declared TIER-2 at errors.py:1657). It is policy, not silent bug-hiding: the violation text is recorded on the flush state and in transform_errors. It is not reported as a finding.

## 3. Audit correctness

- Named sink: each member gets `(FAILURE, ON_ERROR_ROUTED, <sink>)` and one DIVERT routing event. Discard gives `(FAILURE, QUARANTINED_AT_SOURCE)` with error_hash = hash of the canonical batch reason. That is the same pair the per-row transform discard already uses (ADR-019 row "QUARANTINED"). There is one transform_errors row per member. Seen in the e2e audit DB and pinned by test_row_type_violation_routing.py:558/652.
- Value-free: the BatchRowTypeError and BatchRowFieldCollisionError reasons carry field names, types and the row index only. Input-model failures use `safe_validation_error_text`. 0 LEAKs in the fuzz run, and the e2e transform_errors JSON contained no row value.
- Resume finality: `recorded_failure_verdict_condition` (batch_lineage.py:110-147) is excluded from `get_incomplete_batches` (batches.py:463) and refused by `retry_batch`. Mutant M3 shows the tests enforce "never re-invokes the plugin".

## 4. Scope and accounting

69c57f943 adds a new counter, `collector_groups_failed`. It comes from a new Landscape table `collector_group_failures` (epoch 45), sits outside the (outcome, path) table, and was added to `failure_indicator` in `RunResult` and `derive_terminal_run_status`. It is audit-only and excluded from the live/audit parity check. The web accounting (`web/execution/accounting.py:298,370`), the web status validator (`schemas.py:564-595`) and MCP `get_run_summary` all read the same table, so the three surfaces agree.

ADR-018 was amended, and it already mandates this pattern: a structural failure counter must be registered in `failure_indicator`. ADR-019 itself was not amended. Its §"Counter derivation contract" list and its predicate text do not mention the counter. That is doc drift, not a behaviour contradiction, and is not filed.

## 5. Mutation results

Each mutant was applied in the worktree, run with `-n 0 -p no:cacheprovider`, and then restored from `git show HEAD:<path>`. `elspeth.__file__` was verified to resolve inside the worktree. Baseline, unmutated: test_batch_threshold_summary.py plus test_row_type_violation_routing.py, 49 passed, exit 0.

| # | Mutant | Tests run | Result |
|---|---|---|---|
| M1 | batch_threshold_summary.process: `except BatchRowTypeError` re-raises TypeError (the pre-fix behaviour) | tests/unit/plugins/transforms/test_batch_threshold_summary.py | KILLED, 2 failed (test_non_numeric_value_fails_the_whole_batch_with_a_recorded_reason, test_bool_value_fails_the_whole_batch) |
| M2 | aggregation._run_flush_transform: the PluginContractViolation arm re-raises | test_row_type_violation_routing.py + tests/unit/engine/test_executors.py | KILLED, 15 failed (typed-schema route/discard, missing-field route, output-count, non-canonical-output, span ERROR, and others) |
| M3 | batches.get_incomplete_batches: `.where(~recorded_failure_verdict_condition())` dropped, so resume would retry a verdict batch | test_aggregation_recovery.py, test_batch_flush_recovery_and_redaction.py, test_aggregation_failure_verdict.py | KILLED, 23 failed (including test_resume_completes_the_recorded_verdict_and_never_reinvokes_the_plugin x12) |
| M4 | validate_batch_inputs: `if absent:` changed to `if False and absent:` | test_row_type_violation_routing.py, test_batch_contract_validation_parity.py, test_collector_executor.py | KILLED, 8 failed |
| M5 | batch_top_k.process: `except BatchRowTypeError` re-raises TypeError | test_row_type_violation_routing.py -k top_k (engine-level Orchestrator run) | KILLED, 1 failed (TypeError escaped Orchestrator.run) |

## Findings

### F1 (low, confirmed): a non-scalar group, cohort, variant or pair key is silently bucketed in 6 plugins

batch_stats now rejects a non-scalar group key as a wrong type (c3cdeee89). Its commit message calls a JSON array "silently bucketed as one category" a defect. The same key handling remains unguarded elsewhere:

- batch_distribution_profile `_group_rows`, batch_distribution_profile.py:294
- batch_top_k group_by, batch_top_k.py:224
- batch_drift_compare cohort, batch_drift_compare.py:279
- batch_effect_size variant, batch_effect_size.py:250
- batch_experiment_compare variant, batch_experiment_compare.py:272
- batch_paired_preference pair and variant, batch_paired_preference.py:232,287

Keys are compared by equality only, so a mappingproxy or tuple key is accepted as a category.

E2E (JSON source, batch_distribution_profile with `group_by: g`, where g is `["x",1]`, `{"k":"y"}` or `"z"`): exit 0, COMPLETED. The output rows are grouped by `['x', 1]` and `{'k': 'y'}`, with summaries such as "grouped by g=('x', 1)".

This does not abort the run and loses no data, so it is not K063's crash. It is a wrong-type value that is neither failed nor routed, and it contradicts the batch_stats ruling.

## Attacks tried and rejected

- **OverflowError on huge ints.** Two escapes exist at unit level: batch_stats.py:456 `sum(values)` on floats mixed with a 10**400 int, and batch_distribution_profile.py:397 `_format_number` with ±10**309 and 198 zeros. Neither is reachable. `stable_hash` raises IntegerDomainError for any int above 2**53, and an e2e JSON row carrying 10**400 was dropped at the source ("1 rows processed"). Not filed.
- **Fix reverted while tests survive.** None of the post-merge commits touches the plugin, executor, batches or lineage sites. The main checkout has no src modifications. The fuzz run exercised live HEAD code.
- **Missing-field KeyError.** Closed by the presence check, and declarations match reads for all 13 plugins.
- **Timeout-flush path.** It routes; see §1.
- **Passthrough mode.** It routes; see §1.
- **Value leaks.** None found in the fuzz run or the e2e audit rows.

```json
{"findings": [
  {"title": "Non-scalar group/cohort/variant/pair keys silently bucketed in 6 batch plugins (batch_stats rejects the same input)",
   "severity": "low",
   "confidence": "confirmed",
   "files": ["src/elspeth/plugins/transforms/batch_distribution_profile.py", "src/elspeth/plugins/transforms/batch_top_k.py", "src/elspeth/plugins/transforms/batch_drift_compare.py", "src/elspeth/plugins/transforms/batch_effect_size.py", "src/elspeth/plugins/transforms/batch_experiment_compare.py", "src/elspeth/plugins/transforms/batch_paired_preference.py"],
   "repro": "JSONL source (observed) rows {\"g\":[\"x\",1],\"v\":1.5},{\"g\":[\"x\",1],\"v\":2.5},{\"g\":{\"k\":\"y\"},\"v\":3.5},{\"g\":\"z\",\"v\":4.5} -> aggregation batch_distribution_profile value_field v group_by g, on_error discard; `python -m elspeth.cli run -s settings.yaml --execute` => exit 0 COMPLETED, output groups ['x',1] and {'k':'y'}. Unit fuzz: process() returns success for dict/list/bytes keys in all 6 plugins, while batch_stats returns invalid_input/wrong_type.",
   "detail": "c3cdeee89 made batch_stats reject non-scalar group_by keys as BatchRowTypeError, explicitly calling silent bucketing of a JSON array a defect. The equality-only grouping in distribution_profile (:294), top_k (:224), drift_compare cohort (:279), effect_size (:250), experiment_compare (:272) and paired_preference (:232,:287) still accepts mappingproxy/tuple keys as categories. Not an abort (so not K063's crash), but a wrong-type row value that is neither failed nor routed, inconsistent with the ruling applied to batch_stats."}
]}
```
