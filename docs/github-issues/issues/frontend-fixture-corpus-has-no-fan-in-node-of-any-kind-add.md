---
title: Frontend e2e fixture corpus contains no fan-in node, so fan-in rendering is never exercised
labels: [area/composer, area/tests, type/task]
---

The shared end-to-end workspace fixture declares exactly one node kind, so no visual or e2e run ever renders a fan-in node. A fan-in rendering defect can pass every frontend gate.

## The gap

`src/elspeth/web/frontend/tests/e2e/helpers/workspace-fixtures.ts` contains exactly one `node_type` occurrence, `"transform"` (line 167). There is no `coalesce`, no `row_union`, no `queue`, no `collector`, and no gate with `fork_to`. Consequently none of the 11 PNG baselines in `src/elspeth/web/frontend/tests/e2e/composer-workspace.visual.spec.ts-snapshots` renders a fan-in node.

This is why a defect in which a coalesce rendered with half its inbound edges missing could sit unnoticed: every visual gate stayed green, and it was caught only once someone wrote a targeted unit test by hand.

## Do not read the saved-session counts as "row_union is dead"

A scan of 666 saved composition states found 38 `coalesce` nodes and no `row_union`. An early reading of that count framed the row_union paths as inert; that reading was wrong and is withdrawn. row_union is reachable, taught and load-bearing:

- The planner authoring aid `fork_row_union_exemplar_args` in `src/elspeth/web/composer/planner_authoring_aids.py` ships an exemplar carrying `"node_type": "row_union"` (line 2639), so the planner is actively briefed to author one.
- `examples/row_union_ab_experiment/` is a dedicated example with five settings variants, `examples/ab_llm_experiment/` is a second, and eight example YAML files carry a `row_unions:` block.
- `src/elspeth/web/composer/authority_hashing.py` has row_union-specific handling in both the projection and restore paths: `branches` is projected to an ordered pair array because it relies on mapping insertion order, which RFC 8785 key sorting would destroy.
- The workspace offers an Import YAML action, so any of those example pipelines can reach the UI directly.

Zero instances in the saved corpus is a coverage gap, not evidence of disuse.

## Scope

1. Add a fan-in shape to `workspace-fixtures.ts`: at minimum a `coalesce` with a two-alias `branches` map (the shape the planner produces) **and** a `row_union` with the same (the shape the examples and the planner exemplar produce). Both kinds, because they take different code arms — the inbound path is shared via `FAN_IN_NODE_TYPES` (`src/elspeth/web/frontend/src/lib/graphTopology.ts:165`), while the outbound-semantics rewrite in `GraphView.tsx` is deliberately row_union-only (the exclusion is recorded at lines 1902 and 2111). A coalesce fixture alone leaves the second arm uncovered; a row_union fixture alone leaves the widened inbound arm unverified end to end.
2. Re-render the affected visual baselines and review the diffs by eye rather than accepting them blind — a new node changes layout.
3. Consider carrying the `queue` and `collector` kinds while the file is open; `collector` appears 66 times in the saved corpus and is likewise absent from the fixtures.

## Already covered elsewhere

The engine-level scenario corpus already covers this axis — `tests/fixtures/dag_scenario_corpus/schema.py:86` declares `("row-union-interleave", …)`, exercised by `tests/integration/core/dag/test_dag_scenario_production_path.py`. This task is about the frontend fixture corpus, which has no equivalent; do not duplicate the engine coverage.

## Verification

Mutation-check the new coverage: re-narrow `FAN_IN_NODE_TYPES` to `row_union` only and confirm an e2e or visual run turns red. The unit assertion at `src/elspeth/web/frontend/src/lib/graphTopology.test.ts:62` will fail regardless; the thing being checked is whether the *fixture-driven* run fails. If it does not, the fixture was added but is not actually asserted on.
