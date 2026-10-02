---
title: LLM fanout admission has no cross-node provider-call cardinality contract
labels: [area/web, type/bug]
---

Before a pipeline runs, a guard estimates how many language-model provider calls it could make and asks the operator to acknowledge the number when it is large or unbounded. The guard only ever considers transform nodes as the caller, so a model hosted on an aggregation or a collector escapes it entirely.

## Where this lives

`src/elspeth/web/execution/fanout_guard.py`, a single self-contained module of about 700 lines. `evaluate_execution_fanout_guard` is the entry point; it is called from `src/elspeth/web/execution/service.py` when a run is launched. Existing coverage is in `tests/unit/web/execution/test_llm_capability_security_gates.py` and `tests/unit/web/execution/test_service.py`.

## What happens

The guard walks the composition looking for nodes that make provider calls, traces what feeds them, and raises a risk entry when the number of calls is unbounded or above a threshold. Its subject loop skips any node whose kind is not `transform` before it even tests the node for the language-model capability. A pipeline whose provider calls originate on another plugin-bearing node kind therefore raises no risk entry — and when the risk list is empty the function returns no guard at all, so no acknowledgement is requested and nothing records that the estimate was skipped.

The module already treats those other kinds as *upstream* producers: a token-creating transform, a transform-mode aggregation, or a collector sitting upstream of a model is treated as unbounded fanout. The asymmetry is only in which kinds are allowed to be the subject.

## Impact

An operator can start a run whose provider-call volume was never estimated, and see nothing to suggest the estimate was skipped rather than favourable. The cost exposure is whatever that pipeline would have spent.

This describes the subject model as written, not an observed escape: it has not been demonstrated with a pipeline that actually makes uncounted calls. Whether one can be built today depends on which plugin kinds can host a model in practice, and confirming that is the first thing to do.

## Fix

The narrow change — widening the subject test to accept every plugin-bearing node kind — is a few lines. It is not the whole fix, and shipping it alone would trade a silent miss for a wrong number, because the guard's arithmetic assumes one call per row per node and that assumption does not hold for a barrier node that processes a whole group at once.

What needs deciding first: how a plugin declares its provider-call cardinality. The options are a per-plugin declared multiplier, an explicit unknown that forces the unbounded path, or deriving it from an existing capability declaration. That decision has not been taken, and it should be made before code is written, because it determines whether this stays inside `fanout_guard.py` or reaches into the plugin contract.

Done looks like:

1. cardinality, or an explicit unknown, is declared per plugin rather than inferred from node kind, and no plugin or kind is named literally in the guard;
2. a pipeline whose model sits on an aggregation or a collector reaches the same admission decision as the equivalent transform-hosted pipeline; and
3. a plugin-bearing node with no declared cardinality raises a risk entry marked unknown rather than being passed over silently.

## Size

Medium, and gated on the design decision above rather than on the code. Not a good first issue: it needs someone who can rule on the plugin contract. If you want the value early, a defensible smaller step is to make the guard raise an unknown-cardinality risk for any plugin-bearing node kind it cannot reason about — that removes the silent miss without pretending to a number — and leave the contract for the full fix.

## Note

This is a follow-up to earlier fanout admission work, filed rather than fixed at the time because it was judged too large to attach to the change that surfaced it.
