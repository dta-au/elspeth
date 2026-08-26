# Session 75cec2b2 — investigation pointer (superseded by Filigree)

Investigated 2026-08-26:
`https://elspeth.foundryside.dev/#/75cec2b2-fa31-43a3-89b8-505816caaa29`
(composition v22, run `6f32968e-6067-42ea-9f8d-9177e5894f26`, completed 10/10/0).
Pipeline: csv source → gate `fan_out` → two `transform:llm` → `coalesce` →
`output:csv`. The run was correct; the written CSV carries all seven columns
with correct values. All four findings are presentation or declaration defects.

Full detail now lives in the tracker — read it there, not here:

| | Finding | Issue | State |
|---|---|---|---|
| A | Run-output preview ignored CSV quoting: any comma in a value shredded the row and padded the header with blank columns. Fired on every LLM pipeline via the `_usage` dict repr. | `elspeth-7f1e148ed6` | FIXED 280cc5564, live-verified. Residual: the backend row cap still counts physical lines. |
| B | GraphView draws no inbound edge for a coalesce's non-first branch — phase 1 enumerates `branches` only for `row_union`. Missing from the a11y text alternative too. | `elspeth-625e85c59b` | FIXED 3726d811a, live-verified. Inbound sites widened to `FAN_IN_NODE_TYPES`; the **outbound** rewrite is deliberately NOT widened and a test guards that. |
| C | Spec tab dropped a coalesce's `branches`/`policy`/`merge`. | `elspeth-59684fb0c8` | FIXED 280cc5564, live-verified. `condition` deliberately still excluded (pinned by an existing test). |
| D | Composer authors no `guaranteed_fields` downstream of the source; there is no DAG-wide propagation, and observed mode locks the sink's columns to row 1's keys. | `elspeth-15b400881f` | OPEN, DEFERRED past the demo by operator ruling 2026-08-26. Intent confirmed: `guaranteed_fields` is a **contract for the coordinator**, so a display-only slice does not close it. The derivation already exists and runs — see the ticket comment. |

## Corpus facts established while fixing B

Across all 666 rows of `composition_states`: **38 `coalesce` nodes and zero
`row_union`.** The composer only ever authors `coalesce`, so every
`row_union`-scoped path in `GraphView.tsx` had never fired on a real pipeline.
All 38 coalesces are alias-mapped; the 20 states that also materialise explicit
inbound edges write them with `label: null`, and no state has two aliases
naming one connection.

The hazard that shaped the fix: one saved state holds an explicit
`merge_branches -> tidy_columns` edge on a coalesce whose `on_success`,
`on_error` and `routes` are **all null**. The outbound-semantics rewrite drops
any unclaimed explicit hint whose source is a fan-in node, so widening it to
`coalesce` would have erased a working connection.

## Evidence commands

```bash
sqlite3 -readonly data/sessions.db \
  "select nodes from composition_states
   where session_id='75cec2b2-fa31-43a3-89b8-505816caaa29' and version=22;" | python3 -m json.tool
cat data/outputs/75cec2b2-fa31-43a3-89b8-505816caaa29/colours_with_pairings.csv
```
