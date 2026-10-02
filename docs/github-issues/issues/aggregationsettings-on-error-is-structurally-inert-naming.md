---
title: AggregationSettings.on_error is inert — naming a real sink makes the pipeline unbuildable
labels: [area/engine, type/bug]
---

`AggregationSettings.on_error` is a required field documented as a sink name, but naming a real
sink there makes the pipeline unbuildable. `discard` is the only value that builds, and the
engine never reads the field at all.

## Background

An ELSPETH pipeline is declared in YAML and compiled into a directed graph. Each processing
node names a sink in its `on_error` so that rows failing at that node are quarantined rather
than lost; the literal `discard` opts out of that. Aggregations are the nodes that process rows
in batches.

## What happens

Reproduced on a command-line run against a clean `git archive HEAD` export. With a real sink
named in an aggregation's `on_error`:

```
Pipeline graph error: Graph validation failed: 1 unreachable node(s) detected:
  sink_quarantine_1e71e69ffd96 (sink)
=== EXIT: 1 ===
```

The option is accepted at configuration time, required of the author, and then never wired.
Someone following the field's own documentation gets an unbuildable pipeline and an error that
names an unreachable sink rather than the aggregation option that orphaned it.

## Why

- `src/elspeth/core/config.py:658` declares `on_error: str` with the description "Sink name for
  rows that fail batch processing, or 'discard'", and validates it as a sink name.
- `src/elspeth/core/dag/builder.py` has a transform error-edge loop (`# Transform error edges`)
  and a config-gate row-error edge loop (`# Config-gate row-error edges`). There is no
  aggregation error-edge loop; the only aggregation wiring loop handles `on_success`. With no
  edge into it, the named sink is an unreachable node, and graph validation rejects it.
- `grep -n "on_error" src/elspeth/engine/executors/aggregation.py` returns no hits — the
  executor never reads the field. (Control: the same file matches 31 `def ` lines, so the empty
  result is a real absence, not a broken search.)
- All 23 aggregation `on_error` values across the `examples/` corpus are `discard`, measured by
  parsing every `examples/**/*.yaml` and reading `aggregations[].on_error`, with no file failing
  to parse and a synthetic non-`discard` value confirmed visible to the same instrument. The
  named-sink form is entirely unexercised, which is why this has survived.

## Where to start

Two files. `src/elspeth/core/dag/builder.py` wires the graph, and its existing transform
error-edge loop is the working model for what an aggregation loop would look like.
`src/elspeth/engine/executors/aggregation.py` is what would have to honour the edge at runtime.
The field itself is in `src/elspeth/core/config.py`.

**Size.** Small once the direction is chosen, but the direction is a product decision that
should be settled first. Restricting the field is roughly a field type, a description and a
test. Wiring the edge is larger: a builder loop mirroring the transform one, executor support
for routing failed batch rows, and a worked example so the path stops being unexercised.

## Impact

The only value that works is the one that routes nowhere. Because the executor never reads
`on_error`, `discard` is not an error route the engine takes; it is the value that lets the
graph build.

It also means the aggregation flush path has nowhere to send a row that fails a check during
the flush, even in principle — there is no error edge to send it down — so that is blocked
behind this.

Whoever takes this should note the tension with the project's rule that a row either leaves in
good order or is quarantined. Today an aggregation's failed rows have no sink available to
them.

## Fix

**Decide first, then build.** The two options are not variations on one fix:

1. **Wire the aggregation error edge** in `builder.py` so a named sink is reachable, and have
   the executor honour it. Correct behaviour: a pipeline whose aggregation `on_error` names a
   real sink builds and runs, and rows that fail batch processing arrive at that sink. You
   would know it holds when such a pipeline runs end to end, the failed rows are present in
   that sink, and the audit trail attributes them to the aggregation that rejected them — plus
   an `examples/` case exercising the named-sink form, so it is no longer unexercised.
2. **Restrict `on_error` to `discard`** and say so in the field description, if aggregation
   error routing is genuinely not intended. Correct behaviour: a configuration naming a sink is
   rejected at configuration time with a message that names `on_error` and says what is
   allowed — not at graph validation with an unreachable-node error. You would know it holds
   when a test feeding a sink name gets that message, and the field description no longer
   advertises a capability the engine does not have.

The present state — a required field, documented as a sink name, unbuildable when given one —
is the one arrangement that cannot be right.
