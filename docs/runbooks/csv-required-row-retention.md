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
the graph routes to this output or establish that the retained columns contain
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
