# Token Outcome Contract

Current as of 2026-08-31.

This is the durable contract for token outcome records in Landscape. The
authoritative implementation lives in:

- `src/elspeth/contracts/enums.py`
- `src/elspeth/contracts/audit.py`
- `src/elspeth/core/landscape/schema.py`
- `src/elspeth/core/landscape/data_flow_repository.py`

## Definitions

- **Token**: one instance of a source row moving through a DAG path.
- **Terminal row**: a `token_outcomes` row with `completed = 1`.
- **Non-terminal row**: a `token_outcomes` row with `completed = 0`; either
  `(NULL, buffered)` for work that may still decide or `(NULL, abandoned)` for
  an undecided token whose run cannot resume.
- **Outcome**: lifecycle answer: `success`, `failure`, `transient`, or `NULL`.
- **Path**: producer-declared provenance answer.

## Legal Pairs

| completed | outcome | path | required | exact | forbidden |
|-----------|---------|------|----------|-------|-----------|
| 1 | `success` | `default_flow` | `sink_name` | none | `batch_id`, `error_hash` |
| 1 | `success` | `gate_routed` | `sink_name` | none | `batch_id`, `error_hash` |
| 1 | `success` | `gate_discarded` | none | none | `sink_name`, `batch_id`, `error_hash` |
| 1 | `failure` | `gate_error_discarded` | `error_hash` | none | `sink_name`, `batch_id` |
| 1 | `failure` | `on_error_routed` | `sink_name`, `error_hash` | none | `batch_id` |
| 1 | `success` | `filter_dropped` | none | none | `sink_name`, `batch_id`, `error_hash` |
| 1 | `success` | `coalesced` | none | none | `batch_id`, `error_hash` |
| 1 | `failure` | `unrouted` | `error_hash` | none | `sink_name`, `batch_id` |
| 1 | `failure` | `quarantined_at_source` | `error_hash` | none | `batch_id` |
| 1 | `transient` | `sink_fallback_to_failsink` | `sink_name`, `error_hash` | none | `batch_id` |
| 1 | `failure` | `sink_discarded` | `sink_name`, `error_hash` | `sink_name=__discard__` | `batch_id` |
| 1 | `transient` | `fork_parent` | none | none | `sink_name`, `batch_id`, `error_hash` |
| 1 | `transient` | `expand_parent` | none | none | `sink_name`, `batch_id`, `error_hash` |
| 1 | `transient` | `batch_consumed` | `batch_id` | none | `sink_name`, `error_hash` |
| 0 | `NULL` | `buffered` | `batch_id` | none | `sink_name`, `error_hash` |
| 0 | `NULL` | `abandoned` | none | none | `sink_name`, `batch_id`, `error_hash` |

Required fields must be non-NULL, exact fields must equal the shown value, and
forbidden fields must be NULL. A discriminator absent from all three columns is
optional. `context_json` is optional context rather than a discriminator.

## Invariants

1. Every token in a completed run has exactly one completed token outcome. A
   token in a non-resumable failed or interrupted run may instead have one
   non-terminal `abandoned` outcome.
2. A token may have non-terminal `buffered` rows before its decided or
   abandoned outcome.
3. `completed = 0` requires `outcome IS NULL` and a non-terminal path:
   `buffered` or `abandoned`.
4. `completed = 1` requires a legal `(outcome, path)` pair.
5. Required discriminator fields must be present for the pair.
6. Forbidden discriminator fields must be absent for the pair.
7. Parent/delegation paths (`fork_parent`, `expand_parent`, `batch_consumed`)
   must have corresponding lineage-frame, group, batch, or recovery evidence.
8. `token_outcomes` is the authoritative token lifecycle record. `node_states`
   and `artifacts` explain work, but do not replace the lifecycle row.

## Schema Notes

The table stores:

- identity: `outcome_id`, `run_id`, `token_id`
- lifecycle: `outcome`, `path`, `completed`, `recorded_at`
- discriminators: `sink_name`, `batch_id`, `error_hash`
- context: `context_json`

Fork/expand lineage is stored in `token_lineage_frames` and `group_records`.
The coalesce result identity stays on `tokens.join_group_id`; none of those
lineage fields is duplicated in the outcome row.

The schema has a partial unique index that permits multiple non-terminal rows
but allows only one terminal row per token.
