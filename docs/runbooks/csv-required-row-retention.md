# Stop when a CSV output refuses a required row

ELSPETH 0.8.9 adds the CSV sink option `require_all_rows`. Set it to the
Boolean `true` when losing any pipeline row must stop the write, such as a
required error-retention output:

```yaml
sinks:
  retained:
    plugin: csv
    on_write_failure: discard
    options:
      path: outputs/retained.csv
      collision_policy: auto_increment
      require_all_rows: true
      schema:
        mode: observed
```

When CSV preparation refuses a row because of extra columns, encoding or CSV
serialization, it raises `CSVRowRejectedError` before publishing that effect.
It removes the temporary body and preserves an existing target. The error
identifies the row ordinal and refusal category without including cell values.
This applies to pipeline members arriving by ordinary routes, source quarantine,
transform error routes or linked sink fallback. It does not change which cases
the graph routes to this output or, by itself, establish that the retained columns contain
all required source identity and reasons.

The default is `false`, which preserves existing per-row `on_write_failure`
routing and discard behavior. The routing field remains required; its values
remain `discard` or a valid sink name. A referenced fallback sink must retain
the existing no-chain configuration. The new option stops a refusal before
that routing decision, so it can be combined with `on_write_failure: discard`
without dropping the refused row and continuing.

The option requires a real Boolean. Strings, integers and null are rejected.
It belongs only to CSV pipeline-member writes; it does not add a policy to
other sink plugins or the separate audit-export path. Filesystem failures
continue to propagate through the existing effect machinery.

A refusal does not roll back earlier published effects or guarantee atomic
publication across several reports. Existing effect reservations and source
identities remain available for diagnosis and recovery; do not treat an
exception as a completed delivery or fabricate a discard outcome. Follow the
[sink-effect recovery runbook](sink-effect-recovery.md).

Existing saved graphs that omit this option keep their original options and
behavior. Graphs that explicitly use it require a supporting runtime, beginning
with 0.8.9; older runtimes can reject the unknown option. Do not strip it during
export or downgrade, because doing so weakens the required retention behavior.

## Include row identity and audit reason references

Set `include_audit_context: true` alongside `require_all_rows: true` to write
a fixed retention envelope instead of ordinary business columns. This format
requires an observed input schema, normalized headers and configured `mode: write`.
The default remains ordinary CSV. It is a pipeline-row format; audit exports
reject this option.

The physical columns, in order, are `run_id`, `row_id`, `token_id`,
`ingest_sequence`, `outcome`, `path`, `error_hash`, `primary_effect_id` and
`row_json`. The identifiers come from owned audit records, never from similarly
named business fields. `row_json` preserves the complete canonical carried row
inside one CSV cell, including nested values and heterogeneous keys. It is the
sink-boundary row, which may include transformation or linked-diversion fields;
it is not a copy of original source bytes or Python numeric spelling.

`outcome` and `path` are the recorded processing categories. `error_hash` is
the authoritative audit reason fingerprint scoped by the run and token; it
is not a unique diagnostic-record ID or the full diagnostic text. Retain the
related Landscape evidence to investigate it. Linked sink fallback includes
its primary effect ID and preserves existing diversion fields in `row_json`.
An ordinary gate route remains `success/gate_routed`; author any required
business-check explanation explicitly in the carried row. This format does
not infer one from node names, predicates or prose.

Cumulative writes recover predecessor context only from finalized,
all-accepted effects, checking the complete original membership binding.
Missing, inconsistent or unprovable context stops publication; it never
silently falls back to raw columns. Normal resume validates the exact envelope
headers. Existing local-effect reconciliation rules still apply: this option
does not regenerate a lost CSV staging body.

Technical row identity distinguishes ordinary source rows sharing a business
identifier. Token identity is a processing-event identity. The format does
not deduplicate cases, count excluded source cases, collect multiple business
failure reasons, or coordinate publication across reports. Those behaviors
must be expressed and checked separately in the workflow.
