# Composer Path-Quality Battery — Design

**Date:** 2026-08-13 (rev 1) · rev 2/3 2026-08-16 · **rev 4 2026-08-16**
**Status:** Rev 4 — **approved** (two panel reviews + the Decision 7 panel,
`2026-08-16-composer-battery-design-review.md`); pre-implementation
**Home:** `evals/composer-battery/` (new), reusing `evals/lib/` and the
`evals/composer-parity/` fixture format
**Owner:** the ELSPETH maintainer; the corpus is curated by whoever edits
the affordance kit, in the same change.

**Errata (2026-08-17, post-implementation):**
(a) §5's "MDE" is, as implemented, a one-sample 95% CI half-width
(`ci_half_width_pp`) — read every per-case interval as "95% CI ±X pp
(one-sample half-width; a two-round delta needs more)", not as a minimum
detectable effect.
(b) §2's "Graphs are ≤ 12 nodes" does not hold for the shipped corpus:
`deep_routing` is 16 nodes. Isomorphism is still cheap at that size —
measured 2026-08-17 on the maintainer's machine, 5-repetition
`time.perf_counter()` median via `topologies_match()` on `deep_routing`'s
scenario: match ≈0.00005 s, worst-case rejection (node kind/plugin
multiset held equal to `expected`, edge structure forced non-isomorphic by
collapsing one gate's two routes onto the same output) ≈1.21 s —
re-measure at calibration.
(c) A new named v1 blind spot beside §1's multi-source one: the compose
surface cannot author repo-relative asset files, so `template_lookups`'
template and lookup files are inlined as a `prompt_template`, and its
`expected_topology` is identical to `openrouter_sentiment`'s — v1 measures
the same shape twice under two case names.
(d) Gate thresholds are pinned by a `condition_literal` membership
assertion (the numeric literal must be present somewhere in the graph's
gate `condition`s), because thresholds live in `condition`, not `options`;
on a multi-gate case this confirms presence, not which gate carries the
value.

**Errata (2026-08-17, post-calibration — ruled by the operator after 30 live
canary runs; evidence in `evals/composer-battery/calibration/README.md`):**
(e) §6's canary rule ("expect ≥ 9/10 optimal, otherwise the instrument, not
the corpus, is wrong") is **withdrawn**. Two prompts and three N=10 blocks
against a healthy rig produced 8/10, 0/10 and 2/10 at floor; the same prompt
scored 8/10 and 2/10 on consecutive blocks. The kit's path length varies
2–5 calls on a single-shape task, so an optimality threshold on ten runs
reads variance as an instrument fault. The canary asserts the **instrument**:
zero exclusions, `surface_observed == compose_loop` 10/10,
`other_text_calls == 0`, and at least one run at floor. Optimality is what
the corpus measures, pooled, with `n` beside it.
(f) `must_discover_schema_before_first_mutation` is **false in every
scenario**. Observed: the composer authors `set_pipeline` directly for
plugins it knows (json→json) and fetches `get_plugin_schema` up to three
times for one it does not (`field_mapper`), so the criterion tracks plugin
familiarity, not path quality — and it marked the straightest observed path
non-green. The failure it was meant to catch (authoring blind, then
patching) is already measured by outcome rather than ritual:
`schema_fumble`, `repair` and `excess_discovery` fire on the consequences in
either direction. The per-case switch remains, so any case can re-enable it.

## Purpose

The Web Composer is an LLM tool loop: an operator states what they want, the
model builds it with tool calls. Success alone is a weak signal — the model
can reach a working pipeline while struggling (repairs, backtracking,
redundant discovery) some fraction of the time. This battery measures, for a
fixed corpus of operator-voice prompts, whether the composer takes the
**straightest path** to the answer, and when it does not, produces the
evidence needed to triage each deviation into:

- **kit defect** — our tool descriptions, schemas, skill text, or guidance
  (the *affordance kit*) gave incorrect or ambiguous direction; or
- **hard problem** — the case is intrinsically difficult.

Each case is *constructed to have a known optimal path*. Deviations from that
path — not failures — are the primary datum.

**Non-goal (Goodhart guard).** The floor measures the straightest path *for
this corpus's one-shot prompts on the compose-loop surface*. It is not a
prescription for composer behaviour with iterating operators, and a battery
delta is never on its own sufficient justification for a kit edit that
reduces discovery or clarification behaviour outside the corpus. Kit edits
motivated by the ledger must be argued from the evidence bundle, not the
rate. The report prints this caveat in its header.

**Success criteria for the instrument.** Keep it while (a) every deviation it
reports carries evidence a reader can act on without opening a raw
transcript; (b) the calibration firing and the first measurement firing
agree within the stated MDE on the pooled rates; and (c) **it discriminates**:
across those two firings the ledger attributes deviations to *both* classes
— at least one kit defect and at least one hard problem, each with an
evidence bundle. All-"hard problem" means the corpus does not probe real
ambiguity; all-"kit" means the floors are probably mis-derived; either way
the instrument is re-cut, not re-tuned. Kit edits argued from an evidence
bundle (never from the rate) should be followed by a measured drop in that
case's deviation rate of at least the MDE. If the tripwire never fires across
the first several firings, question it at the v2 review — a judgement call,
not a calendar rule.

## Relationship to existing harnesses

| Harness | Question it answers |
| --- | --- |
| `evals/composer-harness/` | Does the composer succeed across personas? (May 2026 15-scenario sweep) |
| `evals/composer-rgr/` | Did this skill edit fix a known failure mode? (fast single-scenario iteration; corpus deliberately untracked) |
| `evals/composer-parity/` | Do the three authoring surfaces derive equivalent graphs from one intent? (tracked; ten canonical topology fixtures with validated `set_pipeline` payloads) |
| **`evals/composer-battery/`** | **How far from optimal is the compose-loop trajectory, and whose fault is a deviation?** |

The battery imports, not forks: `evals/lib/composer_rgr_score.py`
(tool-row parsing, red/green criteria — the helpers are private today and
get public names when imported), the **parity fixture format**
(`intent` / `canonical_arguments` / `semantic_expectations`) as the oracle
contract, and `evals/lib/scenario_from_example.py` as a cross-check only.
API choreography lessons come from `ops-local/acceptance/drive_graph.py` and
`scripts/acceptance_battery.py` — **with one deliberate departure**: those
scripts are register-primary and do not hard-fail on a missing
`access_token` (the elspeth-2e5086dce6 bug); the battery driver is
login-only and hard-fails.

**Deliberate divergence from composer-rgr precedent:** the battery corpus,
driver, scorer and report are **tracked in git**. Prompts are the
instrument's calibration; comparability requires versioned bytes. Every
predecessor harness that left its driver untracked rotted
(`136f2c703`; `ops-local/acceptance/` has no history at all).
`battery_score.py` gets a unit-test import dependency for the same reason
`evals/lib/` survived. Because the oracle is the per-case canonical payload,
**the corpus freeze is robust to `examples/` drift** — an example edit costs
the battery nothing.

## Decisions

Agreed 2026-08-13:

1. **Optimal reference = derived floor** — mechanically computed from the
   case's target shape by pre-registered rules, recorded with its
   derivation. Not hand-authored gold sequences, not empirical best-of-N.
2. **Repeats: N=5 default** — a `--repeats` flag, not a mode. Per-case rates
   at 0–5/5 granularity; ~95-run pooled aggregate.
3. **Corpus: new, from `examples/`** — stratified by shape, frozen as
   `corpus_version: 1` after a calibration firing.
4. **Substrate: `https://elspeth.foundryside.dev`** (local live deploy) —
   app-layer measurement; battery traffic stays out of the gov-domain
   production DB.
5. **Triage: mechanical classification + evidence bundles** — no LLM judge.
   The affordance-vs-hard judgment is a reading exercise over the deviation
   ledger, reviewed case by case.
6. **Approach A** — new package reusing `evals/lib/`. Reconsidered against
   building on composer-parity: kept, because parity's job is cross-surface
   equivalence and its fixtures route to the planner (their intents are
   "Build a pipeline that…"), but the battery **adopts parity's fixture
   format as its oracle contract** and reuses matching topologies.

Added 2026-08-16:

7. **Surface: E — the standing corpus measures the compose loop; a
   one-shot paired probe and a standing tripwire cover the planner.**
   `service.py:3227-3275` routes a freeform request on an empty session to
   the **planner** (`_plan_and_stage_empty_pipeline`) when
   `classify_pipeline_mutation_intent` returns EXPLICIT_MUTATION, else to
   `_compose_loop`. **Both surfaces run the same affordance kit** — the
   byte-identical rendered skill (`self._composer_skill_text`,
   `service.py:4287`), the same tool registry (the planner filters it to a
   discovery subset) and the same authoring aids — so a kit defect can hit
   either. The reason the standing corpus measures the loop is
   **observability and taxonomy**, not kit: only the loop writes `role=tool`
   rows, which every deviation class in §3 reads. Facts that fix the shape
   of E (Decision 7 panel, verified): natural operator voice essentially
   never reaches the planner (acceptance intents 1/24, harness scenarios
   0/27, personas 0/4 classify EXPLICIT_MUTATION; only "Build a pipeline
   that…" does, 10/10 parity fixtures); a semantically-null prefix (`"Hi. "`)
   or a trailing `?` flips every such request back to the loop; the planner
   persists a `planner_attempt_audit` row per model response with closed
   vocabularies and is fully recoverable via `include_llm_audit=true`. **No
   traffic-share figure is cited anywhere in this spec** — the only journal
   sample was the telemetry's own acceptance run.
   - **Standing corpus (§1):** operator voice; every prompt is run through
     the classifier at authoring, at freeze, and **in CI** (a unit test
     over `corpus.md`) and must not be EXPLICIT_MUTATION; the observed
     surface is asserted per run from artifacts (§5).
   - **Paired probe (§7, once, at calibration):** the 10
     `evals/composer-parity` fixtures × 2 arms — as authored (→ planner) and
     `"Hi. "`-prefixed (→ loop) — same `canonical_arguments` oracle, scored
     on a shared information-class floor. It answers one question: *for a
     fully-specified build request, does the surface change the outcome?*
     Pre-registered reading: a null result closes the v2 planner-stratum
     *scope* question, not the behavioural one; a material difference is
     the measured case for building the stratum.
   - **Tripwire (§7, standing):** 2–3 parity fixtures fired as authored
     every round; binary pass/fail (a `PIPELINE_STAGED_*` message and a
     staged payload topologically ≡ the fixture) plus health counts; **no
     floor, no deviation taxonomy, never pooled with the loop rates**. A
     regression check on the shared kit's planner path, not a measurement.
   - Report header caveat: "the standing rates measure the compose loop on
     operator-voice prompts; the planner is covered by the tripwire only."
8. **Currency = tool-bearing provider calls** with `status == success`,
   counted from `llm_call_audit` rows (one per outbound model call).
   Three buckets per row: **tool-bearing** (`tools_spec_hash` non-null —
   the floor currency), **advisor** (`model_requested == advisor model`;
   advisor calls are text-only, so `tools_spec_hash` is null), and
   **other text** (null hash, primary model — e.g. diagnostics; reported,
   never in the floor). Retried/failed calls are counted separately
   (`retried_calls`), never in the floor. Assistant-row counting is
   abandoned: retries collapse into one row, advisor passes persist as
   `role="audit"`, TIMEOUT calls yield a synthetic assistant row. The
   tool-bearing rule is surface-agnostic (planner calls carry tools too), so
   it is only correct together with the per-run surface assertion (§5).
9. **Oracle = per-case validated canonical payload.** Each case carries a
   `canonical_arguments` `set_pipeline` payload that validates against
   `SetPipelineArgumentsModel` and trained-operator plugin availability
   (as `tests/unit/evals/composer_parity/test_fixtures.py` does), plus an
   `expected_topology` (nodes, **edges**, outputs) derived from it. Green
   criteria compare the final state's topology to that by graph
   isomorphism (§2). The payload also *proves* the 1-mutation floor is
   authorable for the case. Canonical payloads use a plain `path` source so
   the synthetic ideal can be built hermetically; the composer's real runs
   bind data via `source.inline_blob` — source *binding* is not part of the
   topology match.
10. **Data path: `source.inline_blob` is the optimal path** for invented
    data (one `set_pipeline` call carries the rows). The kit's
    `create_blob` → bind route is its *repair* path
    (`pipeline_composer.md` "Blob Source"); a run that takes it fires the
    soft class `data_setup_detour` (+1 expected). The floor is stated once,
    on the inline path.

## Glossary

- **case** — one corpus prompt with its scenario file.
- **run** — one session firing one case once.
- **round** — one invocation of the driver (`--round <name>`); a **firing**
  is a complete round over the whole corpus at the configured `--repeats`.
- **floor** — the pre-registered minimum tool-bearing provider-call count for
  a case.
- **tool-bearing call** — an `llm_call_audit` row with non-null
  `tools_spec_hash` and `status == success` (Decision 8).
- **clean / optimal** — defined in §3.
- **RGR** — the red/green/refactor scorer in `evals/lib/composer_rgr_score.py`.
- **gNN pattern** — the operator-voice intent style of
  `ops-local/acceptance/intents/gNN.json`.
- **identity block** — the stamped substrate/kit/model facts a round is
  comparable under (§4); split into *binding* and *recorded* fields.
- **kit defect** — the canonical term for an affordance-kit-caused deviation.
- **probe / tripwire** — the one-shot paired planner experiment and the
  standing binary planner regression check (§7); neither enters a rate.

## 1. Corpus

### Case selection (18 stratified + 1 canary)

Shape data verified 2026-08-16 (all directories exist; shapes hold).
Excluded by rule: examples needing external services or cloud credentials
(`azure_*`, `textract_inline`, `chroma_rag*`, `database_sink`), chaos
harnesses (`chaosllm*`, `chaosweb`, `multi_worker_showcase` — ships chaos
configs), runtime-feature demos without a distinctive compose shape
(`checkpoint_resume`, `rate_limited_llm`, `multi_worker`,
`concurrent_scheduler`, `large_scale_test`, `retention_purge`,
`landscape_journal`, `audit_export`), and near-duplicates of included
shapes (`threshold_gate_container`, `openrouter_multi_query_assessment`,
`schema_contracts_llm_assessment`). `blob_transforms` is a candidate
stratum for v2 (blob/URL fetch; no plain `settings.yaml`).

| # | Case (example) | Shape stratum |
| --- | --- | --- |
| 0 | `canary` (linear passthrough, csv → json) | **Canary** — trivially easy; fired FIRST as a pre-flight at N=10; >1/10 non-optimal ⇒ the firing is flagged degraded before the corpus is spent |
| 1 | `transform_pipeline` | Linear 2-transform chain (`type_coerce` → `value_transform`) |
| 2 | `boolean_routing` | Single boolean gate, two labeled routes |
| 3 | `explicit_routing` | Transform + gate with named routes |
| 4 | `threshold_gate` | Numeric threshold gate, true-route split (`option_assertions`: threshold) |
| 5 | `deep_routing` | 3 transforms + 5 chained gates (decision tree) |
| 6 | `error_routing` | Transforms + gates with error/quarantine routing |
| 7 | `fork_coalesce` | Fork gate (2 paths) + coalesce (`merge_results`) — parity topology reused |
| 8 | `row_union_ab_experiment` | Fork (2 branches) + per-branch tagging + row union — parity `row_union` reused |
| 9 | `batch_aggregation` | Batch aggregation (`batch_stats`) |
| 10 | `statistical_batch_plugins` (variant `top_k`) | Statistical batch plugin (`batch_top_k`; `option_assertions`: k) |
| 11 | `json_explode` | Reshape: one row → many (`json_explode`) |
| 12 | `deaggregation` | Reshape: row replication (`batch_replicate`) |
| 15 | `template_lookups` | LLM transform with template + lookup files |
| 16 | `multi_query_assessment` | LLM transform, multi-field structured assessment |
| 17 | `openrouter_sentiment` | LLM sentiment transform (provider-configured) |
| 18 | `llm_source` | LLM as the *source* |
| 19 | `schema_contracts_demo` | Explicit schema contracts + gate |
| 20 | `report_assemble` | `report_assemble` aggregation batching text lines into a report |

**Deferred to v2 (recorded, not silently dropped):** 13 `multi_source_queue`
and 14 `multi_flow`. Both need multiple invented-data sources; `set_pipeline`
v1 cannot bind named/multiple blob-backed sources
(`web/composer/tools/_common.py:3055` — "named or multiple blob-backed
sources cannot round-trip through set_pipeline v1"), so their mutation floor
is neither 1 nor derivable by the pre-registered rules (the likely v2 route
is `set_source_from_blobs` + `set_pipeline`, a 2-mutation floor to be
verified), and the example extractor cannot express their topology. **This
leaves corpus v1 single-source only — a named blind spot: multi-source
authoring is a real operator task the battery does not yet measure.**
Numbering is kept so the strata table stays comparable when they return.

### Prompt authoring rules

- One **hand-written operator-voice prompt** per case, kept verbatim in
  `evals/composer-battery/corpus.md`. Extraction discipline follows
  `ops-local/acceptance/extract_intents.py`: the first unlabelled fenced
  block under each case heading is the intent, copied byte-for-byte to the
  wire. Ordinary phrasing is evidence; never paraphrase in transit.
- Prompts describe the **task, never the implementation** ("work both things
  out at the same time and bring them back together into one row", not "use
  fork_coalesce") — but pin the task tightly enough that exactly one
  pipeline shape is the reasonable reading. That property is what makes the
  derived floor legitimate. Where a stratum is defined by an option value
  (threshold, k), the prompt states it and the scenario carries an
  `option_assertions` entry for it.
- **Surface gate (offline, at authoring time, at freeze, and in CI):**
  `classify_pipeline_mutation_intent(prompt)` must not return
  `EXPLICIT_MUTATION` (Decision 7); a unit test re-runs it over every
  corpus prompt so a classifier-grammar edit fails the build instead of
  silently re-routing a frozen case. The decision is recorded per case in
  `scenario.json` (`surface.classifier_decision`). Prompts that trip it are
  rephrased in operator voice — "Build a pipeline that reads…" is the
  phrasing to avoid. If the classifier changes after freeze and a frozen
  prompt's decision flips, the case is `instrument_error: surface` on the
  next firing (§5), and the corpus needs a version bump — never a silent
  re-route.
- Prompts that need data instruct the composer to invent it ("make up three
  products"), following the proven gNN pattern, so no fixture files gate a
  firing. `multi_query_assessment`'s `criteria_lookup.yaml` is inlined into
  the prompt as invented content, not shipped as a fixture.
- Where the calibration firing shows two defensible shapes for one prompt,
  either tighten the prompt or widen the floor — **widening only for a
  stated structural reason** (a verified tool dependency the rules missed),
  never because the model took longer. Record the pre-calibration and
  post-calibration floor per case in `corpus.md`. (Single-developer honesty
  rule: the same person authors the prompt, the payload, the floor and the
  kit — the pre/post record is the only audit trail, so it is mandatory.)

### Versioning

`corpus.md` carries `corpus_version: 1` after freeze. Any prompt, floor,
canonical payload, expected-topology or option-assertion change bumps the
version. Reports embed the version; cross-version comparisons are refused by
tooling. The identity block's *binding* fields (§4) also guard `--compare`.

## 2. Scenario format and floor derivation

Each case gets `evals/composer-battery/scenarios/<case>/scenario.json`
(all keys required; no placeholders):

```json
{
  "case": "fork_coalesce",
  "example": "examples/fork_coalesce",
  "variant": null,
  "corpus_version": 1,
  "surface": { "required": "compose_loop", "classifier_decision": "AMBIGUOUS" },
  "canonical_arguments": { "source": {}, "nodes": [], "edges": [], "outputs": [], "metadata": {} },
  "expected_topology": {
    "source": { "plugin": "csv" },
    "nodes": [ { "ref": "n1", "node_type": "gate", "fork_to": 2 },
               { "ref": "n2", "node_type": "coalesce", "policy": "require_all", "merge": "union" } ],
    "edges": [ { "from": "source", "to": "n1", "edge_type": "on_success" },
               { "from": "n1", "to": "n2", "edge_type": "fork" },
               { "from": "n1", "to": "n2", "edge_type": "fork" } ],
    "outputs": [ { "ref": "o1", "plugin": "json" } ],
    "match": "isomorphism"
  },
  "option_assertions": [],
  "floor": {
    "tool_bearing_calls": 2,
    "components": { "discovery": 1, "dependent_listing": 0, "mutation": 1 },
    "repairs": 0, "backtracks": 0,
    "derivation": [
      "discovery: schemas for csv, truncate, coalesce, json — one batched call",
      "data: invented rows travel as source.inline_blob inside the mutation — 0 extra",
      "dependent_listing: none",
      "mutation: single set_pipeline authors the whole graph"
    ],
    "pre_calibration": 2, "post_calibration": null
  },
  "red_criteria": {
    "passivity_phrases": "reuse RGR default list",
    "build_failure_sentinels": ["I cannot mark this pipeline complete", "runtime preflight failed"]
  },
  "green_criteria": {
    "topology_matches_expected": true,
    "option_assertions_hold": true,
    "must_discover_schema_before_first_mutation": true,
    "is_valid": true
  }
}
```

**Oracle contract.** `canonical_arguments` is validated in unit tests exactly
as the parity fixtures are. `expected_topology` is **derived** from it by one
function (`battery_score.topology_from_arguments`) and stored for
readability; a test asserts derivation ≡ stored, and a second test asserts
that the topology of the state `_execute_set_pipeline`
(`web/composer/tools/sessions.py`) commits for the same payload ≡ stored —
so the projection is anchored to the server's own args→state path, which is
hermetic for `path`-sourced payloads (it needs a session engine only for
`inline_blob`). The persisted `CompositionState` keeps `sources` as a map;
the comparator reads `sources["source"]` for the canonical single source.

**Match rule = graph isomorphism.** Nodes are typed by
(`node_type`, `plugin`); the source by `plugin`; outputs by `plugin`. Edges
are typed by `edge_type` (`on_success` / `on_failure` / route / `fork`).
Fork **labels and node ids are author-chosen strings and are ignored** —
correspondence is structural (which node feeds which). Exact on: node
multiset and cardinality (an extra node is `wrong_shape`), edge multiset,
gate route *count*, coalesce `policy` **and `merge`**, output plugins.
Option values are ignored **except** those listed in `option_assertions`
(the stratum-defining ones — a threshold gate with the wrong threshold is
`wrong_shape`). Graphs are ≤ 12 nodes; exact isomorphism is cheap.

`scenario_from_example` (fixed to read the plural `sources:` and to
hard-fail on an absent source) is run as a **cross-check**: its node-type
multiset — with `fork` normalised to `gate` — must be a subset of
`expected_topology`'s, or the scenario fails sanity.

### Floor rules (tool-bearing provider-call currency)

One tool-bearing call = one successful `llm_call_audit` row with a non-null
`tools_spec_hash`. Parallel tool calls batch inside a turn (cap 16;
verified offline-visible via `parent_assistant_id` grouping) — floors
reward batching deliberately.

- **discovery: 1** — all `get_plugin_schema`-class reads for the case
  (including `list_models` for LLM strata) are batchable in one turn.
  `list_sources/transforms/sinks` are prompt-supplied and cost 0.
- **data: 0** — invented rows travel as `source.inline_blob` inside the
  mutation (Decision 10). A `create_blob` → bind route is `data_setup_detour`.
- **dependent_listing: +1** — only when a listing whose *input depends on a
  prior result* is required. The canonical listing tool is `list_blobs`
  (`list_composer_blobs` is a duplicate; either counts).
- **mutation: 1** — one `set_pipeline` authors the whole graph. Verified: it
  can author fork/coalesce/aggregation/multi-output topologies in one call.
  `apply_pipeline_recipe` delegates to `set_pipeline` and counts as the
  mutation; the run is flagged `recipe_used` for triage but not penalised —
  a matching recipe *is* the straightest path.
- **repairs = 0, backtracks = 0.**

Every v1 floor is therefore 2 unless a case needs a dependent listing. The
`derivation` array is prose, one line per component — it is what makes a
later deviation attributable.

## 3. Deviation taxonomy

Every excess beyond the floor must land in exactly one mechanical class.
Classification reads the captured `llm_call_audit` rows, `role=tool` rows,
the assistant envelopes' per-call outcome stamps, `reviews.json`, and
`meta.json.server_terminal`. Each event carries evidence: `sequence_no`
range, tool name + args digest, error/validation codes, audit-row ordinal.

**Mutation vocabulary** (complete, from the web registry): `set_pipeline`,
`set_source`, `set_source_from_blob`, `set_source_from_blobs`, `set_output`,
`upsert_node`, `upsert_edge`, `patch_source_options`, `patch_node_options`,
`patch_output_options`, `remove_node`, `remove_edge`, `remove_output`,
`clear_source`, `splice_transform`, `apply_pipeline_recipe`, `set_metadata`.
Data tools: `create_blob`, `update_blob`, `delete_blob`, `wire_blob_inline_ref`.

**Mutation outcome discriminator.** The durable pair on the tool row —
`composition_state_id` (a state was written) plus the audit envelope status
— decides *applied vs not*; the assistant envelope's `tool_calls[i].outcome`
(`applied | rejected | failed | cancelled | completed`, a route-time
projection serialised into the capture) is corroboration. Never key on the
raw tool-row `success` alone (elspeth-f5e6723133), and never on tool names.
`cancelled` counts as not-applied.

| Class | Signal | Severity |
| --- | --- | --- |
| `excess_discovery` | re-read of already-fetched schema/state with no intervening mutation | soft |
| `schema_fumble` | repeated `patch_*_options` against the same node | soft |
| `data_setup_detour` | `create_blob` → bind instead of `source.inline_blob` | soft |
| `data_rework` | `update_blob`/`delete_blob`/second `create_blob` for the same invented data | soft |
| `retried_provider_error` | a transport-status audit row (`api_error`/`timeout`) followed by a successful retry in the same run | soft (counted; the run stays in the denominator) |
| `repair` | a mutation not-applied (discriminator above) followed by a retried mutation | hard |
| `backtrack` | an applied mutation later undone by `remove_*`/`clear_source`/wholesale re-`set_pipeline` of the same target | hard |
| `malformed_output` | any `llm_call_audit.status ∈ {malformed_response, bad_request_error}` — the model produced an unusable turn | hard |
| `wrong_shape` | `is_valid` final state whose topology ≠ `expected_topology` or an `option_assertion` fails | hard (scored separately from path) |
| `decline` / `passivity` | no mutation + permission-seeking (RGR red criteria reused) | hard |
| `turn_exhaustion` | `server_terminal.budget_exhausted ∈ {composition, discovery}` without valid state | hard |
| `wall_timeout` | `server_terminal.budget_exhausted == timeout` (600 s) without valid state | hard |
| `approval_pending` | mutation persisted `success: true` with `status: APPROVAL_REQUIRED` (latent — default `trust_mode` is `auto_commit`) | hard |
| `instrument_error` | see sub-kinds below | **excluded from rates; flags the firing** |

`instrument_error` sub-kinds (the `score.json.excluded` enum):
`no_calls` (zero tool-bearing calls), `auth`, `http` (unrecovered HTTP
failure or 499 with writes in flight), `read_integrity`
(`AuditIntegrityError` on the messages read), `truncated` (last page was
full), `surface` (planner rows present, or zero audit rows so the surface
is undetermined — never defaulted to compose loop), `terminal_missing`
(non-valid end state and no server terminal reason), `transport` (a
transport-status audit row with **no** successful retry — the run never
recovered), `capture` (missing/unparseable artifact).

- **Clean run** = zero deviation events ∧ green criteria ∧ `is_valid`.
- **Optimal run** = clean ∧ tool-bearing calls == floor.
- Above-floor with no class fired ⇒ `unattributed_excess` — a visible
  taxonomy gap, never silently absorbed into "clean". A case whose
  `unattributed_excess` rate stays high across rounds is a candidate
  mis-derived floor and is re-reviewed.
- Interpretation-review rounds are counted (`review_rounds`, one entry per
  captured payload with its round index) and reported; resolution is
  automated and not a deviation, but a review-after-repair cycle is signal.
- The soft/hard split exists for the report's histogram and for triage
  ordering; it does not change clean/optimal.
- **Trap:** `ComposerLLMCallStatus.TIMEOUT` on an audit row is a
  *provider-call* timeout on a different clock; only
  `server_terminal.budget_exhausted == timeout` is `wall_timeout`.

Taxonomy revisions are expected (case-by-case review). Because scoring is
offline over captured artifacts, revisions re-score history free **for any
class expressible from the captured channels** (thread rows, audit rows,
review payloads, state, validate, meta incl. the terminal body). Timing- and
token-based classes are expressible; anything server-side that persists
nothing is not, and would need a capture change plus a new identity field.

## 4. Driver

`evals/composer-battery/drive_battery.py` (standalone Python, tracked).
Flags: `--base` (default `https://elspeth.foundryside.dev`),
`--round <name>` (e.g. `2026-08-20-baseline`), `--repeats` (default 5),
`--cases a,b,c` (comma-separated case names; omit for all), `--resume`,
`--cleanup`, `--probe` (runs the §7 paired probe; calibration only).
Firing order is fixed: canary pre-flight (N=10), then the §7 tripwire
(2–3 fixtures, once), then round-robin by repeat index over the corpus (repeat 1 of every case, then
repeat 2, …) so mid-firing drift is not confounded with case index. Repeat
index is recorded per run and the report bins by it, because provider
prompt-cache state differs between repeat 1 and later repeats
(`cached_prompt_tokens` is captured per call).

Per run:

1. **Login only, never register.** Credentials from the state dir
   (`battery_local` account, `credentials.json` mode 600). Hard-fail the
   firing if `access_token` is absent — never cache an error body
   (elspeth-2e5086dce6; a deliberate departure from `drive_graph.py`).
2. New session; **`PATCH` title to `battery/<round>/<case>/<n>` BEFORE
   posting** — this is a contract, not hygiene: the server's auto-title
   fires an *unaudited* provider call only when the title is still the
   default (`_auto_title.py`), and the pre-PATCH suppresses it (a unit test
   pins the ordering). POST the verbatim corpus prompt. Client timeout
   620 s — a deliberate 20 s margin over the server's 600 s budget
   (`COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS` is 660 s).
   - On a **422**, capture the full `detail` body — it carries `turns_used`,
     `budget_exhausted ∈ {composition, discovery, timeout}` and `reason`
     (the closed `ComposerProgressReason` vocabulary). This is the **only
     durable carrier of the terminal reason**; `GET /messages` writes no
     row for a convergence failure and `/api/sessions/_active` excludes
     terminal phases by construction.
   - On a client timeout, immediately (once) `GET
     /api/sessions/{id}/composer-progress` and capture `reason` — the
     registry is in-process, one slot per session, clobbered by the next
     turn — then recover the thread via `GET /messages`. Never declare
     failure from the client side alone.
   - A **499** or any non-200 compose response means server writes may still
     be in flight (`run_sync_in_worker` never cancels its worker); wait for
     the audit-row count to stabilise across two reads before capturing.
3. **Interpretation-review gate:** auto-resolve pending reviews as
   `accepted_as_drafted`; re-poll after any repair (a correction can stage
   new reviews); **bounded at 5 rounds** (as `drive_graph.py`), then the run
   is `instrument_error`. Capture every review payload with its round index
   to `reviews.json`.
4. Server-side `POST /validate?state_id=<final>` (pinned — validate is not
   pure; it can surface reviews and insert `interpretation_events`) →
   capture to `runs/<round>/<case>/<n>/`:
   - `messages.json` — `GET …/messages?include_tool_rows=true&include_llm_audit=true&include_raw_content=true&limit=500&offset=…`,
     **paginated on `offset` with an identical flag set until a page shorter
     than 500 — a full page always triggers the next fetch, including a
     page of exactly 500**; a run whose last page is full is
     `instrument_error: truncated`. `sequence_no` is a total order with
     gaps permitted; already-fetched pages never shift.
   - `state.json` (with `state_id`), `validate.json`, `reviews.json`,
   - `meta.json` (schema below).
   Capture failure is logged, never kills the round (`drive_graph.py`
   pattern); a run missing any artifact is `instrument_error: capture`.
   **`--resume` re-verifies artifacts exist and parse; it never re-fetches
   or overwrites a captured page** (assistant-envelope outcome stamps are
   recomputed per request and could differ).
5. **Stop — no execute.** Path quality is fully determined at
   compose+validate.

`meta.json`:

```json
{
  "round": "…", "case": "…", "repeat": 3, "corpus_version": 1,
  "prompt_sha256": "…", "session_id": "…", "state_id": "…",
  "http": [ { "step": "post_message", "status": 422, "elapsed_ms": 1234,
              "detail": { "turns_used": 40, "budget_exhausted": "composition", "reason": "…" } } ],
  "server_terminal": { "budget_exhausted": "composition|discovery|timeout|null",
                       "reason": "…", "source": "422_detail|composer_progress|none" },
  "identity": {
    "binding": {
      "substrate": "https://elspeth.foundryside.dev",
      "composer_model": "openrouter/anthropic/claude-sonnet-5",
      "advisor_model": "openrouter/anthropic/claude-opus-4-8",
      "model_returned": "…",
      "composer_timeout_seconds": 600.0,
      "budgets": { "composition_turns": 30, "discovery_turns": 10 },
      "tools_spec_hash": "…",
      "temperature": null, "seed": null
    },
    "recorded": {
      "composer_skill_hash": "…", "first_call_messages_hash": "…",
      "server_version": "0.7.2", "frontend_build": "index-….js"
    }
  }
}
```

**Binding vs recorded.** `--compare` **refuses** on any binding mismatch
and **prints** recorded deltas. `composer_skill_hash` is *recorded*, not
binding, on purpose: a kit edit is the independent variable the battery
exists to compare across, so its hash is the delta under test, printed in
the report header. `frontend_build` is SPA-only noise to an API driver.
Sources: `substrate`, `composer_model`, `composer_timeout_seconds`,
`frontend_build` from `/api/system/status`; `advisor_model` and budgets from
the operator (`deploy/elspeth-web.env`) until an endpoint exposes them;
`tools_spec_hash`, `model_returned`, `temperature`, `seed` from the first
tool-bearing audit row; `composer_skill_hash` = SHA-256 of the *rendered*
system prompt (`service.py:1961`), reachable via
`GET …/interpretations` when a review exists, else null;
`first_call_messages_hash` is a strict superset discriminator (varies with
overlay, plugin snapshot, cache markers) — it fails only in the safe
direction and is recorded, not binding.

Operational posture:

- **Serial in v1**, canary-first then round-robin. A full 19×5 firing runs a
  few hours; the per-user rate limiter is in-process so other users' buckets
  are safe, but the OpenRouter key and `sessions.db` are shared — fire
  off-peak and say so in the round name.
- **Abort conditions:** (a) three consecutive runs ending `instrument_error`
  halt the firing; (b) **the same case** ending `instrument_error` on two
  consecutive repeats flags that case and continues (round-robin means a
  single drifting case never trips (a)); (c) an excluded-run count above
  15 % of runs so far flags the firing as degraded in the report header.
- **Session hygiene.** `--cleanup` deletes only sessions owned by
  `battery_local` whose title matches `battery/<this round>/…` **and** whose
  capture passed the completeness check; default **off** while the
  instrument is young.
- **`.gitignore`:** re-include `evals/composer-battery/` with the same
  credential re-exclusions composer-parity uses; `runs/` is not tracked.

## 5. Scoring and reporting

Both layers run **offline against captured artifacts** — the server is never
needed after capture.

`evals/lib/battery_score.py` — per run → `score.json`:

```json
{
  "case": "…", "repeat": 3,
  "surface_observed": "compose_loop|planner|undetermined",
  "tool_bearing_calls": 3, "advisor_calls": 2, "other_text_calls": 0,
  "retried_calls": 0, "audited_provider_calls": 5,
  "floor": 2, "excess": 1,
  "deviations": [ { "class": "repair", "sequence_no": [41, 47], "tool": "set_pipeline",
                    "args_digest": "…", "codes": ["…"], "audit_ordinal": 2 } ],
  "review_rounds": 1, "recipe_used": false,
  "green": true, "red": false, "is_valid": true, "wrong_shape": false,
  "clean": false, "optimal": false, "excluded": null,
  "tokens": { "prompt": 0, "completion": 0, "cached_prompt": 0, "unknown_calls": 0 },
  "cost": null, "wall_ms": 0
}
```

Field rules: `surface_observed` = `planner` if any audit row has
`planner_call_ordinal != null` or `_kind == planner_attempt_audit`;
`compose_loop` if tool-bearing rows exist and none are planner rows;
`undetermined` if there are zero audit rows (⇒ `instrument_error: surface`,
never a default). `audited_provider_calls` = all `llm_call_audit` rows
(unaudited server calls such as auto-title are outside it by construction —
hence the PATCH-before-POST contract). Tokens/cost are `null` on every
failed call (`TokenUsage.unknown()`); the scorer sums known values and
counts `unknown_calls`. `wall_ms` = `max(finished_at) − min(started_at)`
over audit rows — recomputable from the bytes, never the driver clock.
`excluded` ∈ the `instrument_error` sub-kind enum or `null`.

`evals/composer-battery/report.py` — per round → `report.md` + `report.json`:

```json
{
  "round": "…", "corpus_version": 1, "identity": { "binding": {}, "recorded": {} },
  "caveats": ["compose-loop surface only", "operator-voice register only", "…"],
  "canary": { "n": 10, "non_optimal": 0, "flag": false },
  "tripwire": [ { "fixture": "fork_coalesce", "pass": true, "staged_variant": "PIPELINE_STAGED_AUTO_COMMIT",
                  "planner_calls": 4, "planner_codes": {} } ],
  "pooled": { "n": 95, "excluded": 3, "clean": 71, "optimal": 60, "hard": 12,
              "formula": "sum(successes)/sum(n)", "mde_pp": 10 },
  "by_repeat": [ { "repeat": 1, "n": 19, "clean": 14, "cached_prompt_tokens_median": 0 } ],
  "by_case": [ { "case": "…", "n": 5, "excluded": 0, "clean": 4, "optimal": 3,
                 "histogram": { "repair": 1 }, "median_excess": 0, "median_review_rounds": 0,
                 "per_case_ci_pp": 40 } ],
  "ledger": [ { "case": "…", "class": "repair", "events": [ { "…": "…" } ] } ]
}
```

Headline first: clean rate, optimal rate, hard-deviation rate — **each with
`n` and the number of excluded runs beside it**, the pooling formula
(Σsuccesses/Σn, correct under unequal per-case n after exclusions), the
canary result, and the degraded-firing flag if any abort condition fired.
Then per-repeat bins, per-case rates, then the **deviation ledger** grouped
case → class with each event's evidence inline. `--compare <prev-round>`
emits pooled and per-case deltas, guarded on matching `corpus_version` and
binding identity, printing recorded deltas (the skill hash first) and the
MDE: at N=5 a per-case rate carries roughly a ±40 pp 95 % interval, so
per-case deltas are shown but labelled *indicative*; claims are made on the
pooled aggregate only.

The deviation ledger is the triage surface: the case-by-case "kit defect vs
hard problem" review reads typed events with evidence, not raw transcripts.
Deviations triaged to kit defects become Filigree issues manually — the
battery does not auto-file.

## 6. Testing and calibration

Unit tests under `tests/unit/evals/composer_battery/`, honoring
mutation-test-the-guard:

- **Scenario sanity:** every `scenario.json` well-formed; `canonical_arguments`
  validates as parity fixtures do; `expected_topology` ≡
  `topology_from_arguments(canonical_arguments)` ≡ topology of the state
  `_execute_set_pipeline` commits for it; extractor cross-check subset
  holds (with `fork`→`gate`); classifier decision ≠ EXPLICIT_MUTATION.
- **Synthetic ideal from the canonical payload:** the ideal state is built
  through `_execute_set_pipeline` (hermetic for `path` sources); a synthetic
  thread at exactly the floor scores CLEAN + OPTIMAL.
- **Comparator both ways:** for fork_coalesce — same topology under
  different fork labels/node ids **passes**; swapped route wiring between
  two same-typed gates **fails**; wrong coalesce `merge` **fails**; extra
  passthrough node **fails**; sink `json`→`jsonl` **fails**; threshold gate
  with a wrong threshold fails only when `option_assertions` lists it.
- **Classifier both ways, plus boundary and near-miss:** per class, a
  minimal thread that must trigger it, the ideal thread that must not, a
  **near-miss** (the durable pair says applied but the envelope says
  `rejected` — must follow the pair) that must not mis-fire, and
  **cross-class negatives** (`repair` fixture must not fire `backtrack` and
  vice versa; `cancelled` outcome is not-applied). Boundary tests are on
  the captured terminal body (`budget_exhausted` present/absent,
  `turns_used` 39/40, `timeout` vs a provider-call `TIMEOUT` status), not
  on inferred turn counts.
- **Instrument honesty:** zero audit rows → `surface: undetermined` →
  excluded; a full last page (incl. exactly 500) → `truncated`; planner rows
  → `surface`; transport row with no successful retry → `transport`
  (excluded) vs with a retry → `retried_provider_error` (counted); a
  `malformed_response` row → `malformed_output` (hard, counted); missing
  terminal body on a non-valid end → `terminal_missing`.
- **Discriminator invariants (offline):** a characterization test that the
  advisor call path passes `tools=None`; that `composer_advisor_model` ≠
  `composer_model` (config validator); that PATCH-title precedes POST in the
  driver.
- **Probe/tripwire honesty:** each arm's surface is asserted from
  artifacts; both arms of a fixture must dry-run to their intended surface
  offline before firing (classifier fingerprint); tripwire results render
  in their own table and never enter a pooled rate; `undetermined` is a
  flag, never a pass.
- **Report honesty:** `--compare` refuses on binding mismatch and prints
  recorded deltas; `n`, exclusions and the formula render beside every rate;
  the per-case streak and 15 % exclusion flags render.
- **Calibration firing before freeze:** canary at N=10, the tripwire, the
  §7 paired probe (10 × 2, once), then one N=1 pass across the 18 cases. Calibration runs are corpus QA, never measurement —
  they enter no rate. Checks: surface observed = compose loop for every
  case; advisor rows are on the advisor model with null `tools_spec_hash`;
  first-call `messages_hash` stable across two runs of one case; floors
  reachable (at least one run at floor across the calibration, else
  re-derive); the composer's data path (inline vs create_blob) observed and
  recorded per case; the passivity rate reported as a corpus-QA signal
  (Decision 7). Ambiguity found ⇒ tighten prompt or widen floor for a
  structural reason, decision recorded per case with pre/post floors. Only
  then freeze `corpus_version: 1`.
- Full `pytest tests/` before merge (whole-tree AST gates; scoped runs prove
  nothing about them).

### Implementation obligations (verified 2026-08-16)

- ~~Fix `scenario_from_example._extract_source` for the plural `sources:` dict
  and make an absent source a hard error~~ — **landed 2026-08-16** (reads the
  first declared source, records `name`/`source_count`, raises on a null
  plugin; a truth-test pins every plain example). ~~`fork`→`gate`~~ — landed as
  a `fork`→`gate` alias in `composer_rgr_score._SHAPE_TOKEN_ALIASES`, so the
  cross-check needs no extra normalisation.
- Give the RGR tool-row helpers public names before importing them.
- Author `battery_score.topology_from_arguments`, the isomorphism
  comparator, and the `sources["source"]` mapping (no comparator exists for
  parity `semantic_expectations` today).
- Confirm tool **arguments** survive redaction on the assistant envelope
  (needed by `excess_discovery`/`schema_fumble`); `success` is known to
  survive on the tool row.
- Confirm `status: APPROVAL_REQUIRED` survives redaction (latent class).
- Verify at calibration: advisor discriminator; `create_blob` vs
  `inline_blob` behaviour; `messages_hash` stability.
- Do not cross-check the scorer's repair count against
  `composer_meta.repair_turns_used` — it is hard-capped 0..2
  (`composer/protocol.py`), corroboration at best.
- Every audit-grade `GET /messages` page writes an `audit_access_log` row —
  harmless, but pagination multiplies it; expected, not a defect.
- If `composer_skill_hash` is null (no review in the session), the blob
  record's `creating_composer_skill_hash` (`contracts/blobs.py`) is a
  candidate second source; confirm it is exposed on a read API before
  depending on it.

## 7. Planner probe and tripwire

Both use the parity fixtures verbatim (`evals/composer-parity/fixtures/*.json`
`intent`, tracked) — the only tracked register that routes to the planner —
and their `canonical_arguments` as the oracle, so no new authoring.

**Paired probe (once, at calibration; enters no rate).** For each of the 10
fixtures: arm P = `intent` verbatim (classifier: EXPLICIT_MUTATION → planner);
arm L = `"Hi. " + intent` (→ compose loop). Both arms are asserted per run
from artifacts (`planner_call_ordinal != null` ⇔ planner). Scored on a
**shared information-class floor**: the set of `ComposerPlannerInformationClass`
values the case's topology requires (e.g. `plugin.schema` per selected
plugin, `catalog.selection`) must appear in the union of `new_information`
(planner arm) or be implied by the discovery tool calls (`get_plugin_schema`
⇒ `plugin.schema`; loop arm) before the accepting mutation; plus one
accepted terminal (`outcome=accepted, led_to=done` / a `set_pipeline` with
`is_valid`). Deviations on the planner arm map from the closed
`ComposerPlannerCode` vocabulary (`DISCOVERY_NO_GAIN`/`DISCOVERY_CYCLE` ⇒
kit-misled discovery; `REPAIR_BLIND_REPEAT`/`repeated_fingerprint` ⇒ error
message not actionable; `*_EXHAUSTED` ⇒ budget; `MALFORMED_RESPONSE`/prose
⇒ model). Output: a 10×2 table of clean/floor/deviations plus the
`PIPELINE_STAGED_*` outcome, and a written reading against the
pre-registered rule above. The probe is bound to a classifier fingerprint
(the offline dry-run of both arms is a precondition; if the grammar changes,
the pair silently unpairs).

**Tripwire (standing, every round, before the corpus).** 2–3 fixtures
(`fork_coalesce`, `error_routing`, one linear) fired as authored. Pass ⇔ a
`PIPELINE_STAGED_*` assistant message appears **and** the staged candidate's
topology ≡ the fixture's `expected_topology` under the §2 isomorphism rule.
Recorded, not scored: `planner_call_ordinal` count, `planner_code`
distribution, the `PIPELINE_STAGED_*` variant. `undetermined` surface is a
flag, never a pass. Reported in its own table; never pooled with loop rates.

**Prerequisites (both):** capture `GET …/proposals` and `/proposal-events`
per run — under `explicit_approve` or a preflight-not-green outcome the
staged candidate lands in a proposal row, not `state.json` — and pin the
`battery_local` auto-commit preference so runs are comparable; paginate the
messages fetch (§4 already does). **Deferred to v2 with the planner
stratum:** the per-invocation discovery rows (`_kind:"audit"`) and the
`planner_failure_disposition` row are filtered from every API view; the
failure-disposition one is a one-line server predicate widening
(`sessions/routes/_helpers.py:_is_composer_llm_audit_tool_message`) and is
needed only for planner *failure* triage, which the tripwire reports as a
count, not a class.

## Out of scope (v1)

- Executing composed pipelines (compose+validate only).
- A scored planner *stratum* (v2, gated on the paired probe's result); v1
  covers the planner by probe + tripwire only (§7).
- Cases 13/14 until `set_pipeline` binds multiple blob-backed sources.
- LLM-judge triage verdicts; auto-filing tracker issues.
- Firing at the gov-domain production host.
- Parallel repeat execution.
- Guided-mode path quality.

## Appendix — revision history

**Rev 2 (2026-08-16)**, after the first panel review: pinned one surface
(C0); currency from `llm_call_audit` rows (C1); oracle rebased on canonical
payloads with edges, extractor demoted (C2–C4); cases 13/14 deferred,
canary added; capture fixed (`include_llm_audit`, pagination, `state_id`,
identity block) (C5); budgets corrected (H1); provider-error class and abort
(H2, H8); test plan, statistics, Goodhart non-goal, mutation vocabulary,
tracking, schemas, glossary (H3–H7, Mediums).

**Rev 3 (2026-08-16)**, after the second panel review: data path corrected
to `source.inline_blob` with `data_setup_detour` (Decision 10);
`provider_error` split into `retried_provider_error` (counted) /
`malformed_output` (hard) / `instrument_error: transport` (excluded), and
the abort conditions reworked with a per-case streak and an exclusion
threshold; the 422 terminal body and `composer-progress` captured as the
terminal-reason sources; `surface_observed` asserted from artifacts with an
`undetermined` state; three-bucket call discriminator with `advisor_model`
in identity; identity split into binding vs recorded with the skill hash as
the recorded independent variable; oracle match rule = isomorphism with
labels ignored, `merge` and cardinality exact, `option_assertions`;
mutation-outcome discriminator pinned to the durable pair with the full
outcome vocabulary; canary as N=10 pre-flight; per-repeat bins; pooling
formula; tokens/cost null rule; `wall_ms` provenance; PATCH-before-POST
contract; `--order` flag, `driver_version`, quarterly clause dropped;
`report.json` schema; ownership; file-path corrections.

**Rev 4 (2026-08-16)**, after the Decision 7 panel (7 seats): Decision 7 →
E (loop corpus + one-shot paired probe + standing tripwire); rationale
rewritten on observables — the planner runs the same kit; the contaminated
traffic datum struck; classifier gate added to CI; §7 added with the
shared information-class floor, prerequisites and v2 deferrals; success
criteria gain the discrimination test.
