# Calibration record

One row per case, appended during the §6 calibration firing. Pre/post
floors are the `floor.pre_calibration` / `floor.post_calibration` values
committed in `scenarios/<case>/scenario.json`.

| case | pre floor | post floor | surface observed | data path observed | decision |
| --- | --- | --- | --- | --- | --- |
| canary (N=10, round `2026-08-17-calib`) | 2 | 2 (reachable: 8/10 at floor) | `compose_loop` 10/10 | blob + interpretation review 10/10 — **not** `source.inline_blob` | instrument PASSES; corpus defect found, see below |
| (remaining 18 — not yet fired) | | | | | |

## Canary, 2026-08-17 (round `2026-08-17-calib`, backend restarted at 15:23 AEST onto `b7dd59c83`, frontend `index-IcB8V6Qh.js`)

**Instrument verdict: healthy.** 10/10 runs captured with zero exclusions;
`surface_observed == compose_loop` 10/10; exactly one advisor call per run on
`openrouter/anthropic/claude-opus-4-8` with a null `tools_spec_hash`, and
`other_text_calls == 0` in every run (the currency discriminator holds on the
live wire, not just in the fixture builder). Wall 26–36 s/run, $0.19/run,
$2.05 for the block. The taxonomy behaved: the two above-floor runs surfaced
`unattributed_excess` rather than going silent.

**Floor: reachable, but decomposed differently than derived.** 8/10 runs used
exactly 2 tool-bearing calls. The derivation says `discovery 1 + mutation 1`;
the observed 2-call path is `set_pipeline` + `request_interpretation_review`
with **no discovery turn at all**. Total unchanged, so no floor change is
warranted — but the `derivation` prose is wrong for this case and the
`must_discover_schema_before_first_mutation` green criterion fails 9/10 as a
direct consequence (see Decision 2).

**Data path: blob + review, corpus-wide.** Every run routed the invented rows
through a blob (`source.blob_ref`, `source_authoring.modality:
llm_generated`) resolved by one `request_interpretation_review` round, not
through `source.inline_blob` inside the mutation as Decision 10 assumes. Per
the runbook this is a **kit finding, not a floor change** — the floor total
already accommodates it. The driver's review loop resolved it in 1 round.

### Decision 1 — canary oracle is wrong; the composer is right (OPEN, needs the operator)

`wrong_shape` fired 10/10: the oracle expects `source → passthrough →
output` (3 topology nodes); the composer built `source → output` (0 nodes)
every time. Reading the prompt ("read a made-up colour list and write it back
out as JSON unchanged"), **no transform is needed and the composer's pipeline
is the better one**. The `passthrough` node exists in the oracle only because
`validate_canonical_arguments` used to require ≥1 node — the rule that was
relaxed on 2026-08-17 (3b fix round 1) precisely because it forced an
artificial node into `llm_source`. The canary inherited the same artifact.

Recommended: rewrite `scenarios/canary/scenario.json`'s
`canonical_arguments` as a zero-node `source(json) → output(json)` payload,
regenerate `expected_topology`, and keep the floor at 2. Structural reason,
recorded pre/post: the corpus payload asserted a node the task does not
require. Until this lands the canary can never go optimal, so it cannot serve
as the instrument-alive check.

### Decision 2 — is schema discovery before the first mutation a requirement? (OPEN, needs the operator)

`must_discover_schema_before_first_mutation` failed 9/10: the composer
authored `set_pipeline` directly for `json`/`json`, without reading a plugin
schema first. That is arguably the *straightest path* for plugins the model
already knows, so the criterion as written penalises the behaviour the
battery exists to reward. Options: (a) keep it green-critical corpus-wide
(then the canary's floor derivation should say `discovery 0 + mutation 1 +
review 1`); (b) make it advisory for trivial-plugin cases; (c) drop it and
rely on `excess_discovery` to catch the opposite failure. Not changed
unilaterally — it alters what the instrument measures.

Probe reading (§7): (not yet fired — `--probe`).
