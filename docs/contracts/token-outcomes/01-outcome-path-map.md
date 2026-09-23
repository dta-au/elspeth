# Outcome Path Map

Current as of 2026-09-23.

Use this map to locate the producer responsible for a token outcome gap. The
exact line numbers change often; treat the file/function names as the stable
orientation and verify against source before editing.

| Outcome/path | Meaning | Primary producer |
|--------------|---------|------------------|
| `(success, default_flow)` | Token reached a normal sink successfully. | Sink/orchestrator completion path in `src/elspeth/engine/`. |
| `(success, gate_routed)` | Gate routed the token to a named sink. | Gate/row processing path in `src/elspeth/engine/processor.py`. |
| `(success, gate_discarded)` | Gate route target intentionally discarded the token. | Gate/row processing path in `src/elspeth/engine/processor.py`. |
| `(failure, gate_error_discarded)` | Config-gate expression evaluation failed and configured `on_error: discard` stopped that row without a sink. | Gate error terminalization in `src/elspeth/engine/token_traversal.py`. |
| `(failure, on_error_routed)` | Transform processing or config-gate expression evaluation failed and `on_error` routed the token to an error sink; or an aggregation batch failed and its `on_error` routed every buffered token (with its original row) to the named sink. | Transform and gate executors plus error handling in `src/elspeth/engine/token_traversal.py`; the failed-flush arm of `RowProcessor.handle_timeout_flush`, or the journal restore completing a recorded FAILED verdict on resume (both `RowProcessor._dispose_failed_flush`). Recorded by the sink after durability. |
| `(success, filter_dropped)` | A filter-style transform intentionally dropped the row. | Transform result handling in `src/elspeth/engine/processor.py`. |
| `(success, coalesced)` | Branch token was consumed by a coalesce operation. | Coalesce handling in `src/elspeth/engine/coalesce_executor.py` and processor recovery branches. |
| `(failure, unrouted)` | A token could not be routed to a valid destination, or a batch flush hit a Tier-1 cross-check violation before the run crashed. | Routing failure handling in `src/elspeth/engine/processor.py` (incl. `_record_flush_violation`). A FAILED batch no longer produces it: see the next row and the routed row above. |
| `(failure, quarantined_at_source)` | Source validation failed; a transform's `on_error: discard`; a member a successful batch quarantined; or an aggregation batch failed with `on_error: discard` (operator ruling B3, the per-row discard pair). | Source handling in `src/elspeth/engine/orchestrator/` / data-flow repository write path; `token_traversal.handle_transform_error_status`; `RowProcessor._complete_aggregation_flush` (terminal outcomes inside `complete_barrier`). |
| `(transient, sink_fallback_to_failsink)` | Sink failure was redirected to a failsink and the final lifecycle answer lives in paired evidence. | Sink execution/error handling in `src/elspeth/engine/executors.py`. |
| `(failure, sink_discarded)` | Sink failure was discarded through the discard sentinel. | Sink execution/error handling in `src/elspeth/engine/executors.py`. |
| `(transient, fork_parent)` | Parent token delegated to fork children. | Fork handling in `src/elspeth/core/landscape/data_flow_repository.py` and engine token manager paths. |
| `(transient, expand_parent)` | Parent token delegated to expanded children. | Expand/deaggregation handling in data-flow repository and processor paths. |
| `(transient, batch_consumed)` | Token was consumed by batch handling. | Batch aggregation in `src/elspeth/engine/processor.py` and batch repository paths. |
| `(NULL, buffered)` | Token is waiting in a batch buffer and is not terminal yet. | Batch aggregation in `src/elspeth/engine/processor.py`. |

## Cross-Checks

- If a completed sink node state exists without a terminal token outcome, inspect
  the sink completion path.
- If a terminal token outcome points to a sink but no sink node state/artifact
  exists, inspect the sink write path.
- If a parent path has no child or batch evidence, inspect the corresponding
  token-manager/data-flow repository path.
- If a `buffered` row remains after run completion, inspect batch flush and
  finalization handling.
