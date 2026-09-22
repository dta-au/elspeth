---
title: AggregationSettings.on_error is inert — naming a real sink makes the pipeline unbuildable
labels: [area/engine, type/bug]
---

`AggregationSettings.on_error` is a required field documented as a sink name, but naming a
real sink there makes the pipeline unbuildable. `discard` is the only value that builds, and
the engine never reads the field at all.

## What happens

Reproduced on a command-line run against a clean `git archive HEAD` export. With a real sink
named in an aggregation's `on_error`:

```
Pipeline graph error: Graph validation failed: 1 unreachable node(s) detected:
  sink_quarantine_1e71e69ffd96 (sink)
=== EXIT: 1 ===
```

The option is accepted at configuration time, required by the model, and then never wired.
An author following the field's own documentation gets an unbuildable pipeline and an error
that names an unreachable sink rather than the aggregation option that orphaned it.

## Why

- `src/elspeth/core/config.py:658` declares `on_error: str` with the description "Sink name
  for rows that fail batch processing, or 'discard'", and it is validated as a sink name.
- `src/elspeth/core/dag/builder.py` has a transform error-edge loop (`# Transform error
  edges`) and a config-gate row-error edge loop (`# Config-gate row-error edges`). There is
  no aggregation error-edge loop; the only aggregation wiring loop handles `on_success`.
- `grep -n "on_error" src/elspeth/engine/executors/aggregation.py` returns no hits — the
  executor never reads the field. (Control: the same file matches 31 `def ` lines, so the
  empty result is a real absence and not a broken search.)
- All 23 aggregation `on_error` values across the `examples/` corpus are `discard`, measured
  by parsing every `examples/**/*.yaml` and reading `aggregations[].on_error`, with no file
  failing to parse and a synthetic non-`discard` value confirmed visible to the same
  instrument. The named-sink form is entirely unexercised, which is why this has survived.

## Impact

The only value that works is the one that routes nowhere. Because the executor never reads
`on_error`, `discard` is not an error route the engine takes; it is the value that lets the
graph build.

This is also why the aggregation-flush seam has nowhere to route a trust-boundary violation
even in principle: a routing fix there needs this wiring to exist first.

Whoever takes this should note the tension with the project's rule that a row either leaves
in good order or is quarantined. Today an aggregation's failed rows have no sink available
to them.

## Fix

Either wire the aggregation error edge in `builder.py` so a named sink is reachable and the
executor honours it, or — if aggregation-level error routing is genuinely not intended —
make `on_error` accept only `discard` and say so in the field description, so the option
stops advertising a capability the engine does not have. The present state (a required field,
documented as a sink name, unbuildable when given one) is the one arrangement that cannot be
right.
