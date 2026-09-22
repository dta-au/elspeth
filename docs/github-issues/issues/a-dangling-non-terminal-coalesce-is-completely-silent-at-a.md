---
title: Dangling-coalesce detection rests on an unguarded coupling and has no dedicated error code
labels: [area/composer, type/bug, needs-triage]
---

A coalesce node whose output nothing consumes is reported only by a generic medium-severity warning, and the check that detects it depends on an invariant no test pins.

## Where this lives

The Composer is the authoring surface that builds a pipeline graph and validates it before it runs. Its validation pass lives in `src/elspeth/web/composer/state.py` — one large module holding the composition state and a `validate()` method that returns errors and warnings. Everything below is in that file, plus one helper module beside it.

Two terms used throughout: a **connection** is a named edge between nodes; a **coalesce** is the node kind that merges several branches of a fork back into one stream. A coalesce publishes its output under its own id without being told to, which is why it can dead-end without anything obviously missing.

## What happens

When a fork/coalesce group is authored with nothing naming the coalesce, its rows go nowhere. The Composer reports this only as the generic warning "Node 'X' has no outgoing edges — its output is not connected to any downstream node or sink", at medium severity.

A queue in the same position gets a dedicated high-severity error, `queue_no_consumer` (`src/elspeth/web/composer/state.py:8660`). There is no `coalesce_no_consumer` counterpart anywhere in the tree.

Coalesce is the node kind most exposed to this. An aggregation cannot reach the dangling case, because a separate check (`aggregation_missing_on_error`) forces it to declare error routing first; a queue has its own error. Coalesce is the only one of the three with no such requirement, so it is served by the weakest signal.

## Why

The check is `_runtime_consumer_connections` (`src/elspeth/web/composer/state.py:2346`), which collects the connection names the runtime can resolve to a consuming node. It deliberately ignores `node.input` for `coalesce` and `row_union`, reading their declared branch values instead.

For a *branchless* coalesce or row_union, that would leave a real consumer invisible and the check would miss a genuine problem. Today that state cannot arise, because `branches` is mandatory for exactly those two kinds. So the exclusion is safe only by virtue of a rule enforced somewhere else, and it reads like a free choice. Searching `tests/` returns no reference to `_runtime_consumer_connections`, so if the mandatory-`branches` rule were ever relaxed, this check would break silently with nothing going red.

Severity is bounded by a later gate: graph construction still fails closed with "Dangling output connections with no consumer" (`src/elspeth/core/dag/graph.py:602-608`). So the cost is authoring-time feedback deferred to run time, not silent data loss.

## Fix

**Size: small in code, but one decision is needed before starting.** Whether a dead-ended coalesce should block authoring (an error) or keep warning is a product call, not something to infer from the code. The rest follows once that is settled.

Correct behaviour, assuming the error reading:

1. A coalesce whose published connection appears in no node's input and in no sink name is rejected at authoring time with a `coalesce_no_consumer` error, mirroring `queue_no_consumer` in severity and message shape.
2. The coupling in `_runtime_consumer_connections` is checked rather than assumed.

You would know both hold by: a test that authors a fork/coalesce group with nothing naming the coalesce and asserts the new error code is returned; and a test asserting `branches` is mandatory for `coalesce` and `row_union`, referenced from a comment at the exclusion so the connection is discoverable. The second test should fail if the mandatory-`branches` rule is removed — check that by temporarily relaxing it, not by assuming.

## Note

This issue was raised against an earlier state of `state.py` and only partly reproduces today. The original report also described a completely silent dangling coalesce (zero errors and zero warnings), several hand-written restatements of the self-publishing node kinds, and an unguarded helper. The code at those locations has since changed shape: the check now tests whether the published connection is consumed, the error-routing limb no longer counts `"discard"`, and the constant listing the self-publishing kinds now lives in `src/elspeth/web/composer/_producer_resolver.py` with tests importing it.

Only the two items under **Fix** were reproduced against the current tree. Anyone picking this up should not work from the original description without re-triaging it.
