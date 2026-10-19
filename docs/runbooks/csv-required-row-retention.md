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

`outcome` and `path` are producer-declared processing categories. Landscape
finalizes the corresponding token outcomes after CSV publication. `error_hash` is
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

## Require a readable recorded failure cause

In a supporting 0.8.9 runtime, add `include_error_reason: true` to the audit
envelope configuration to append an `error_reason` column. This strict Boolean
option requires `include_audit_context: true` and its existing restrictions.
Omitting it or setting it to false preserves the original envelope and identity
recipe. Older runtimes reject the new option; do not remove it to make a
required-retention workflow run on an older version.

The readable cause comes from the exact durable source-quarantine or routed
transform-failure handoff for that run, row, token, sink, outcome and error
reference. It is not generated business prose or a dump of the traceback.
The display is limited to 2,048 characters, with truncation marked; the full
recorded message remains bound by a separate witness digest. The original
`error_hash` continues to identify the originating message, not the shortened
display. Recovery verifies both the original membership and the selected cause.

Missing, ambiguous, inconsistent or credential-redacted causes stop the write
explicitly. This mode also refuses linked sink diversions because their
recoverable evidence may contain only hashes. It does not substitute an upstream
failure, a path classification or a hash placeholder for that sink failure's
cause. Ordinary successful gate routes are not supported by this error-only
mode; put business-check explanations explicitly in the carried row.

An inability to write this required report aborts processing rather than
silently discarding the case and continuing. Earlier publications are not
rolled back. Enabling the option does not reinterpret existing pending effects
or upgrade an old CSV in place: incompatible target, configuration or membership
bindings refuse before publication. Resume requires the exact extended headers
and provable predecessor evidence.

## Require a readable failure or gate-routing explanation

Use `include_disposition_reason: true` instead of `include_error_reason` when
the same required CSV receives both failures and ordinary Boolean gate routes.
This separate strict Boolean requires the same audit-envelope configuration;
the two readable modes are mutually exclusive. It adds `disposition_kind` and
`disposition_reason` after the original envelope columns. Existing omitted,
false and error-only configurations keep their original representation.

Actual source/transform failures use the same verified cause as the error-only
mode, with kind `error`. A successfully evaluated Boolean gate uses kind
`gate_route` and a factual explanation such as “Gate quality_check selected
false and routed this row to retained.” Its outcome remains
`success/gate_routed`, and its error reference remains empty. This explanation
does not claim which particular field failed or supply a business diagnosis.

The gate mode requires one exact durable sink handoff and one unique completed
gate decision whose unchanged row, run-scoped configuration, MOVE edge,
Boolean result and retained routing-reason payload agree. It never re-evaluates
the predicate or guesses from the sink name or scheduler cursor. Missing,
contradictory or multiple selecting decisions stop preparation, including
ambiguous retry histories. String-valued routes and configured names that
cannot safely be disclosed are unsupported. Full evidence remains bound even
though the displayed explanation omits the predicate and row values.

Cumulative writes and finalized retries re-verify each member's original
error or gate witness, including batches that mix the two kinds. Changing the
mode cannot upgrade an older pending effect or overwrite an existing artifact
under a different identity. Linked sink diversions still refuse because this
mode does not create a missing durable sink-write cause. Earlier reports are
not rolled back. Count distinct excluded source cases separately; neither
this explanation nor the engine's failure counter implements that count.
