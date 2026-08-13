# Composer Path-Quality Battery — Design

**Date:** 2026-08-13
**Status:** Approved design, pre-implementation
**Home:** `evals/composer-battery/` (new), reusing `evals/lib/`

## Purpose

The Web Composer is an LLM tool loop: an operator states what they want, the
model builds it with tool calls. Success alone is a weak signal — the model
can reach a working pipeline while struggling (repairs, backtracking,
redundant discovery) some fraction of the time. This battery measures, for a
fixed corpus of ~20 operator-voice prompts, whether the composer takes the
**straightest path** to the answer, and when it does not, produces the
evidence needed to triage each deviation into:

- **affordance-kit defect** — our tool descriptions, schemas, or guidance gave
  incorrect or ambiguous direction; or
- **hard problem** — the case is intrinsically difficult.

Each case is *constructed to have a known optimal path*. Deviations from that
path — not failures — are the primary datum.

## Relationship to existing harnesses

| Harness | Question it answers |
| --- | --- |
| `evals/composer-harness/` | Does the composer succeed across personas? (May 2026 15-scenario sweep) |
| `evals/composer-rgr/` | Did this skill edit fix a known failure mode? (fast single-scenario iteration; corpus deliberately untracked) |
| **`evals/composer-battery/`** | **How far from optimal is the trajectory, and whose fault is a deviation?** |

The battery imports, not forks: `evals/lib/composer_rgr_score.py` (persisted
tool-row parsing, red/green criteria), `evals/lib/scenario_from_example.py`
(structural extraction from `settings.yaml`), and the API choreography lessons
from `ops-local/acceptance/drive_graph.py`.

**Deliberate divergence from composer-rgr precedent:** the battery corpus is
**tracked in git**. A standard battery's prompts are the instrument's
calibration; comparability across firings and across affordance-kit edits
requires the exact bytes to be versioned.

## Decisions (agreed 2026-08-13)

1. **Optimal reference = derived floor** — mechanically computed from the
   case's target shape, recorded with its derivation. Not hand-authored gold
   sequences, not empirical best-of-N.
2. **Repeats: N=5 default** — a `--repeats` flag, not a mode. Cost is no
   longer the binding constraint (official hosting exists), but N=5 gives
   per-case rates at 0–5/5 granularity and 100-run aggregates.
3. **Corpus: new, from `examples/`** — ~20 cases stratified by shape, frozen
   as `corpus_version: 1` after a calibration firing.
4. **Substrate: `https://elspeth.foundryside.dev`** (local live deploy) — the
   measurement is app-layer; battery traffic stays out of the gov-domain
   production DB.
5. **Triage: mechanical classification + evidence bundles** — no LLM judge.
   The affordance-vs-hard judgment is a reading exercise over the deviation
   ledger, reviewed case by case.
6. **Approach A** — new package reusing `evals/lib/`; composer-rgr and
   composer-harness keep their identities.

## 1. Corpus

### Case selection (20 cases, stratified)

Shape data verified via `evals.lib.scenario_from_example` extraction on
2026-08-13. `statistical_batch_plugins` is multi-variant (ten
`settings_<variant>.yaml`); the case pins variant `top_k`. Excluded strata: cloud-credential examples (`azure_*`,
`textract_inline`), chaos harnesses (`chaosllm*`, `chaosweb`), and
runtime-feature demos without a distinctive compose shape
(`checkpoint_resume`, `rate_limited_llm`, `multi_worker`,
`concurrent_scheduler`, `large_scale_test`, `retention_purge`,
`landscape_journal`).

| # | Case (example) | Shape stratum |
| --- | --- | --- |
| 1 | `transform_pipeline` | Linear 2-transform chain (`type_coerce` → `value_transform`) |
| 2 | `boolean_routing` | Single boolean gate, two labeled routes |
| 3 | `explicit_routing` | Transform + gate with named routes |
| 4 | `threshold_gate` | Numeric threshold gate, true-route split |
| 5 | `deep_routing` | 3 transforms + 5 chained gates (decision tree) |
| 6 | `error_routing` | Transforms + gates with error/quarantine routing |
| 7 | `fork_coalesce` | Fork gate (2 paths) + coalesce (`merge_results`) |
| 8 | `row_union_ab_experiment` | Fork (2 branches) + per-branch tagging + row union |
| 9 | `batch_aggregation` | Batch aggregation |
| 10 | `statistical_batch_plugins` (variant `top_k`) | Statistical batch plugin |
| 11 | `json_explode` | Reshape: one row → many (`json_explode`) |
| 12 | `deaggregation` | Reshape: deaggregation |
| 13 | `multi_source_queue` | Multiple sources queued into one flow |
| 14 | `multi_flow` | Two independent flows in one pipeline |
| 15 | `template_lookups` | LLM transform with template lookups |
| 16 | `multi_query_assessment` | LLM transform, multi-query assessment |
| 17 | `openrouter_sentiment` | LLM sentiment transform (provider-configured) |
| 18 | `llm_source` | LLM as the *source* |
| 19 | `schema_contracts_demo` | Explicit schema contracts + gate |
| 20 | `report_assemble` | Report assembly from batch results |

### Prompt authoring rules

- One **hand-written operator-voice prompt** per case, kept verbatim in
  `evals/composer-battery/corpus.md`. Extraction discipline follows
  `ops-local/acceptance/extract_intents.py` / round-3: the first unlabelled
  fenced block under each case heading is the intent, copied byte-for-byte to
  the wire. Ordinary phrasing is evidence; never paraphrase in transit.
- Prompts describe the **task, never the implementation** ("work both things
  out at the same time and bring them back together into one row", not "use
  fork_coalesce") — but pin the task tightly enough that exactly one pipeline
  shape is the reasonable reading. That property is what makes the derived
  floor legitimate.
- Where the calibration firing shows two defensible shapes for one prompt,
  either tighten the prompt or widen the floor — and record which was done,
  per case, in the corpus doc.
- Prompts that need data instruct the composer to invent it ("make up three
  products"), following the proven gNN pattern, so no fixture files gate a
  firing.

### Versioning

`corpus.md` carries `corpus_version: 1` after freeze. Any prompt or floor
change bumps the version. Reports embed the version; cross-version
comparisons are refused by tooling.

## 2. Scenario format and floor derivation

Each case gets `evals/composer-battery/scenarios/<case>/scenario.json`:

```json
{
  "case": "fork_coalesce",
  "example": "examples/fork_coalesce",
  "variant": null,
  "corpus_version": 1,
  "structural_target": { "…": "from scenario_from_example" },
  "floor": {
    "provider_calls": 2,
    "mutations": 1,
    "repairs": 0,
    "backtracks": 0,
    "derivation": [
      "discovery: schemas for csv, truncate, coalesce, json sink — one batched call",
      "listing: none — data invented by prompt",
      "mutation: single set_pipeline authors the whole graph"
    ]
  },
  "red_criteria": { "…": "reuse RGR passivity + build-failure sentinels" },
  "green_criteria": { "…": "derived from structural_target" }
}
```

### Floor rules (provider-call currency)

One assistant turn = one provider call; parallel tool calls batch inside a
turn. Floors reward batching deliberately — a composer that fetches three
schemas in one turn is genuinely straighter than one that serializes them.

- **discovery: 1 call** — all needed `get_plugin_schema`-class reads are
  batchable in a single turn (measured 2026-08-13: parallel 3-schema batch).
- **+1 call** only if the case needs a listing whose input depends on a prior
  result (`list_blobs`-class read).
- **mutation: 1 call** — a single `set_pipeline` authors the whole graph.
- **repairs = 0, backtracks = 0.**

Most floors land at 2–3 provider calls. The `derivation` array is prose,
one line per component — it is what makes a later deviation attributable.

### Implementation obligations (verified gaps)

- `scenario_from_example.extract_structural_target` returned
  `source.plugin: null` for `fork_coalesce` (2026-08-13 dry run). Fix source
  extraction for that settings shape before floors are derived; a floor built
  on a null source is a wrong floor.
- **Verify assistant-row ≡ provider-call** against a real captured thread
  before trusting any count (dry-run-the-analyser discipline). If the
  equivalence fails, derive provider calls from the session's audit surface
  (local substrate ⇒ read-only enrichment from the server DB is available).

## 3. Deviation taxonomy

Every excess beyond the floor must land in exactly one mechanical class.
Classification reads the persisted tool rows (`?include_tool_rows=true`) plus
validation codes. Each event carries evidence: tool-row ids, tool name + args
digest, error/validation codes, assistant-turn index.

| Class | Signal | Severity |
| --- | --- | --- |
| `excess_discovery` | re-read of already-fetched schema/state with no intervening mutation | soft |
| `schema_fumble` | repeated `patch_*_options` against the same node | soft |
| `repair` | mutation rejected (tool-row `success=false` / candidate reject) then retried | hard |
| `backtrack` | `remove_node`/`remove_edge`/wholesale re-`set_pipeline` undoing its own accepted work | hard |
| `wrong_shape` | valid final state that misses the structural target | hard (scored separately from path) |
| `decline` / `passivity` | no mutation + permission-seeking (RGR red criteria reused) | hard |
| `non_convergence` | 30-turn / 600 s budget exhausted without valid state | hard |
| `instrument_error` | zero provider calls, auth failure, unrecovered HTTP failure | excluded from rates; **flags the firing** |

- **Clean run** = zero deviation events ∧ green criteria ∧ `is_valid`.
- **Optimal run** = clean ∧ provider calls == floor.
- Above-floor with no class fired ⇒ `unattributed_excess` — a visible
  taxonomy gap, never silently absorbed into "clean".
- `instrument_error` institutionalizes the excision lesson: a battery that
  measured nothing must never read as a battery that measured clean. (Cf.
  the zero-provider-calls e2e efficiency gate; `ensure_account` trap
  elspeth-2e5086dce6.)
- Interpretation-review rounds are counted as path evidence (a
  review-after-repair cycle is signal), though review resolution itself is
  automated (below) and not a deviation.

Taxonomy revisions are expected (case-by-case review). Because scoring is
offline over captured artifacts, taxonomy changes re-score history for free.

## 4. Driver

`evals/composer-battery/drive_battery.py` (standalone Python, like
`drive_graph.py`). Flags: `--base` (default
`https://elspeth.foundryside.dev`), `--round <name>`, `--repeats` (default
5), `--cases <filter>`, `--resume`, `--cleanup`.

Per run:

1. **Login only, never register.** Credentials from the state dir
   (`battery_local` account). Hard-fail the firing if `access_token` is
   absent — never cache an error body (elspeth-2e5086dce6).
2. New session → POST the verbatim corpus prompt. Compose timeout 620 s; on
   client timeout the server keeps composing — recover via `GET /messages` /
   `/api/sessions/_active`, never declare failure from the client side alone.
3. **Interpretation-review gate:** auto-resolve pending reviews as
   `accepted_as_drafted`; re-poll after any repair (a correction can stage
   new reviews). Count review rounds.
4. Server-side `POST /validate` → capture to `runs/<round>/<case>/<n>/`:
   `messages.json` (`?include_tool_rows=true&include_raw_content=true&limit=500`),
   `state.json`, `validate.json`, `meta.json` (timings, HTTP outcomes).
5. **Stop — no execute.** Path quality is fully determined at
   compose+validate; execution measures the pipeline, not the composer.

Operational posture:

- **Serial in v1.** The composer's 600 s wall budget and the shared LLM rate
  bucket (tutorial-429 lesson, elspeth-06fec73e33) make parallel repeats a
  contention trap. A full 20×5 firing runs a few hours.
- **Resumable.** A ledger in the state dir records completed runs;
  `--resume` continues an interrupted round.
- **Session hygiene.** Sessions are title-prefixed
  `battery/<round>/<case>/<n>`. `--cleanup` deletes them post-capture;
  default **off** while the instrument is young.

## 5. Scoring and reporting

Both layers run **offline against captured artifacts** — the server is never
needed after capture.

- `evals/lib/battery_score.py` — per-run: parse the thread (reusing
  `composer_rgr_score` tool-row parsing), count provider calls, classify
  deviations, evaluate green/red criteria, compute floor delta →
  `score.json` beside the artifacts.
- `evals/composer-battery/report.py` — per-round: `report.md` +
  `report.json`. Headline first (clean-path rate, optimal rate,
  hard-deviation rate), then per-case rates (n/N clean, deviation histogram,
  median excess calls), then the **deviation ledger** grouped case → class
  with each event's evidence inline. `--compare <prev-round>` emits per-case
  deltas, guarded on matching `corpus_version`.

The deviation ledger is the triage surface: the case-by-case
"kit defect vs hard problem" review reads typed events with evidence, not raw
transcripts. Deviations triaged to kit defects become Filigree issues
manually — the battery does not auto-file.

## 6. Testing and calibration

Unit tests under `tests/unit/evals/` (existing pattern), honoring
mutation-test-the-guard:

- **Scenario sanity:** every `scenario.json` well-formed; a synthetic ideal
  trajectory at exactly the floor scores CLEAN + OPTIMAL (catches
  internally-impossible floors — same synthetic-ideal trick as the RGR
  suite).
- **Classifier both ways:** per deviation class, a minimal synthetic thread
  that must trigger it, and the ideal thread that must trigger nothing.
- **Instrument honesty:** zero-provider-call thread → `instrument_error`,
  excluded from the denominator, firing flagged.
- **Calibration firing before freeze:** one N=1 pass across all 20 cases.
  Calibration runs are corpus QA, never measurement — they enter no rate.
  Ambiguity found ⇒ tighten prompt or widen floor, decision recorded per
  case. Only then freeze `corpus_version: 1`.
- Full `pytest tests/` before merge (whole-tree AST gates; scoped runs prove
  nothing about them).

## Out of scope (v1)

- Executing composed pipelines (compose+validate only).
- LLM-judge triage verdicts.
- Auto-filing tracker issues from deviations.
- Firing at the gov-domain production host.
- Parallel repeat execution (revisit if serial wall time becomes the
  constraint).
- Guided-mode path quality (freeform surface only; guided is staged and needs
  its own floor semantics).
