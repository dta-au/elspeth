---
title: LLM fanout admission has no cross-node provider-call cardinality contract
labels: [area/web, type/bug]
---

Execution fanout admission only ever considers transform nodes as LLM subjects, so an LLM hosted on an aggregation or a collector escapes the guard entirely.

## What happens

Before a pipeline runs, the fanout guard estimates how many provider calls the composition can make and requires an acknowledgement when that number is unbounded or high. It selects its subjects by node kind: the loop in `evaluate_execution_fanout_guard` in `src/elspeth/web/execution/fanout_guard.py` skips any node whose `node_type` is not `"transform"` before testing it for the LLM plugin capability. A pipeline whose provider calls originate on another plugin-bearing node kind produces no risk entry and no acknowledgement prompt.

The same module already treats those other kinds as *upstream* fanout producers — a token-creating transform, a transform-mode aggregation or a collector upstream of an LLM is treated as unbounded. The asymmetry is only in which nodes can be the LLM subject.

## Impact

An operator can start a run whose provider-call volume was never estimated, because the node making the calls is not a transform. Such a pipeline produces no risk entry, and `evaluate_execution_fanout_guard` returns no guard at all when the risk list is empty, so no acknowledgement is requested and nothing records that the estimate was skipped.

Note that this describes the subject model, not an observed escape — it has not been demonstrated with a pipeline that actually makes uncounted calls.

## Fix

Define provider-call cardinality, or an explicit unknown multiplier, across all plugin-bearing node kinds rather than transforms alone, and drive resource and fanout admission from that contract. The classification should come from the plugin's declared capability and cardinality, not from a hardcoded list of plugin names or node kinds.

Done looks like: a pipeline whose LLM sits on an aggregation or a collector produces the same admission decision as the equivalent transform-hosted pipeline, and a plugin-bearing node kind with no declared cardinality raises a risk entry marked unknown rather than being passed over.

## Limits

This is a follow-up to earlier fanout admission work and is estimated beyond the allowance for a fix made alongside a related change; it needs scheduling in its own right.
