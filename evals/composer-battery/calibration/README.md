# Calibration record

One row per case, appended during the §6 calibration firing. Pre/post
floors are the `floor.pre_calibration` / `floor.post_calibration` values
committed in `scenarios/<case>/scenario.json`.

Substrate for every block below: `elspeth-web.service` restarted 2026-08-17
15:23 AEST onto `b7dd59c83`, frontend `index-IcB8V6Qh.js`, composer
`openrouter/anthropic/claude-sonnet-5`, advisor
`openrouter/anthropic/claude-opus-4-8`.

| case | pre floor | post floor | surface observed | data path observed | decision |
| --- | --- | --- | --- | --- | --- |
| canary | 2 | 2 (reached in 10/30 runs across three blocks) | `compose_loop` 30/30 | blob + one interpretation-review round, 30/30 — **never** `source.inline_blob` | oracle rewritten twice; threshold question OPEN (Decision 3) |
| field_drop (new case) | 2 | not set — 2 is not reachable as authored | `compose_loop` 10/10 | blob + review; `create_blob` detour in 2/10 | KEEP as a case: it is the strongest kit signal in the corpus so far |
| (remaining 18) | | | | | not yet fired |

## Instrument verdict: HEALTHY

Across all three N=10 blocks (30 live runs): **zero exclusions**,
`surface_observed == compose_loop` 30/30, exactly one advisor call per run on
the advisor model with a null `tools_spec_hash`, and `other_text_calls == 0`
in every run. The currency discriminator, the capture parser, the review
loop, the durable-pair projection and the deviation taxonomy all behave on
the live wire, not just against the fixture builder. Cost ≈ $0.19–0.30/run;
wall 26–125 s/run.

## Block 1 — canary as authored (`2026-08-17-calib`): oracle defect

`wrong_shape` 10/10. The oracle expected `source → passthrough → output`;
the composer built `source → output` with zero nodes, every run. The prompt
("read a made-up colour list and write it back unchanged") genuinely needs no
transform — the composer was right and the payload was wrong. The
`passthrough` node was an artifact of the old ≥1-node rule in
`validate_canonical_arguments`, relaxed earlier the same day for
`llm_source`. **Fixed**: canary payload is now zero-node
`source(json) → output(json)`.

Floor reachable: 8/10 at exactly 2 tool-bearing calls — but by
`set_pipeline` + `request_interpretation_review`, with **no discovery turn**,
not the derived `discovery 1 + mutation 1`. Derivation prose corrected.

## Block 2 — `field_mapper` canary (`2026-08-17-calib2`): promoted to its own case

Prompt: two-field invented list, one field dropped on the way out.
**Topology correct 10/10** (`field_mapper` built every time, `wrong_shape`
never fired) — so the prompt reliably elicits the intended shape. But
0/10 clean: `schema_fumble` in 6 runs (up to 3 `get_plugin_schema` reads then
repeated `patch_node_options` against the same node), plus `repair`,
`data_setup_detour`, `abandoned_mutation`, and one run ending `not is_valid`.
Excess up to 8 over a floor of 2; cost $3.04 for the block vs $2.05.

**Decision: keep it as a corpus case (`field_drop`), not as the canary.** It
is a genuine affordance-kit finding — authoring a single `field_mapper` node
is hard for this kit — and that makes it valuable as a case and useless as an
instrument-alive check. Roster is now 20 (18 strata + canary + field_drop).

## Block 3 — trivial canary re-fired (`2026-08-17-calib3`): the real finding

Same prompt as block 1, corrected zero-node oracle,
`must_discover_schema_before_first_mutation: false` for this case (trivial
well-known plugins; skipping discovery is the straightest path, and the
canary's job is to prove the instrument alive, not to police discovery).

Result: **2/10 clean and optimal**, floor distribution 2×2, 4×3, 3×4, 1×5,
with `data_setup_detour` ×3, `backtrack`, `repair`, `unattributed_excess` ×4,
and one run that built a **csv** source instead of json (`wrong_shape` on the
source plugin).

Block 1 and block 3 ran the same prompt against the same kit and produced
8/10-at-floor and 2/10-at-floor respectively. **The finding is path variance,
not a corpus defect**: on a task with a single reasonable shape, this kit's
path length varies 2–5 calls and its plugin choice varies. That is precisely
what the battery exists to measure — but it means the spec's canary rule
("expect ≥ 9/10 optimal; otherwise the instrument is wrong") does not hold
for any canary design tried, and would fire as a false instrument alarm.

Prompt tightened after block 3 (not yet re-fired): the input format is now
stated ("sitting in a JSON file"), which should remove the csv/json source
variance seen in run 10.

### Decision 3 — the canary threshold (OPEN, needs the operator)

Recommended: restate the canary rule as an **instrument** check, not an
optimality check — e.g. "0 exclusions, `compose_loop` 10/10, `other_text_calls`
0, and ≥1 run at floor" — and let optimality be measured by the corpus. As
written, spec §6's ≥9/10 would abort calibration on a healthy instrument.

### Decision 2 (carried from block 1) — schema discovery before the first mutation

Set to `false` for the canary only, with the reason recorded above. Whether
it stays green-critical for the other 19 cases is unresolved: the composer
skipped discovery on trivial plugins and fumbled it on `field_mapper`, so the
criterion may be measuring plugin familiarity rather than path quality.

Probe reading (§7): (not yet fired — `--probe`).
