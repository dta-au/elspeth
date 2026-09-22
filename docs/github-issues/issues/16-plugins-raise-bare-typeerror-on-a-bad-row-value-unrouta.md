---
title: Batch-aware transforms raise bare TypeError on a bad row value, aborting the whole run
labels: [area/plugins, area/engine, type/bug]
---

A buffered row whose value has the wrong type raises a bare `TypeError`. Nothing in the engine converts that into a routable outcome, so one bad cell aborts the entire run instead of being diverted: the configured error sink never fires, the row is never counted as failed, and the operator gets a raw traceback and exit code 4.

## Where to start

`src/elspeth/plugins/transforms/`. A transform is a plugin that receives rows and returns a `TransformResult`; a *batch-aware* transform (`is_batch_aware = True`) buffers rows and produces its result when the batch is flushed, which is how the aggregation plugins work.

One worked example exists in the tree: `batch_stats.py` raises the shared `BatchRowTypeError` from its helper (line 386) and converts it to a returned error in `process` (line 563), using `_batch_row_types.py`. Read those three places first.

## What happens

`batch_replicate` reads a `copies` field. Two bad inputs to that one field, everything else identical:

| `copies` | check | exit | traceback | rows counted failed | audit outcomes |
|---|---|---|---|---|---|
| `0` — bad value, right type | `raw_copies < 1` → quarantine | 2 | none | 2 | 2 terminal |
| `"abc"` — bad type | `type(raw_copies) is not int` → `raise TypeError` | 4 | yes | 0 | 0 terminal, 4 pending |

Audit outcomes are the per-row records ELSPETH writes to its audit trail, so four left pending means the run's account of those rows is never closed. The routable idiom sits three lines from the crashing one (`src/elspeth/plugins/transforms/batch_replicate.py:298`). Evidence for this issue was measured at commits `d211fcc8a`, `280887d9c` and `28a602dfd`.

## Why

`TypeError` is not a routable transform outcome. `RowProcessor._execute_transform_with_retry` (`src/elspeth/engine/processor.py:2318`) handles `InterruptedError`, `PluginRetryableError`, the transport errors, the trusted-error set and `PluginContractViolation` — there is no `TypeError` arm, its `is_retryable` helper does not match one, and the single broad `except BaseException` in the attempt wrapper (`processor.py:2419`) records a node-state id and re-raises without converting. The exception leaves the retry seam untranslated and surfaces as a framework crash.

The exception class was copied between plugins with its message. To find the sites, scope on the prefix `This indicates an upstream validation bug` and nothing longer — the trailing dash differs between plugins, so a pattern including it silently misses `report_assemble`:

```
$ grep -rn "This indicates an upstream validation bug" src/elspeth/plugins/
```

That returns 12 raise sites across 11 batch-aware transforms — nine `batch_*` plugins, `batch_drift_compare` twice, and `report_assemble` — and every one raises `TypeError`. The convention originally reached 17 sites across 16 transforms, including per-row ones, so this is not inherently an aggregation problem.

## Impact

Any pipeline whose source can hand a wrongly-typed value to one of these transforms. An `on_error` sink can be configured and still do nothing, and the operator sees a framework traceback rather than a data error.

## Fix

The error must be **returned** as a `TransformResult.error`, not raised. Converting `TypeError` to a raised `PluginContractViolation` is the obvious first move and it does not work: a raised violation is still unroutable at the point where a batch-aware transform flushes its buffer, so those sites would keep aborting while appearing fixed.

The work splits 2 against 11. Only `batch_replicate` and `report_assemble` check inside `process`, where a result can be returned directly. The other eleven sites sit in helpers that return a value rather than a result (`_validate_label`, `_finite_values_for`, `_numeric_values_for`, `_categorical_values_for`, `_stats_for_group`, `_finite_entries_for`, `_score_entry_for`, `_validate_value`) and cannot return one. Those raise `BatchRowTypeError` and let `process` catch it once and convert, as `batch_stats` does. Keep that logic in `_batch_row_types.py`: a second copy of the rule is the same defect as the first.

Do not coerce — a `str` that is not a number is not a number.

Two constraints on the reason payload. `TransformErrorReason` (`src/elspeth/contracts/errors.py:635`) is the audit-trail reason type and already declares `expected` and `actual_type` for this case, so do not widen the TypedDict; the row index belongs in the `error` field. And the offending row value must never reach the reason, because row content is untrusted and the reason is written to the audit trail.

Out of scope: the `None` and non-finite branches keep their skip-and-report behaviour, because a missing value is semantically different from a wrongly-typed one.

**Acceptance.** A run that trips one of these guards should end up exactly where the `copies: 0` row of the table above ends up — exit 2, no traceback, the rows counted as failed, terminal rather than pending audit outcomes. That comparison is the test: run one pipeline with a bad value and one with a bad type, and assert the two dispositions match.

## Size, and what this ticket cannot deliver

Not a one-liner and not uniform: 12 sites, 11 of which need the helper-raises/`process`-converts shape rather than a direct return. Starting from either easy `process`-level site generalises the wrong shape for the majority, so start from `batch_stats`.

One limit matters before starting. Even with the error returned, the audit still records `sink_name=None` and a path of `unrouted`, because no batch-shaped node can currently name a reachable error sink — `on_error: <sink>` is unbuildable for aggregations and collectors, a separate defect. This ticket can deliver "the failure is recorded and the run survives", not "the row reaches a quarantine sink", and must not be described as quarantine. Agree with a maintainer first whether to do all 12 sites now or wait for that routing defect, since the return path changes when it lands.

## Testing notes

A test here can pass while arming nothing. The four non-obvious guards:

- The seven numeric guards (`not in (int, float)`) need a `str` to survive as a `str`. Set the aggregation's own schema to `mode: observed` — which infers column types from the data rather than declaring them — as well as the source's, or the aggregation's fixed schema re-coerces `str` to `float` and the guard never fires. A fixture missing this is silently vacuous.
- `batch_classifier_metrics` (`not in (str, int, bool)`) rejects floats, not strings: use a float label column.
- `report_assemble` (`type(value) is not str`) is the inverse: use an int column.
- `batch_top_k` (`not in (str, int, float, bool)`) accepts every scalar, so only a non-scalar trips it — use the `json` source carrying a list. It is the only one of the twelve unreachable from a CSV-only pipeline.

Rows are made immutable before the check, so the reported type name is not always `type(value).__name__`: a list reports as `tuple`, a dict as `mappingproxy`.
