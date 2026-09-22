---
title: Telemetry health metrics cannot distinguish enabled-with-no-exporters from an idle run
labels: [area/engine, type/task, priority/P3]
---

When telemetry is enabled but no exporters are configured, the only acknowledgement is a single
log line at construction. The health metrics an operator reads afterwards show zeros that are
indistinguishable from a run that simply had nothing to report.

## Where to start

`src/elspeth/telemetry/manager.py`. The `HealthMetrics` typed dictionary is at `:82` and
`TelemetryManager.health_metrics` builds it at `:597`.

## Why

`handle_event` short-circuits when the exporter list is empty:

```python
# Skip if no exporters configured
if not self._exporters:
    return
```

at `:458-460`, returning before `_events_attempted` is incremented. Nothing downstream records
that a dispatch was skipped for this reason rather than for any other.

`HealthMetrics` carries `events_attempted`, `events_delivered`, `events_failed`,
`events_emitted`, `events_dropped`, `queue_drops`, `observer_failures`, `exporter_failures`,
`consecutive_total_failures`, `queue_depth`, `queue_maxsize`, `circuit_breakers` and
`exporter_delivery`. None of them names the no-exporter condition, so an operator reading a
snapshot sees every counter at zero and no in-band explanation of why.

## Impact

Minor, and bounded to observability. Someone diagnosing missing telemetry has to go back to the
startup logs to find out whether the exporter list was empty, and if those logs have rotated
there is no way to recover the answer from the running process.

## Fix

Give the condition a name in the metrics — an explicit state or counter that says telemetry is
enabled and has nowhere to send events — and keep it distinct from the telemetry-disabled
no-op path, which is a different thing and should stay a no-op.

You would know it holds when a health read from a manager with an empty exporter list names
that state, a health read from a manager with exporters configured and no traffic does not, and
a regression covering both goes red if the new field is removed.

**Size.** Small. One field, the code that sets it, and two assertions.

## Note

An earlier framing of this held that a manager could reach production with an empty exporter
list and silently ignore every event with nothing recording it. That is not the case.
`create_telemetry_manager` (`src/elspeth/telemetry/factory.py:236-242`) is the only production
constructor, and it logs `telemetry_enabled_no_exporters` unconditionally when the list is
empty. Direct construction with an empty exporter list is a shape only tests use. What survives
is the narrower gap above: the acknowledgement exists once, at construction, and never in
observable state.
