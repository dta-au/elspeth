# Composer Path-Quality Battery — Design Review (go/no-go)

**Reviewed:** `2026-08-13-composer-battery-design.md` @ `430b957b6`
**Date:** 2026-08-16
**Method:** seven independent review lenses (solution-design failure modes,
codebase reality/hallucination hunt, QA/test strategy, systems risk,
test-suite anti-patterns, determinism/re-score sufficiency, doc clarity),
each read-only against `release/0.7.2`; the load-bearing findings were then
re-verified by hand (extractor run over all 20 cases, chain-token matcher,
`llm_call_audit` envelope, live `/api/system/status`).

## Verdict

**NO-GO as written → GO after a bounded spec revision (rev 2), before the
implementation plan.** Not "abandon": the architecture (offline scoring over
immutable capture, mechanical deviation taxonomy, `instrument_error` /
`unattributed_excess` honesty, tracked corpus, calibrate-then-freeze) is
sound and worth building. But the spec's central artifact — the **floor** —
is currently defined in a currency the substrate cannot supply from the
rows the spec captures, and derived through an extractor that returns a null
source for **all 20 cases** and an oracle that two cases can **never**
satisfy. A plan written from the current text would encode those contracts.

Counts (deduplicated across lenses): **Critical 6 · High 8 · Medium ~14.**
Hallucinations found: 0 — every path, route, tool name and ticket id in the
spec is real. Every finding below is a cheap fix now; several become
expensive after the first firing.

## Critical — must change in the spec before writing-plans

### C0. The battery would silently measure TWO architectures, decided by prompt wording
`service.py:3236-3275`: freeform + structurally-empty state + an intent the
closed-grammar classifier calls EXPLICIT_MUTATION routes to
`_plan_and_stage_empty_pipeline` — the **planner** (own budget
`composer_planner_max_provider_calls` default 75, its own discovery/repair
counters, discovery turns persisted as `role="audit"` rows plus one
`PIPELINE_STAGED_*` message, no `role="tool"` rows for the taxonomy to read).
Everything else goes to `_compose_loop`. Dry-run of
`classify_pipeline_mutation_intent` (`no_tool_policy.py:804`) over the real
acceptance intents: the operator-voice gNN style the spec mandates classifies
AMBIGUOUS/CONVERSATIONAL → compose loop, while "Build a pipeline that
reads…" (g04p) → EXPLICIT_MUTATION → planner. So a corpus authored under §1's
rules would be **bimodal by accident**: some cases measure the planner,
others the loop, with different floors and different observable rows, and
the ledger would not say which. **Fix (decidable offline, do it first):** run
every corpus prompt through the classifier at authoring time; require one
surface for `corpus_version: 1` (compose loop, or the planner as its own
declared stratum with its own floor rules) and record the decision per case;
stamp the observed surface per run (`planner_attempt_audit` rows appear only
on the planner path and are exposed by `include_llm_audit=true`).

### C1. Provider-call currency: assistant rows ≠ provider calls (CONFIRMED false, not merely unverified)
§2 asserts "one assistant turn = one provider call". In this substrate
`assistant_rows ≤ provider_calls`, often far below:
- retries on 429/5xx up to `_LLM_API_MAX_ATTEMPTS = 3` collapse into one row
  (`web/composer/service.py:389`, loop ~7953–8011);
- the **advisor is mandatory** (`prompts.py:51-56`) and its checkpoint passes
  persist as `role="audit"` rows, never `role="assistant"`;
- no-tool / repair-injection turns persist no assistant row; a "last-chance"
  second call fires in the same turn at budget exhaustion;
- a `TIMEOUT` provider call still yields a *synthetic* assistant row
  (`contracts/composer_llm_audit.py:289-299`).
Floors of "2–3 calls" are therefore unreachable-or-wrong by construction and
`unattributed_excess` would fire on every clean run.

**The correct source already exists on the wire.** `GET …/messages?include_llm_audit=true`
returns `role="audit"` rows with `tool_calls[0]._kind == "llm_call_audit"`
(`sessions/routes/_helpers.py:1307-1321`), one per outbound model call, each
carrying `status` (`success/timeout/api_error/auth_error/…`),
`model_requested/model_returned`, tokens, cache tokens, `latency_ms`,
`provider_cost`, `tools_spec_hash`, `declared_tool_names`,
`planner_policy_hash`, `planner_call_ordinal`, `messages_hash`, `finish_reason`
(`web/composer/audit.py:316-344`).
**Fix:** define the currency as *count of `llm_call_audit` rows*, and decide
explicitly which roles count toward the floor (planner-only vs
planner+advisor — the advisor is mandatory, so either include its minimum
passes in every floor or exclude by role using `planner_call_ordinal`).
Re-derive every floor from a dry-run count (advisor: 1 EARLY + up to 2 END
passes × 2 attempts, plus up to 4 LLM-initiated hints — up to ~10
calls/compose, `config.py:311-312`). Note `llm_call_audit` rows carry no FK
to the assistant row (`models.py:454-457`) — turn attribution is
`sequence_no`-adjacency, state that limit. Delete the "verify assistant-row
≡ provider-call" obligation — it is settled.

### C2. Extractor null-source is universal, and fails open
`scenario_from_example._extract_source` reads a singular `source:` key; all
65 `examples/*/settings*.yaml` use the ADR-025 plural `sources:` dict, zero
use the singular. Verified: **all 20 cases return `source.plugin: None`**,
silently (`push()` drops `None` tokens). §2 frames this as a `fork_coalesce`
"settings shape" issue; it is corpus-wide. Cases 13/14 are additionally
inexpressible in the extractor's linear model (`multi_source_queue` has a
top-level `queues:`; `multi_flow`'s two flows flatten to one 2-transform/2-sink
chain). (`set_pipeline` itself CAN author multi-source/multi-flow/fork/coalesce in one
call — `sessions.py:1542-1782` — so the 1-mutation floor holds for 13/14;
only the oracle side is broken.) **Fix:** support `sources:` and hard-fail
on an absent source; extend or exclude 13/14 for `corpus_version: 1` and
record why; publish the
re-extracted table before any floor is derived.

### C3. Green criteria for cases 7 and 8 are unsatisfiable by any real composer output
`_ordered_chain_tokens` pushes a literal `"fork"` token for any gate with
`fork_to` (`scenario_from_example.py:335-340`); the RGR matcher compares
tokens against `plugin`/`node_type` only, with no `fork` alias
(`composer_rgr_score.py:108-121, 128-150`); the composer's fork is a
`gate` node with `routes: {true: fork}` + `fork_to` — "there is no 'fork'
node_type" (`web/composer/pipeline_planner.py:2165`). Both fork cases would
AMBER on green criteria on every run regardless of path quality, i.e. read as
`wrong_shape` forever. Worse, the §6 "synthetic ideal" test builds its ideal
state by echoing the chain tokens back as `node_type` values
(`tests/unit/evals/test_convergence_scenarios.py:136-185`), so it manufactures
a `node_type: fork` node the real system can never produce and **passes**.
**Fix:** the oracle must not be derived from the extractor's tokens alone,
and the synthetic ideal must be built by an independent path (see C4).

### C4. The oracle never asserts edge topology; a better tracked precedent already exists
`build_criteria_from_target` never consumes `gates[].routes` / `fork_to` /
`coalesce_nodes` even though the extractor pulls them — a right node-multiset
wired to the wrong routes scores clean, on a corpus stratified *by routing
shape*. Meanwhile **`evals/composer-parity/fixtures/*.json` is tracked (14
files) and holds ten canonical topology fixtures** — fork_coalesce,
multi_source_queue, error_routing, row_union, aggregation, conditional_gate,
linear_transform, multi_output, row_expansion, structured_llm — each with an
`intent`, a **validated `set_pipeline` `canonical_arguments` payload**
(`SetPipelineArgumentsModel`), `semantic_expectations` (node/**edge**/output
shape) and `runtime_assertions`. That is (a) the tracked-corpus precedent the
spec says it is inventing, (b) an oracle with edge topology, and (c) *proof*
that one `set_pipeline` can author the whole graph — the mutation-floor
premise. **Fix:** derive each battery case's `structural_target` /
green criteria from a per-case validated `canonical_arguments` payload (reuse
parity fixtures where the topology matches; author the rest the same way),
build the synthetic-ideal state from that payload, and reassess Decision 6
against building on composer-parity. Decision 6 currently records no
alternatives.

### C5. Capture omits the channels the scorer needs, and comparability is unpinned
- `include_llm_audit=true` is not in the fetch — a capture that omits a
  channel can never be re-scored for it; the spec's "server never needed
  after capture" claim is false without it.
- `limit` is hard-capped `le=500`, the slice is a **head** slice by
  `sequence_no`, and the response is a bare list with no `has_more`
  (`routes/messages.py:999-1048`) — long `non_convergence` threads silently
  lose their tail, exactly where `non_convergence`/late `backtrack` live.
  `max(sequence_no) == len(rows)` is NOT a valid completeness check (gaps are
  permitted, `service.py:4983-4985`); "last page shorter than `limit`" is.
- `POST /validate` is **not pure**: it can insert `interpretation_events` and
  a new `composition_states` row (`execution/routes.py:878-902`), and
  `ValidationResult` carries no `state_id`, so `state.json` and
  `validate.json` cannot be proven to describe the same state — the
  `--resume` case is exactly where this bites.
- Interpretation-review resolutions live in `interpretation_events` and no
  captured artifact holds them, yet §3 counts review rounds as path evidence.
- Nothing pins the instrument: `corpus_version` guards prompt/floor bytes only.
  Model snapshot (OpenRouter can repoint silently), tool set, skill kit,
  budgets, sampling config all float. `/api/system/status` exposes
  `composer_model`, `composer_timeout_seconds`, `frontend_build` (no git SHA);
  `tools_spec_hash`, `model_returned`, `temperature`/`seed` come free on every
  `llm_call_audit` row; `composer_skill_hash` (SHA-256 of
  `pipeline_composer.md`) exists durably (`schemas.py:918`).
**Fix:** fetch with `include_tool_rows&include_llm_audit&include_raw_content`,
paginate on `offset` until a short page, gate `--cleanup` on that check; pin
`?state_id=` on validate and record it; capture the interpretation payloads;
stamp `meta.json` with model_returned, tools_spec_hash, composer_skill_hash
(or first-call `messages_hash`, verified stable once), budgets/timeout, server
build/SHA, corpus prompt hash; make `--compare` refuse on any mismatch, as it
already does for `corpus_version`.

## High — should change in the spec

- **H1 Budgets.** 600 s is correct (verified live). "30-turn" is composition
  only: live is 30 composition + 10 discovery = **40**
  (`deploy/elspeth-web.env:10-11`; `EnvironmentFile` of
  `elspeth-web.service`). `_COMPOSER_PLANNING_SECONDS_PER_TURN = 15.0` (`config.py:53`) ⇒ 600/15 = 40
fundable vs 40 configured — exactly at the line, zero headroom (not
underfunded). Split
  `non_convergence` into `turn_exhaustion` vs `wall_timeout`; capture the
  server's own terminal reason and fail closed to `instrument_error` when it
  is absent (the 620 s client margin alone cannot tell "server hit budget"
  from "still composing when we gave up"). State the 20 s margin on purpose.
- **H2 Provider-error classification.** Decide now what a 429/5xx/overloaded
  provider outcome is (`llm_call_audit.status = api_error/timeout` gives the
  signal): `instrument_error` (excluded, flags firing) vs a deviation. Add an
  abort condition (N consecutive upstream errors ⇒ halt and flag). Also
  enumerate `AuditIntegrityError` on the messages read as an
  `instrument_error` mode.
- **H3 Test plan is weaker than existing precedent.** Require: boundary tests
  per class (turn 39/40, 599/600 s) in the style of
  `test_gov_pages_rate_cool_tool_call_efficiency_boundaries`; cross-class
  negatives (`repair` fixture must not fire `backtrack`, keyed on the
  `success` discriminator); a concrete `wrong_shape` fixture; near-miss
  threads (one flipped `success`); and a synthetic ideal built independently
  of the criteria (C3/C4).
- **H4 Statistics.** N=5 per-case rates carry ~±40 pp 95 % CI; per-case
  `--compare` deltas are noise-dominated. Report `n` beside every rate, state
  the MDE, reserve claims for the pooled 100-run aggregate; add one
  trivially-easy **canary** case whose non-optimal rate should be ~0 to detect
  a degraded firing; fire **round-robin by repeat index**, not case-major, so
  mid-firing drift is not confounded with case index.
- **H5 Goodhart / floor capture.** State as a non-goal that the floor measures
  *this corpus's* straightest one-shot path, not a prescription for composer
  behaviour with iterating operators; forbid citing battery deltas as sole
  justification for kit edits that reduce discovery/clarification; consider
  one iterative-construction case. Constrain post-hoc floor widening:
  pre-register the derivation rule, allow widening only for a stated
  structural reason, record pre- and post-calibration floors per case.
- **H6 Mutation vocabulary.** `backtrack` names only
  `remove_node`/`remove_edge`/`set_pipeline`; the freeform surface also has
  `set_source`, `set_output`, `upsert_node`, `remove_output`, `clear_source`,
  `set_source_from_blob`, `apply_pipeline_recipe` (RGR's own list,
  `composer_rgr_score.py:52-55`). Enumerate the full mutation set per class.
  Name the canonical listing tool (`list_blobs` vs `list_composer_blobs`;
  both exist on the web composer, neither on MCP). §1's "invent the data"
  rule drives the **blob** path, absent from floor and taxonomy; reported
  (not hand-verified) that `set_pipeline` cannot express named/multiple
  blob-backed sources (`_common.py:3055-3079`), making those cases 1+M
  mutation calls. Recipes are still live tools (`fork-coalesce-truncate-jsonl`,
  `split-by-numeric-threshold` map onto cases 7 and 4; `apply_pipeline_recipe`
  delegates to `_execute_set_pipeline`) — decide whether a recipe call is an
  optimal path or a distinct class; the call count alone cannot tell.
- **H7 Rot prevention.** Every predecessor harness accreted round-scoped
  scaffolding and was untracked (`136f2c703`); `ops-local/acceptance/` has no
  git history at all. Track `drive_battery.py`, `report.py`,
  `battery_score.py`; give `battery_score.py` a unit-test import dependency;
  add the `.gitignore` re-include for `evals/composer-battery/` **with** the
  credential re-exclusions composer-parity uses (`.gitignore:53-79`); state
  whether `runs/` is tracked (it will contain `meta.json` from a logged-in
  driver).
- **H8 Operational.** Rate limiter is per-user in-process (`middleware/rate_limit.py`),
  so other users' buckets are safe; the shared OpenRouter key and SQLite
  `sessions.db` are not. Name a firing window or an explicit
  shared-instance acknowledgement plus the abort condition in H2.

## Medium

- `scenario.json` has three `"…"` placeholders (`structural_target`,
  `red_criteria`, `green_criteria`); `score.json`/`report.json` have no
  schema; `--cases <filter>` syntax undefined; §3 "review rounds counted as
  path evidence" has no destination field in §5 — add `review_rounds`.
- The cited choreography precedents (`drive_graph.py`, `scripts/acceptance_battery.py`)
  are **register-primary and do not hard-fail on a missing `access_token`** —
  exactly the elspeth-2e5086dce6 bug. §4's "login only, hard-fail" is a
  deliberate departure; say so, or an implementer will copy the wrong half.
  Also confirm `PATCH /api/sessions/{id}` accepts `title` (needed for the
  `battery/<round>/<case>/<n>` prefix scheme and `--cleanup` scoping).
- Capture-failure ≠ run-failure discipline (as `drive_graph.py:155-189`);
  `--resume` must re-verify artifacts, not trust the ledger; bound the
  interpretation-review loop (drive_graph caps at 5).
- Under `trust_mode == explicit_approve` an intercepted mutation persists
  `success: true` + `status: APPROVAL_REQUIRED` while unapplied
  (`tool_batch.py:1347-1368`). Default is `auto_commit`
  (`sessions/models.py:306`) so this is latent for fresh battery sessions —
  still add an `approval_pending` class defensively.
- Exclusion rule gaps: `chroma_rag*`/`database_sink` need external services
  but no general rule excludes them; `multi_worker_showcase` ships chaos
  configs but escapes the `chaos*` name pattern; `blob_transforms` may be a
  missing stratum.
- `clean` vs `optimal` nearly coincide (every class but `wrong_shape` implies
  above-floor calls); say what the soft/hard split is for.
- RGR reuse: `_iter_assistant_tool_calls` flattens turn boundaries and the
  helpers are private — the per-turn parser is new code; give the helpers
  public names if load-bearing. RGR already implements much of the taxonomy
  (`max_repair_turns`, `set_pipeline_rejection_without_success`, …) — say so.
- Multi-variant claim: `fork_coalesce` (4 variants), `row_union_ab_experiment`
  (3), `openrouter_sentiment` (1) are multi-variant too (default
  `settings.yaml` is fine); `multi_query_assessment` ships a
  `criteria_lookup.yaml` fixture vs "no fixture files gate a firing".
- Wording: #12 uses `batch_replicate`; #20 "from batch results" overstates.
  Glossary (round/firing/case/run/RGR/gNN), success criteria for the
  instrument itself, ownership note, "~20" vs "20".

## What the design does well (real strengths)

`instrument_error` excluded from the denominator **and** flagging the firing;
`unattributed_excess` refusing silent absorption; offline scoring over
immutable capture; login-only with hard-fail on absent `access_token`
(elspeth-2e5086dce6 encoded exactly); client-timeout recovery via
`GET /messages` (matches real server behaviour); byte-verbatim prompt
discipline (cites `extract_intents.py:3-6` correctly); tracked corpus with
`corpus_version` gating; calibrate-then-freeze with per-case decisions
recorded; no LLM judge; serial-in-v1 grounded in a real incident; all 20
cases exist with the claimed shapes; both ticket ids real and precise; the
spec *admits* the currency gap rather than hiding it — the review sharpens
that admission.

## Recommended order for rev 2

0. Run all prompts through `classify_pipeline_mutation_intent`; pin one
   surface per corpus version (C0) — offline, do it first.
1. Fix `sources:` extraction, fail closed, re-run all 20, decide 13/14 (C2).
2. Define currency on `llm_call_audit` rows and which roles count; dry-run
   count one thread; re-derive floors (C1).
3. Rebase the oracle on validated `canonical_arguments` payloads; reassess
   Approach A against composer-parity (C3, C4).
4. Fix the capture set and identity stamps; `--compare` refuses on mismatch (C5).
5. Fold in H1–H8 and the doc gaps.
6. Then invoke writing-plans.

## Confidence / caveats

High on C1–C5, H1, H6, H7 (verified by direct reads and reproduction);
moderate on H4/H5 (methodological judgement); C1's *magnitude* (advisor
passes per compose) is unmeasured — the fix is the same either way. All
findings are static against `release/0.7.2`; nothing was fired, no file in
the tree was modified by any reviewer (one reviewer's stray write into
`docs/` was reverted; `git status docs/` is clean).

---

# Round 2 — review of rev 2 (`9558f27a8`), 2026-08-16

Same seven lenses, mandate: closure audit + attack rev 2's new claims + over-engineering check. Panel verdict: **GO-WITH-FIXES** — all rev-1 Criticals closed at the mechanism level, 0 hallucinations, but rev 2 introduced defects of its own. All addressed in **rev 3** (same file path).

## Rev-2 defects found (fixed in rev 3)

| # | Finding | Lens | Rev 3 answer |
|---|---|---|---|
| R2-C1 | Floor's `data_setup: 0` derivation (create_blob batches into discovery) contradicts the kit: `pipeline_composer.md:289-292` binds `create_blob` to the build turn, and `set_pipeline` has a first-class `source.inline_blob` path — the true 2-call route the spec never mentioned. "Kit finding, not floor change" was not honest when the kit is explicit. | sdr2 (verified by lead) | Decision 10: `inline_blob` is the optimal data path; `create_blob`→bind = soft `data_setup_detour`. |
| R2-C2 | `provider_error` = any non-success audit status excluded the run — but each retry attempt is its own row, so a recovered 429 voided an informative run and could false-trip the 3-consecutive abort; `malformed_response`/`bad_request_error` are model faults, not transport. | quality2, sdr2 | Split: `retried_provider_error` (soft, counted), `malformed_output` (hard), `instrument_error: transport` (excluded only when never recovered). Floor counts `status == success` rows only. |
| R2-C3 | `meta.json` kept HTTP status and discarded the 422 body — the only durable carrier of `turns_used` / `budget_exhausted` / `reason`; `GET /messages` writes no row on convergence failure and `/_active` excludes terminal phases. `turn_exhaustion`/`wall_timeout` were unmeasurable. | determinism2 | Capture the 422 `detail`; on client timeout fetch `/composer-progress` once; `server_terminal.source ∈ {422_detail, composer_progress, none}`. |
| R2-C4 | `surface_observed` by presence of `planner_attempt_audit` rows has a false-negative when the planner fails before persisting anything → reads as compose loop. The routing log line is server-side only. | quality2, reality2, systems2 | `surface_observed ∈ {compose_loop, planner, undetermined}`; planner iff `planner_call_ordinal != null` or planner rows; zero audit rows ⇒ `undetermined` ⇒ excluded, never defaulted. |
| R2-H1 | Extractor cross-check re-imports the `fork` token (C3 again as a unit-test failure). | sdr2 | `fork`→`gate` normalisation. |
| R2-H2 | `tools_spec_hash` null ⇔ advisor is false (diagnostics text call on the primary model; planner/guided carry tools). | sdr2, systems2, reality2, determinism2 | Three buckets: tool-bearing / advisor (`model_requested == advisor_model`) / other text; `advisor_model` in identity; characterization test on the advisor call path. |
| R2-H3 | Identity block untiered: `frontend_build` (SPA noise) refused; `composer_skill_hash` refused — but it is the independent variable a kit-edit comparison changes on purpose. | systems2, sdr2, quality2 | Binding vs recorded; skill hash recorded and printed as the delta under test. |
| R2-H4 | Oracle: fork labels are author-chosen strings ("exact" = false rejects); no correspondence algorithm once ids are ignored; coalesce `merge` missing; cardinality unstated; `option_values: ignore` too loose for threshold/top_k strata; `source`→`sources["source"]` mapping. | tsr2, quality2, sdr2 | Match = graph isomorphism, labels/ids ignored, `merge`+cardinality exact, `option_assertions`, mapping stated; comparator both-ways tests enumerated. |
| R2-H5 | repair/backtrack discriminator named raw `success` OR envelope outcome — ambiguous, and `cancelled` unhandled. | tsr2, determinism2 | Durable pair (`composition_state_id` + audit envelope) decides; envelope outcome corroborates; full 5-value vocabulary. |
| R2-H6 | Round-robin defeats the 3-consecutive abort for a single drifting case; canary at N=5 cannot detect degradation and only reports after the firing; repeat-1 cache-cold asymmetry unreported. | systems2, tsr2, quality2 | Per-case 2-repeat streak flag; canary as N=10 pre-flight; per-repeat bins with cached-token medians; 15 % exclusion flag. |
| R2-M | `provider_calls_total` misnomer + auto-title unaudited call suppressed only by PATCH-before-POST ordering; `wall_ms` unsourced; tokens null on failed calls; `report.json` schema missing; stale "100-run"; pooling formula; exact-500 follow-up fetch; `--resume` must never re-fetch; `--order` flag / `driver_version` / quarterly clause = ceremony; two file paths wrong; `provider_error` defined three ways; term drift. | doccritic2, determinism2, tsr2, sdr2 | All folded into rev 3; change log moved to an appendix. |

## Independently vindicated in round 2
`?state_id=` on validate is real (`execution/routes.py:855-858`); `planner_attempt_audit` rides the same `include_llm_audit` gate; mixed data+discovery tool batches are executed (`tool_batch.py:625`, cap is pure cardinality); `set_pipeline` authors every non-blob topology in one call; `set_source_from_blobs` exists as the 13/14 v2 route; pagination is idempotent (three INSERT sites, no DELETE/UPDATE on ordering columns, `MAX(sequence_no)+1` under a per-session lock); `composer_skill_hash` is SHA-256 of the *rendered* system prompt (better identity than the file hash the schema comments claim); the args→state path is hermetic for `path`-sourced payloads.

## Still open for the user
Decision 7 (compose loop only; the planner is where "Build a pipeline that…" requests go) — now with its selection tension stated in the spec.
