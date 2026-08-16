# Composer Path-Quality Battery — Design

**Date:** 2026-08-13 (rev 1) · **Rev 2:** 2026-08-16
**Status:** Rev 2 — revised after the 2026-08-16 go/no-go review
(`2026-08-16-composer-battery-design-review.md`); pending user approval,
pre-implementation
**Home:** `evals/composer-battery/` (new), reusing `evals/lib/` and
`evals/composer-parity/fixtures/`

## Rev 2 change log (→ review finding ids)

| Change | Closes |
| --- | --- |
| Corpus pinned to ONE authoring surface (compose loop) via an offline classifier gate; surface stamped per run | C0 |
| Currency redefined: count of tool-bearing `llm_call_audit` rows, not assistant rows; advisor calls reported separately | C1 |
| Oracle rebased on a per-case validated `canonical_arguments` `set_pipeline` payload + expected topology (edges included); extractor demoted to a cross-check and fixed for `sources:` | C2, C3, C4 |
| Cases 13/14 deferred to v2 (multi-blob binding); canary case added | C2, H4 |
| Capture: `include_llm_audit=true`, offset pagination to a short page, `state_id` pinned on validate, review payloads captured, identity block in `meta.json`, `--compare` refuses on identity mismatch | C5, H1 |
| Budgets corrected (600 s; 30 composition + 10 discovery = 40 turns); `non_convergence` split; provider-error class; abort condition | H1, H2, H8 |
| Test plan: boundary, cross-class, near-miss, `wrong_shape` fixture, synthetic ideal built from the canonical payload | H3 |
| Statistics: `n` beside every rate, MDE stated, pooled claims only, round-robin firing order, canary | H4 |
| Non-goal on Goodhart; floor derivation pre-registered; widening constrained and recorded pre/post | H5 |
| Full mutation vocabulary; data-setup component; recipe rule; `list_blobs` pinned | H6 |
| Driver/scorer tracked with a test import; `.gitignore` re-include with credential re-exclusions | H7 |
| Schemas for `scenario.json`, `score.json`, `report.json`; `--cases` syntax; glossary; success criteria; login-only stated as a deliberate departure from `drive_graph.py` | Medium/doc |

## Purpose

The Web Composer is an LLM tool loop: an operator states what they want, the
model builds it with tool calls. Success alone is a weak signal — the model
can reach a working pipeline while struggling (repairs, backtracking,
redundant discovery) some fraction of the time. This battery measures, for a
fixed corpus of operator-voice prompts, whether the composer takes the
**straightest path** to the answer, and when it does not, produces the
evidence needed to triage each deviation into:

- **affordance-kit defect** — our tool descriptions, schemas, skill text, or
  guidance gave incorrect or ambiguous direction; or
- **hard problem** — the case is intrinsically difficult.

Each case is *constructed to have a known optimal path*. Deviations from that
path — not failures — are the primary datum.

**Non-goal (Goodhart guard).** The floor measures the straightest path *for
this corpus's one-shot prompts on the compose-loop surface*. It is not a
prescription for composer behaviour with iterating operators, and a battery
delta is never on its own sufficient justification for a kit edit that
reduces discovery or clarification behaviour outside the corpus. Kit edits
motivated by the ledger must be argued from the evidence bundle, not the
rate.

**Success criteria for the instrument itself.** The battery is worth keeping
while (a) every deviation it reports carries evidence a reader can act on
without opening a raw transcript, (b) two firings against the same identity
block agree within the stated MDE on the pooled rates, and (c) at least one
kit defect per quarter is found through it. If (a) or (b) fails, fix the
instrument before trusting it; if (c) fails for two quarters, retire it.

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
`evals/lib/` survived.

## Decisions

Agreed 2026-08-13:

1. **Optimal reference = derived floor** — mechanically computed from the
   case's target shape by pre-registered rules, recorded with its
   derivation. Not hand-authored gold sequences, not empirical best-of-N.
2. **Repeats: N=5 default** — a `--repeats` flag, not a mode. Per-case rates
   at 0–5/5 granularity; 100-run pooled aggregate.
3. **Corpus: new, from `examples/`** — stratified by shape, frozen as
   `corpus_version: 1` after a calibration firing.
4. **Substrate: `https://elspeth.foundryside.dev`** (local live deploy) —
   app-layer measurement; battery traffic stays out of the gov-domain
   production DB.
5. **Triage: mechanical classification + evidence bundles** — no LLM judge.
   The affordance-vs-hard judgment is a reading exercise over the deviation
   ledger, reviewed case by case.
6. **Approach A** — new package reusing `evals/lib/`. Reconsidered in rev 2
   against building on composer-parity: kept, because parity's job is
   cross-surface equivalence and its fixtures route to the planner (their
   intents are "Build a pipeline that…"), but the battery **adopts parity's
   fixture format as its oracle contract** and reuses matching topologies.

Added 2026-08-16:

7. **Surface: compose loop only.** `service.py` routes a freeform request on
   an empty session to the **planner** when
   `classify_pipeline_mutation_intent` returns EXPLICIT_MUTATION, else to
   `_compose_loop`. The two are different architectures (planner: own
   75-call budget, discovery not persisted as tool rows). Corpus v1 measures
   the compose loop — the surface the affordance kit governs. Every corpus
   prompt is run through the classifier at authoring time and must NOT be
   EXPLICIT_MUTATION; the decision is recorded per case and re-checked at
   freeze. A planner stratum is v2 with its own floor rules. *(This is the
   one rev 2 decision most worth the user's explicit confirmation.)*
8. **Currency = tool-bearing provider calls**, counted from `llm_call_audit`
   rows (one per outbound model call) whose `tools_spec_hash` is non-null.
   Advisor calls are text-only (no tools) and are reported separately, never
   in the floor. Assistant-row counting is abandoned: retries collapse into
   one row, advisor passes persist as `role="audit"`, TIMEOUT calls yield a
   synthetic assistant row.
9. **Oracle = per-case validated canonical payload.** Each case carries a
   `canonical_arguments` `set_pipeline` payload that validates against
   `SetPipelineArgumentsModel` and trained-operator plugin availability
   (as `tests/unit/evals/composer_parity/test_fixtures.py` does), plus an
   `expected_topology` (nodes, **edges**, outputs) derived from it. Green
   criteria compare the final state's topology to that. The payload also
   *proves* the 1-mutation floor is authorable for the case.

## Glossary

- **case** — one corpus prompt with its scenario file.
- **run** — one session firing one case once.
- **round** — one invocation of the driver (`--round <name>`); a **firing**
  is a complete round over the whole corpus at the configured `--repeats`.
- **floor** — the pre-registered minimum tool-bearing provider-call count for
  a case.
- **clean / optimal** — defined in §3.
- **RGR** — the red/green/refactor scorer in `evals/lib/composer_rgr_score.py`.
- **gNN pattern** — the operator-voice intent style of
  `ops-local/acceptance/intents/gNN.json`.
- **identity block** — the stamped substrate/kit/model facts a round is
  comparable under (§4).

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
| 0 | `canary` (linear passthrough, csv → json) | **Canary** — trivially easy; expected non-optimal rate ≈ 0; a non-zero rate flags a degraded firing |
| 1 | `transform_pipeline` | Linear 2-transform chain (`type_coerce` → `value_transform`) |
| 2 | `boolean_routing` | Single boolean gate, two labeled routes |
| 3 | `explicit_routing` | Transform + gate with named routes |
| 4 | `threshold_gate` | Numeric threshold gate, true-route split |
| 5 | `deep_routing` | 3 transforms + 5 chained gates (decision tree) |
| 6 | `error_routing` | Transforms + gates with error/quarantine routing |
| 7 | `fork_coalesce` | Fork gate (2 paths) + coalesce (`merge_results`) — parity topology reused |
| 8 | `row_union_ab_experiment` | Fork (2 branches) + per-branch tagging + row union — parity `row_union` reused |
| 9 | `batch_aggregation` | Batch aggregation (`batch_stats`) |
| 10 | `statistical_batch_plugins` (variant `top_k`) | Statistical batch plugin (`batch_top_k`) |
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
(`web/composer/tools/_common.py` — "named or multiple blob-backed sources
cannot round-trip through set_pipeline v1"), so their mutation floor is
neither 1 nor derivable by the pre-registered rules (the likely v2 route
is `set_source_from_blobs` + `set_pipeline`, a 2-mutation floor to be
verified), and the example extractor cannot express their topology. Numbering is kept so the strata
table stays comparable when they return.

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
  derived floor legitimate.
- **Surface gate (offline, at authoring time and again at freeze):**
  `classify_pipeline_mutation_intent(prompt)` must not return
  `EXPLICIT_MUTATION` (which would route to the planner, Decision 7). The
  decision is recorded per case in `scenario.json` (`surface.classifier_decision`).
  Prompts that trip it are rephrased in operator voice — "Build a pipeline
  that reads…" is the phrasing to avoid.
- Prompts that need data instruct the composer to invent it ("make up three
  products"), following the proven gNN pattern, so no fixture files gate a
  firing. `multi_query_assessment`'s `criteria_lookup.yaml` is inlined into
  the prompt as invented content, not shipped as a fixture.
- Where the calibration firing shows two defensible shapes for one prompt,
  either tighten the prompt or widen the floor — **widening only for a
  stated structural reason** (a verified tool dependency the rules missed),
  never because the model took longer. Record the pre-calibration and
  post-calibration floor per case in `corpus.md`.

### Versioning

`corpus.md` carries `corpus_version: 1` after freeze. Any prompt, floor,
canonical payload or expected-topology change bumps the version. Reports
embed the version; cross-version comparisons are refused by tooling. The
identity block (§4) is versioned separately and also guards `--compare`.

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
    "nodes": [ { "id": "fork_gate", "node_type": "gate", "fork_to": ["path_a", "path_b"] },
               { "id": "merge_results", "node_type": "coalesce", "policy": "require_all" } ],
    "edges": [ { "from_node": "source", "to_node": "fork_gate", "edge_type": "on_success" },
               { "from_node": "fork_gate", "to_node": "merge_results", "edge_type": "fork", "label": "path_a" } ],
    "outputs": [ { "sink_name": "merged", "plugin": "json" } ],
    "match": { "node_ids": "ignore", "option_values": "ignore", "edge_labels": "exact" }
  },
  "floor": {
    "tool_calls": 2,
    "components": {
      "discovery": 1, "data_setup": 0, "dependent_listing": 0, "mutation": 1
    },
    "repairs": 0, "backtracks": 0,
    "derivation": [
      "discovery: schemas for csv, truncate, coalesce, json — one batched call",
      "data_setup: create_blob for the invented rows batches into the discovery turn — 0 extra",
      "dependent_listing: none",
      "mutation: single set_pipeline binds blob_id and authors the whole graph"
    ],
    "pre_calibration": 2, "post_calibration": null
  },
  "red_criteria": {
    "passivity_phrases": "reuse RGR default list",
    "build_failure_sentinels": ["I cannot mark this pipeline complete", "runtime preflight failed"]
  },
  "green_criteria": {
    "topology_matches_expected": true,
    "must_discover_schema_before_first_mutation": true,
    "is_valid": true
  }
}
```

`canonical_arguments` is validated in unit tests exactly as the parity
fixtures are (`SetPipelineArgumentsModel.model_validate` + trained-operator
plugin availability). `expected_topology` is **derived** from it by one
function (`battery_score.topology_from_arguments`) and stored for
readability; a test asserts derivation ≡ stored. Node ids and option values
are ignored by default; plugin names, node types, edge types, fork labels
and coalesce policy are exact. `scenario_from_example` (fixed to read the
plural `sources:` and to hard-fail on an absent source) is run as a
**cross-check**: its node multiset must be a subset of `expected_topology`'s,
or the scenario fails sanity.

### Floor rules (tool-bearing provider-call currency)

One tool-bearing provider call = one `llm_call_audit` row with a non-null
`tools_spec_hash`. Parallel tool calls batch inside a turn (cap 16;
verified offline-visible via `parent_assistant_id` grouping) — floors
reward batching deliberately.

- **discovery: 1** — all `get_plugin_schema`-class reads for the case
  (including `list_models` for LLM strata) are batchable in one turn. `list_sources/transforms/sinks` are prompt-supplied
  and cost 0.
- **data_setup: 0** — when the prompt asks the composer to invent data, the
  `create_blob` call batches into the discovery turn; the resulting
  `blob_id` is bound in the mutation turn. (This is why the single-source
  floor is 2, not 3.)
- **dependent_listing: +1** — only when a listing whose *input depends on a
  prior result* is required. The canonical listing tool is `list_blobs`
  (`list_composer_blobs` is a duplicate; either counts).
- **mutation: 1** — one `set_pipeline` authors the whole graph. Verified: it
  can author fork/coalesce/aggregation/multi-output topologies in one call.
  `apply_pipeline_recipe` delegates to `set_pipeline` and counts as the
  mutation; the run is flagged `recipe_used` for triage but not penalised —
  a matching recipe *is* the straightest path.
- **repairs = 0, backtracks = 0.**

Most floors are therefore 2. The `derivation` array is prose, one line per
component — it is what makes a later deviation attributable. Advisor calls
(`tools_spec_hash` null) never enter the floor; their count is reported per
run as `advisor_calls`.

## 3. Deviation taxonomy

Every excess beyond the floor must land in exactly one mechanical class.
Classification reads the captured `llm_call_audit` rows, `role=tool` rows
(with the assistant envelope's per-call outcome stamps) and validation
codes. Each event carries evidence: `sequence_no` range, tool name + args
digest, error/validation codes, `llm_call_audit` ordinal.

Mutation vocabulary (complete, from the web registry): `set_pipeline`,
`set_source`, `set_source_from_blob`, `set_output`, `upsert_node`,
`upsert_edge`, `patch_source_options`, `patch_node_options`,
`patch_output_options`, `remove_node`, `remove_edge`, `remove_output`,
`clear_source`, `splice_transform`, `apply_pipeline_recipe`, `set_metadata`,
`set_source_from_blobs`.
Data tools: `create_blob`, `update_blob`, `delete_blob`,
`wire_blob_inline_ref`.

| Class | Signal | Severity |
| --- | --- | --- |
| `excess_discovery` | re-read of already-fetched schema/state with no intervening mutation | soft |
| `schema_fumble` | repeated `patch_*_options` against the same node | soft |
| `data_rework` | `update_blob`/`delete_blob`/second `create_blob` for the same invented data | soft |
| `repair` | mutation with tool-row `success=false` (or envelope outcome `rejected`/`failed`) followed by a retried mutation | hard |
| `backtrack` | a mutation with `success=true` later undone by `remove_*`/`clear_source`/wholesale re-`set_pipeline` of the same target | hard |
| `wrong_shape` | `is_valid` final state whose topology ≠ `expected_topology` | hard (scored separately from path) |
| `decline` / `passivity` | no mutation + permission-seeking (RGR red criteria reused) | hard |
| `turn_exhaustion` | composition (30) or discovery (10) turn budget exhausted without valid state | hard |
| `wall_timeout` | 600 s wall budget exhausted without valid state (server terminal reason present) | hard |
| `approval_pending` | mutation persisted `success: true` with `status: APPROVAL_REQUIRED` (latent — default `trust_mode` is `auto_commit`) | hard |
| `provider_error` | any `llm_call_audit.status ∉ {success}` in the run | **excluded from rates**; run counted as `instrument_error` and the firing flagged if it recurs |
| `instrument_error` | zero tool-bearing calls, auth failure, unrecovered HTTP failure, `AuditIntegrityError` on read, truncated capture, missing server terminal reason on a non-valid end state | excluded from rates; **flags the firing** |

- **Clean run** = zero deviation events ∧ green criteria ∧ `is_valid`.
- **Optimal run** = clean ∧ tool-bearing calls == floor.
- Above-floor with no class fired ⇒ `unattributed_excess` — a visible
  taxonomy gap, never silently absorbed into "clean". A case whose
  `unattributed_excess` rate stays high across rounds is a candidate
  mis-derived floor and is re-reviewed.
- `repair` vs `backtrack` key on the **outcome discriminator** of the prior
  mutation (`success`/envelope outcome), never on "a prior mutating call
  existed".
- Interpretation-review rounds are counted (`review_rounds` in `score.json`)
  and reported; resolution is automated and not a deviation, but a
  review-after-repair cycle is signal.
- The soft/hard split exists for the report's histogram and for triage
  ordering; it does not change clean/optimal.

Taxonomy revisions are expected (case-by-case review). Because scoring is
offline over captured artifacts, revisions re-score history free **for any
class expressible from the captured channels** (thread rows, audit rows,
review payloads, state, validate, meta). Timing- and token-based classes
are expressible (audit rows carry `latency_ms` and tokens); anything
server-side that persists nothing is not, and would need a capture change
plus a new identity-block field.

## 4. Driver

`evals/composer-battery/drive_battery.py` (standalone Python, tracked).
Flags: `--base` (default `https://elspeth.foundryside.dev`),
`--round <name>` (e.g. `2026-08-20-baseline`), `--repeats` (default 5),
`--cases a,b,c` (comma-separated case names; omit for all), `--resume`,
`--cleanup`, `--order round-robin|case-major` (default **round-robin**:
repeat 1 of every case, then repeat 2, … so mid-firing drift is not
confounded with case index).

Per run:

1. **Login only, never register.** Credentials from the state dir
   (`battery_local` account, `credentials.json` mode 600). Hard-fail the
   firing if `access_token` is absent — never cache an error body
   (elspeth-2e5086dce6; a deliberate departure from `drive_graph.py`).
2. New session; `PATCH` title to `battery/<round>/<case>/<n>`; POST the
   verbatim corpus prompt. Client timeout 620 s — a deliberate 20 s margin
   over the server's 600 s budget (`COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS`
   is 660 s). On client timeout the server keeps composing — recover via
   `GET /messages` / `/api/sessions/_active`; never declare failure from
   the client side alone.
3. **Interpretation-review gate:** auto-resolve pending reviews as
   `accepted_as_drafted`; re-poll after any repair (a correction can stage
   new reviews); **bounded at 5 rounds** (as `drive_graph.py`), then the run
   is `instrument_error`. Capture every review payload to `reviews.json`.
4. Server-side `POST /validate?state_id=<final>` (pinned — validate is not
   pure; it can surface reviews and write a state) → capture to
   `runs/<round>/<case>/<n>/`:
   - `messages.json` — `GET …/messages?include_tool_rows=true&include_llm_audit=true&include_raw_content=true&limit=500&offset=…`,
     **paginated on `offset` until a page shorter than 500** (the cap is a
     head slice with no `has_more`); a run whose last page is full is
     `instrument_error: truncated`;
   - `state.json` (with `state_id`), `validate.json`, `reviews.json`,
   - `meta.json` (schema below).
   Capture failure is logged, never kills the round (`drive_graph.py`
   pattern); a run missing any artifact is `instrument_error`.
5. **Stop — no execute.** Path quality is fully determined at
   compose+validate.

`meta.json`:

```json
{
  "round": "…", "case": "…", "repeat": 3, "corpus_version": 1,
  "prompt_sha256": "…", "session_id": "…", "state_id": "…",
  "http": [ { "step": "post_message", "status": 200, "elapsed_ms": 1234 } ],
  "server_terminal": { "reason": "…", "source": "messages|_active" },
  "identity": {
    "substrate": "https://elspeth.foundryside.dev",
    "composer_model": "openrouter/anthropic/claude-sonnet-5",
    "composer_timeout_seconds": 600.0,
    "frontend_build": "index-….js",
    "server_version": "0.7.2",
    "budgets": { "composition_turns": 30, "discovery_turns": 10 },
    "tools_spec_hash": "…", "composer_skill_hash": "…",
    "model_returned": "…", "temperature": null, "seed": null,
    "driver_version": "…"
  }
}
```

`substrate`/`composer_model`/`composer_timeout_seconds`/`frontend_build`
come from `/api/system/status`; `tools_spec_hash`, `model_returned`,
`temperature`, `seed` from the first tool-bearing `llm_call_audit` row;
`composer_skill_hash` from the first interpretation event when present, else
the first-call `messages_hash` (verified stable across runs once, at
calibration); budgets from the operator (`deploy/elspeth-web.env`) until an
endpoint exposes them. Reports print the identity block; `--compare`
refuses on any mismatch.

Operational posture:

- **Serial in v1**, round-robin. A full 19×5 firing runs a few hours; the
  per-user rate limiter is in-process so other users' buckets are safe, but
  the OpenRouter key and `sessions.db` are shared — fire off-peak and say so
  in the round name.
- **Abort condition:** three consecutive runs ending `provider_error` or
  `instrument_error` halt the firing and flag it.
- **Resumable.** A ledger records completed runs; `--resume` re-verifies
  every artifact exists and parses before trusting the ledger.
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
  "case": "…", "repeat": 3, "surface_observed": "compose_loop|planner",
  "tool_calls": 3, "advisor_calls": 2, "provider_calls_total": 5,
  "floor": 2, "excess": 1,
  "deviations": [ { "class": "repair", "sequence_no": [41, 47], "tool": "set_pipeline",
                    "args_digest": "…", "codes": ["…"], "llm_call_ordinal": 2 } ],
  "review_rounds": 1, "recipe_used": false,
  "green": true, "red": false, "is_valid": true, "wrong_shape": false,
  "clean": false, "optimal": false, "excluded": null,
  "tokens": { "prompt": 0, "completion": 0 }, "cost": 0.0, "wall_ms": 0
}
```

`surface_observed` is derived from the presence of `planner_attempt_audit`
rows; a run observed on the wrong surface is `instrument_error: surface`.

`evals/composer-battery/report.py` — per round → `report.md` + `report.json`.
Headline first: clean rate, optimal rate, hard-deviation rate — **each with
`n` and the number of excluded runs beside it**, plus the canary's rate.
Then per-case rates (n/N clean, deviation histogram, median excess calls,
median review rounds), then the **deviation ledger** grouped case → class
with each event's evidence inline. `--compare <prev-round>` emits pooled and
per-case deltas, guarded on matching `corpus_version` **and identity
block**, and prints the MDE: at N=5 a per-case rate carries roughly a ±40 pp
95 % interval, so per-case deltas are shown but labelled *indicative*;
claims are made on the pooled ~95-run aggregate only.

The deviation ledger is the triage surface: the case-by-case "kit defect vs
hard problem" review reads typed events with evidence, not raw transcripts.
Deviations triaged to kit defects become Filigree issues manually — the
battery does not auto-file.

## 6. Testing and calibration

Unit tests under `tests/unit/evals/composer_battery/`, honoring
mutation-test-the-guard:

- **Scenario sanity:** every `scenario.json` well-formed; `canonical_arguments`
  validates as parity fixtures do; `expected_topology` ≡
  `topology_from_arguments(canonical_arguments)`; extractor cross-check
  subset holds; classifier decision ≠ EXPLICIT_MUTATION.
- **Synthetic ideal from the canonical payload:** the ideal state is built
  by converting `canonical_arguments` through the same args→state path the
  server uses (never by echoing criteria tokens); a synthetic thread at
  exactly the floor scores CLEAN + OPTIMAL. Catches unreachable oracles and
  impossible floors.
- **Classifier both ways, plus boundary and near-miss:** per class, a
  minimal thread that must trigger it, the ideal thread that must not, a
  **near-miss** (one flipped `success`) that must not, and **cross-class
  negatives** (`repair` fixture must not fire `backtrack` and vice versa).
  Boundary tests at composition turn 29/30, discovery 9/10, 599/600 s.
- **`wrong_shape` fixture:** an `is_valid` state with the right node
  multiset and a wrong fork label must fire; a state with different node ids
  and option values must not.
- **Instrument honesty:** zero tool-bearing calls → `instrument_error`;
  a full last page → `instrument_error: truncated`; planner rows →
  `instrument_error: surface`; any non-success `llm_call_audit.status` →
  `provider_error`, excluded from the denominator, firing flagged.
- **Report honesty:** `--compare` refuses on identity mismatch; `n` and
  exclusions render beside every rate.
- **Calibration firing before freeze:** one N=1 pass across all 19 cases.
  Calibration runs are corpus QA, never measurement — they enter no rate.
  Checks: surface observed = compose loop for every case; advisor
  discriminator holds (`tools_spec_hash` null ⇔ advisor model); first-call
  `messages_hash` stable across two runs of one case; floors reachable
  (at least one run at floor across the calibration, else re-derive).
  Ambiguity found ⇒ tighten prompt or widen floor for a structural reason,
  decision recorded per case with pre/post floors. Only then freeze
  `corpus_version: 1`.
- Full `pytest tests/` before merge (whole-tree AST gates; scoped runs prove
  nothing about them).

### Implementation obligations (verified 2026-08-16)

- Fix `scenario_from_example._extract_source` for the plural `sources:` dict
  and make an absent source a hard error (all 20 examples currently return
  `None`, silently).
- Give the RGR tool-row helpers public names before importing them.
- Author `battery_score.topology_from_arguments` and the topology comparator
  (no comparator exists for parity `semantic_expectations` today).
- Confirm at calibration: advisor rows have `tools_spec_hash` null;
  `create_blob` batches with discovery in practice (if the kit forbids it,
  that is a *kit finding*, not a floor change).

## Out of scope (v1)

- Executing composed pipelines (compose+validate only).
- The planner surface (v2 stratum with its own floor rules).
- Cases 13/14 until `set_pipeline` binds multiple blob-backed sources.
- LLM-judge triage verdicts; auto-filing tracker issues.
- Firing at the gov-domain production host.
- Parallel repeat execution.
- Guided-mode path quality.
