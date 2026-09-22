---
title: Frontend e2e fixture corpus contains no fan-in node, so fan-in rendering is never exercised
labels: [area/composer, area/tests, type/task]
---

The shared end-to-end test fixture for the Composer workspace declares exactly one kind of node, so no browser test ever renders a fan-in node — one that merges rows arriving from several upstream branches. A fan-in rendering defect passes every frontend gate.

**Where this lives.** `src/elspeth/web/frontend/tests/e2e/helpers/workspace-fixtures.ts` is the fixture every Playwright test of the Composer workspace builds its pipeline from. The rendering under test is `src/elspeth/web/frontend/src/components/inspector/GraphView.tsx`.

## The gap

`workspace-fixtures.ts` contains exactly one `node_type` occurrence, `"transform"` (line 167). There is no `coalesce`, no `row_union`, no `queue`, no `collector`, and no gate with `fork_to`. Consequently none of the 11 PNG baselines in `src/elspeth/web/frontend/tests/e2e/composer-workspace.visual.spec.ts-snapshots` renders a fan-in node.

That is why a defect in which a coalesce rendered with half its inbound edges missing could sit unnoticed: every visual comparison stayed green, and it was caught only once someone wrote a targeted unit test by hand.

## Do not read the saved-session counts as "row_union is dead"

A scan of the saved-sessions database — 666 stored composition states — found 38 `coalesce` nodes and no `row_union`. An early reading of that count treated the row_union code paths as dead. That reading was wrong and is withdrawn. row_union is reachable, taught and load-bearing:

- The Composer's planner (the LLM that authors pipeline structure) is handed an exemplar for it: `fork_row_union_exemplar_args` in `src/elspeth/web/composer/planner_authoring_aids.py` carries `"node_type": "row_union"` at line 2639.
- `examples/row_union_ab_experiment/` is a dedicated example with five settings variants, `examples/ab_llm_experiment/` is a second, and eight example YAML files carry a `row_unions:` block.
- `src/elspeth/web/composer/authority_hashing.py` has row_union-specific handling in both its projection and restore paths: `branches` is projected to an ordered pair array because it relies on mapping insertion order, which RFC 8785 key sorting would destroy.
- The workspace offers an Import YAML action, so any of those example pipelines can reach the UI directly.

No instances in saved sessions is a coverage gap, not evidence of disuse.

## Scope

1. Add a fan-in shape to `workspace-fixtures.ts`: at minimum a `coalesce` with a two-alias `branches` map (the shape the planner produces) **and** a `row_union` with the same (the shape the examples and the exemplar produce). Both kinds, because `GraphView` draws the edges into a node and the edges out of it with different code. The inbound path is shared through `FAN_IN_NODE_TYPES` (`src/elspeth/web/frontend/src/lib/graphTopology.ts:165`, currently `{coalesce, row_union}`), while the outbound rewrite is deliberately row_union-only — the exclusion and its reasoning are recorded at `GraphView.tsx:1902` and `:2111`. A coalesce fixture alone leaves the outbound code untested; a row_union fixture alone leaves the shared inbound path unverified end to end.
2. Re-render the affected visual baselines and review the diffs by eye rather than accepting them blind — a new node changes layout, so every baseline will differ and a real regression can hide in that churn.
3. Consider carrying the `queue` and `collector` kinds while the file is open; `collector` appears 66 times in the saved sessions and is likewise absent from the fixtures.

## Already covered elsewhere

The engine-level scenario corpus covers this axis already — `tests/fixtures/dag_scenario_corpus/schema.py:86` declares `("row-union-interleave", …)`, exercised by `tests/integration/core/dag/test_dag_scenario_production_path.py`. That is the engine, not the browser. Do not duplicate it here.

## Fix

Done means a fan-in rendering defect cannot reach a merge: mutation-check it by re-narrowing `FAN_IN_NODE_TYPES` to `row_union` only and confirming an end-to-end or visual run turns red. The unit assertion at `src/elspeth/web/frontend/src/lib/graphTopology.test.ts:62` fails on that mutation regardless — the thing being checked is whether the *fixture-driven browser run* also fails. If it stays green, the fixture was added but nothing asserts on it, and the gap is still open.

Size: one contained fixture edit, then a baseline re-render that needs unhurried eye review. Closer to a day than an hour, and no design decision is needed first.
