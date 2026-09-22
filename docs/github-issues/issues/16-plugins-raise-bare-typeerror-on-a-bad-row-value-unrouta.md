---
title: Batch-aware transforms raise bare TypeError on a bad row value, aborting the whole run
labels: [area/plugins, area/engine, type/bug]
---

A buffered row whose value has the wrong type raises a bare `TypeError`. That exception matches no clause in the transform retry path and nothing in `engine/` converts it, so a row-level data fault aborts the entire run: `on_error` never fires, the row is never counted, and the operator gets a raw traceback and exit 4.

## What happens

`batch_replicate` reads a `copies` field. Two bad inputs to that one field, everything else identical:

| `copies` | check | exit | traceback | counters | token outcomes |
|---|---|---|---|---|---|
| `0` — bad value, right type | `raw_copies < 1` → quarantine | 2 | none | 2 failed | 2 terminal |
| `"abc"` — bad type | `type(raw_copies) is not int` → `raise TypeError` | 4 | yes | 0 failed | 0 terminal, 4 pending |

The routable idiom is already in the file, three lines from the crashing one (`src/elspeth/plugins/transforms/batch_replicate.py:298`). Reproductions were measured at `d211fcc8a`, `280887d9c` and `28a602dfd`.

## Why

`TypeError` is not a routable transform outcome. `RowProcessor._execute_transform_with_retry` (`src/elspeth/engine/processor.py:2318`) handles `InterruptedError`, `PluginRetryableError`, `ConnectionError`/`TimeoutError`/`CapacityError`, the Tier-1 error set and `PluginContractViolation`. There is no `TypeError` arm, `is_retryable` does not match it, and the one broad `except BaseException` in the attempt wrapper stamps a node-state id and re-raises without converting anything. The exception therefore leaves the retry seam untranslated and surfaces as a framework crash.

The exception class was copied along with its message. Scope on the prefix `This indicates an upstream validation bug` and nothing longer — the trailing dash differs between plugins, so a pattern that includes it silently misses `report_assemble`:

```
$ grep -rn "This indicates an upstream validation bug" src/elspeth/plugins/
```

That returns 12 raise sites across 11 batch-aware transforms: `batch_classifier_metrics`, `batch_distribution_profile`, `batch_drift_compare` (two sites), `batch_effect_size`, `batch_experiment_compare`, `batch_outlier_annotator`, `batch_paired_preference`, `batch_replicate`, `batch_threshold_summary`, `batch_top_k`, `report_assemble`. Every one raises `TypeError`. The convention originally reached 17 sites across 16 transforms, including per-row ones, so this is not inherently an aggregation problem.

## Impact

Any pipeline whose source can yield a wrongly-typed value into one of these transforms. `on_error: quarantine` can be wired and still do nothing; the audit trail records no terminal outcome for the affected tokens; the operator sees a framework traceback instead of a data error.

## Fix

The error must be **returned**, not raised. A raised `PluginContractViolation` is still unroutable at the aggregation flush seam, so swapping the exception class alone would leave these sites aborting while appearing fixed. Do not coerce: a `str` that is not a number is not a number.

The shape splits 2 against 11. Only `batch_replicate` and `report_assemble` check inside `process` and can return `TransformResult.error` directly. The other eleven sites sit in value-returning helpers — `_validate_label`, `_finite_values_for`, `_numeric_values_for`, `_categorical_values_for`, `_stats_for_group`, `_finite_entries_for`, `_score_entry_for`, `_validate_value` — which cannot return a result. `src/elspeth/plugins/transforms/_batch_row_types.py` already carries `BatchRowTypeError` for exactly this: helpers raise it, `process` catches it once and converts, mirroring `reference_join`. One shared module, never a copy per plugin — copying is how this convention spread. `batch_stats` is currently its only caller, so generalising from either `process`-level site would get the shape wrong for the majority.

The reason payload has two constraints. `TransformErrorReason` (`src/elspeth/contracts/errors.py:635`) is the audit reason type and already declares `expected` and `actual_type` for this case, so do not widen the TypedDict — the row index belongs in `error`, and the offending row value must never reach the reason at all.

Out of scope: the `None` and non-finite branches keep skip-and-report, because a missing value is semantically different from a wrongly-typed one.

Stated limit: even with the error returned, the audit still shows `sink_name=None` and path `unrouted`, because no batch-shaped node can name a reachable error sink — `on_error: <sink>` is unbuildable for aggregations and collectors, which is a separate defect. That is not quarantine and must not be described as such.

## Testing notes

A test here can pass while arming nothing. The guards differ, so there is no uniform fixture:

- The seven numeric guards (`not in (int, float)`) need an observed-mode `str`. Set the aggregation's own schema to `mode: observed` as well as the source's, or the fixed schema re-coerces `str` to `float` and the guard never fires — a fixture missing this is silently vacuous.
- `batch_classifier_metrics` (`not in (str, int, bool)`) rejects floats, not strings: arm with a float label column.
- `report_assemble` (`type(value) is not str`) is the inverse and rejects numbers: arm with an int column.
- `batch_top_k` (`not in (str, int, float, bool)`) accepts every scalar, so only a non-scalar trips it. Arm with the `json` source carrying a list; it is the only site unreachable from a CSV-only pipeline.

Values are deep-frozen before the check, so the reported type name is not always `type(value).__name__`: a list reports as `tuple`, a dict as `mappingproxy`.
