# Operator rulings — 2026-09-23 (John, via AskUserQuestion in the lane session)

Intent (John, verbatim): "the intent here is the right fix not the quick fix"; "Fix all of them, but use a worktree with a review cycle."

- B1 + B2: ROUTE BOTH. Collision PCV (batch_replicate/batch_outlier_annotator) → plugin-side returned error.
  validate_batch_inputs PCV → narrow `except PluginContractViolation` after the Tier-1 re-raise at the
  aggregation AND collector flush seams → whole-batch failure routed via on_error / group failure. Rewrite the
  batch_contract_validation.py recorded decision and errors.py PCV docstring accordingly.
- B3: batch `on_error: discard` MATCHES per-row discard: (FAILURE, QUARANTINED_AT_SOURCE) via the atomic
  complete_barrier terminal_outcomes vehicle. Re-pin the affected recovery/orchestrator tests and re-derive §E.3a.
- B5: YES — one transform_errors row per buffered token of a failed batch (new leader-fenced writer, same table, no
  schema change); node_states.error_json stores the scrubbed reason dict.
- Related live aborts — FIX ALL IN LANE: missing required field at a batch node (KeyError abort); ContractMergeError on
  row-to-row observed type variance; value_transform non-scalar result TypeError; collector exception arm leaving member
  holds OPEN + config-admission gaps (batch plugin under transforms:, report_assemble as collector).

Session defaults (not asked; recommendations taken by the lane owner, revisit if wrong):
- B4 yes (error_hash from the scrubbed reason, not the constant). B6: None in batch_replicate/report_assemble becomes a
  batch failure; no new skip branch. B7 leave. B9 fix json_explode docstrings in lane WITH the manifest re-pin.
  B10 reject non-str in RAG template mode alongside C3. B11 judge policy untouched (operator's). G8 regression pytest yes.
- Narrowing aggregation on_error to discard-only is FORECLOSED by ruling d5034647f0.

## 2026-09-23 (later) — E2 frozen-oracle move ACCEPTED (John, via AskUserQuestion)
The value-free pydantic renderer (fix/5887-engine-r2-E2-needs-ruling @ 2067bbc93: loc[0] + pydantic-core type code only,
deeper loc -> [item], never msg) lands as built. The frozen oracle
retry-quarantine-discard-routed-errors/source-quarantine-routed moves (error_hash 00616901a7f46051 -> 8a4095b278791181;
text "id: Input should be a valid integer, unable to parse string as an integer [int_parsing]" -> "id: [int_parsing]").
Apply a scoped oracle write + manifest rotation AFTER the plugin-half manifest edits are on the lane, and record the
delta in the commit message. No dual renderer.

## 2026-09-23 (later still) — three more rulings (John, via AskUserQuestion)
- E2 loc[0]: MODEL-AWARE RENDERER. safe_validation_error_text(exc, schema) prints loc[0] only when the schema declares that
  field/alias, else a placeholder; all 13 call sites pass their schema; the 8 source plugin hash re-pins fold into the corpus
  rotation E2 already owes; accepted oracle text "id: [int_parsing]" must not move.
- Failure/discard counts (web discard summary, failure categories, failure samples counts, MCP analyzers): derive from
  TERMINAL OUTCOMES (token_outcomes), not transform_errors rows. Failed attempts stay queryable as attempt evidence but never
  inflate failed/discarded token counts.
- Resume of a batch whose FAILED verdict is already recorded: COMPLETE THE RECORDED VERDICT (route members to on_error /
  discard with the recorded reason) WITHOUT re-invoking the plugin. Crash timing must not change the outcome.
- Lane-owner decision: fold patches/fr2-R2-F1-shipped-json-source-nonfinite-test.patch into the E2 landing.

## 2026-09-24 — four more rulings (John, via AskUserQuestion)
- R2 type variance at a transform OUTPUT (observed): WIDEN. The node's recorded contract stays a truthful description:
  conflicting types join to `object`, None+T becomes T-nullable; nothing fails at the transform; downstream typed consumers
  fault rows (routed). Order/scheduling-independent. "Locked after first row" is retired for transform OUTPUTS only (sources
  keep invariant 6); coalesce keeps raising via a single type_conflict policy parameter on the merge.
- Collector group failures in reporting: GROUP-VERDICT ARM in the SAME counting authority module (terminal_transform_failures
  or its successor) reading recorded collector group-failure verdicts; categories/samples/MCP show them with their recorded
  reason. No fake transform_errors rows; no second module.
- Template render errors: ROUTE ALL NON-TIER-1. After the named arms in the single template renderer:
  `except TIER_1_ERRORS: raise` then `except Exception` -> row-level failure with a class-only, value-free message.
- Fix IN LANE (pre-existing live aborts): batch_replicate batch mixing rows with/without the optional copies_field
  (Tier-1 PassThroughContractViolation); multi-row aggregation release into a scope opener -> LandscapeRecordError (READY
  re-enqueue collides with a terminal item), present at 74c0ce0db.

## 2026-09-24 — landing coordination (John)
"smaller fixes will continue to land to the major pieces that just landed but otherwise you have a clean runway".
fix/5887-rebased (cut from release/0.8.1) is the SINGLE hand-off branch; the old lane branch is retired once the rebase
review passes. Re-rebase onto the latest release/0.8.1 immediately before the final full-suite gate so the gate evidences
the tree that would merge. Stop at ready-for-merge; never land.

## 2026-09-25 — S1 re-ruled after the specialist panel (John, via AskUserQuestion) — SUPERSEDES "R2 = WIDEN"
Panel: PANEL-S1-S3-synthesis.md (6/7 objected to WIDEN; WIDEN mutates a locked Tier-1 record mid-run, masks plugin drift,
and relocates the abort to the sink).
- S1 = DECLARE, DON'T INFER at transform outputs: operator type > plugin type > `any` (nullable), fixed before row 1,
  declared types CHECKED AGAINST VALUES (violation routes, value-free). Narrow J1 join (equal→keep, different→object,
  nullable OR) ONLY at the sink batch merge and display headers. Coalesce and node evolution keep raising. NO type_conflict
  parameter. The "None+T -> T nullable" rule is deleted. ADR owed for the doctrine change. Execute synthesis §S1.5 (amended
  spec). Fixes the live defect (declared page:int delivered as str, exit 0) and the multi-source sink abort.
- A plugin's OWN computed value breaking its own declared type: ROUTE as a PCV now; record an authorship bit (computed vs
  carried); reconcile data-trust guide:341 ("plugin returns wrong type -> CRASH").
- Declaration location: RUNTIME STAMP now (composer/planner projection unchanged); promotion into the flexible output schema is
  a follow-up behind the composer/runtime agreement test.
- Sweep scope: FULL SWEEP IN LANE — concrete types for plugin-computed fields across the 22 transforms, INCLUDING binding the
  LLM output_fields types with Tier-3 numeric coercion at parse (5.0->5 for integer, 7->7.0 for number) — bound and coerced
  together, never one without the other.
- Lane-owner (per "FIX ALL IN LANE"): the four newly surfaced shapes are in lane — multi-source sink abort, field_mapper
  dotted extraction (observed), value_transform as a typed consumer (Tier-1 on a valid row), declared-type-not-enforced.
- Prerequisite (panel B6): the R3 value_transform pin and ef8a87a8f None consolidation must be on the hand-off branch —
  verified carried by the rebase? (check impl-REBASE.md before starting S1).

## 2026-09-25 — S2 / S3 rulings after the panel (John, via AskUserQuestion)
- S2: re-target to upstream's `collector_group_failures` table (69c57f943). The group-verdict arm lives in the SAME counting
  authority module (terminal_transform_failures.py): `deciding_collector_group_failures` (member tokens) +
  `failed_collector_groups` (groups). Upstream's THREE direct counts (run_status_projection, web accounting/service, MCP run
  summary) are REWIRED through it. MCP errors.total = validation + transform + collector member tokens. Closed reason enum
  with a CHECK; group_id in the hold. Execute synthesis §S2.3.
- Landscape epoch 45 is NOT deployed anywhere: fold the CHECK + group_id into epoch 45 (no bump).
- S3: catch-all lives IN the spawned template worker with a setup/render PHASE SPLIT: setup failures + protocol invariants
  raise FrameworkBugError (abort); render-phase non-Tier-1 failures route class-only; EOF means worker death only (SIGXCPU
  routed). Config-literal template errors (literal truncate(n<3), unknown filter/test names) are rejected at template
  CONSTRUCTION (validate/preflight), never routed per row. _UndefinedContractError registered Tier-1. Execute synthesis §S3.3.
- S3 precondition: CLOSE THE SANDBOX SURFACE FIRST — templates must see plain row values, never owned PipelineRow /
  SchemaContract API; after that a Tier-1 raised during render is our bug and aborts (except TIER_1_ERRORS: raise stands).
- Lane-owner (RC-9, "right fix"): template-worker capacity exhaustion → backpressure (never a permanent row fault); non-CPU
  worker deaths → retryable or FrameworkBugError, never permanent quarantine; wall clock starts when the child is ready.
  Own unit after S3.
- Commit trailer: name the model that actually authored the commit. SUPERSEDED 2026-09-26 (lane owner): the session switched
  to Opus 5.5 on 2026-09-25, so lane commits correctly carry "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>".
  A reviewer must NOT flag the Opus trailer; this line was the lane owner's stale note, not an operator ruling.

## 2026-09-25 — custody and landing (John)
"you have sole custody of the repo now and your priority is getting your fixes tested and merged".
=> This lane may MERGE into local release/0.8.1 after a passing full-suite gate (ruff,mypy,contracts,lints,pytest +
testcontainer) on the rebased tree. PUSH still needs an explicit go-ahead. Priority: land what is done and reviewed first,
then continue the remaining units on top, landing each batch behind its own gate.

## 2026-09-25 (later) — rebase reconciliation rulings (John, via AskUserQuestion)
- C2: an operator-TYPED value_transform target is the OUTPUT DECLARATION (operator > plugin > any); a computed value of
  another type routes value-free. Lane reading kept; upstream 63a2e1825's input-only reading is superseded for typed targets.
- C3: an int value SATISFIES a float declaration (numeric tower; JSON has one number type). Unify SchemaContract.validate to
  accept int for float (ONE rule, matching pydantic); no value conversion; the recorded float declaration is then true.
  bool is NOT an int/float for this purpose (keep the exact-type bool exclusion).
- docs/reviews/2026-09-25-composer-browser-acceptance.md:17 (upstream 63ffb7ee1, already on origin) carries a retired staging
  credential token: REDACT it in the lane (placeholder) so the gate is green; it remains in published history.

## 2026-09-25 — field-name spelling rule (John, via AskUserQuestion), after systems-spelling-sweep.md
CANONICAL NAMES IN DECLARATIONS, HEADER SPELLINGS REJECTED — generalise the source-only authority
(check_declared_fields_reachable / SchemaContract.resolve_name) to every declaration surface: transform and sink schema
fields, required_input_fields, and plugin field options that feed declared_input_fields (rag query_field, web_scrape
url_field, blob_json_expand blob_ref_field, and any others the sweep finds). A declaration must use the normalized name
reachable from the node's upstream contract; an original-header spelling is REJECTED at validate/build time with the
source's actionable message ("headers are normalized … declare 'name'"). Composer/runtime agreement enforced. Row LOOKUPS
(field_mapper mapping sources, expression row['Name'], PipelineRow access) keep resolving either spelling. schema_factory
keyed by normalized name. Scan and fix examples/, the scenario corpus and composer teaching that use header spellings in
declarations in the same change. This SUPERSEDES the spelling-by-spelling patches in field_mapper's carried/declarer
machinery — remove what the general rule makes dead.

## 2026-09-26 — Q1 epoch (John): "this is a dev box, it doesn't count as deployed, it counts as dev"
elspeth.foundryside.dev is John's DEV box, not a deployment. The epoch-45 fold stands as committed (a05cfb2a9); NO bump to 47.
A dev store refused at startup is recreated. Reviewers must not re-raise this.

## 2026-09-26 — lane-owner decisions (obvious calls; John: these "shouldn't actually be asked")
- Q2 S3b: followers honour the run's settings.retry (RuntimeRetryConfig.from_settings; one retry authority). Start from
  patches/fr2-S3b-F1-follower-retry.patch; owe a dedicated follower retry test + scoped testcontainer run.
- Q3 S4: accept the TRANSFORM-mode partial-field-drop gap as inherent to ADR-009 §Alternatives #2; one factual ADR sentence.
- Q4 S7: (b) moves to the SPELLING-RULE unit (declaration surface, refused at build); (c) routes each row via on_error with a
  value-free reason naming the canonical spelling; S7 closes on (a).
- Q5 S0: replace the static whole-row Jinja gate with a RUNTIME PROJECTION — the render context's row holds ONLY the fields
  the node declares it reads (required_input_fields / declared template refs); opt-out `[]` keeps the whole row. The static
  extractor stays only as an early advisory error, not load-bearing. Revert the carrier/macro/loop modelling it makes dead.

## 2026-09-26 — specialist review of Q2–Q5 (all 6 reported: all AMEND, none needs_operator) — AMENDS the lane-owner decisions above
Reports: specialist-q2-engine.md, specialist-q2-systems.md, specialist-q3-contracts.md, specialist-q4-doctrine.md,
specialist-q5-architecture.md, specialist-q5-template-safety.md. Binding for the implementing units.

Q2 S3b follower retry (q2-engine + q2-systems — both measured the SAME blocking collision independently):
- Keep (a). The prototype is NOT landable alone: with followers retrying, a lease rotation after an in-claim retry re-claims
  at offset `claimed.attempt-1` and collides on UNIQUE(token_id, step_index, attempt) → Tier-1 LandscapeRecordError, token
  left with NO terminal outcome (measured, scratch/specialist-q2-engine/). SAME CHANGE must derive the READY-claim attempt base
  from the Tier-1 record: `base = max(claimed.attempt - 1, get_max_node_state_attempts(run, [token])[token] + 1)` when
  claimed.attempt > 1 — PER TOKEN, not per node. No bypass flag on the pending-sink offset guard (comment arm only).
- Wire `follower_shutdown_event` to seat loss / terminal run, or record the bounded delay in one sentence (implementer's choice,
  prefer wiring). Docs: flip configuration.md "Worker lost" paragraph; one line in ADR-030 prose. Fix the patch's import order.
- Tests owed T1–T5 of the report (retry→rotate→reclaim RED on the prototype; multi-node; leader claims rotated item; leader vs
  follower parity incl. exhaustion reason; lost render worker on a real follower) + scoped testcontainer run.
- max_delay_seconds cap below the lease: optional hardening, not required.

Q3 S4 (q3-contracts):
- Keep (a) for the inherent shape (partial carriers among EMITTING inputs; needs attribution, rejected by ADR-009 §Alt #2).
- AMEND: the quarantine-dilution sub-shape is NOT inherent — in TRANSFORM mode intersect only over inputs not in
  `validated_quarantined_indices(...)` (compute once, share with routing); non-empty emission with all inputs quarantined →
  OrchestrationInvariantError; zero-emission branch unchanged. Same rule on the resume re-check (processor.py ~4534); test it.
  Tests: drop-tag-diluted → exit 4, honest-diluted → no violation, mutant restoring the all-token intersection → red.
  Re-run the batch-flush benchmark. MOVE the stale batch_replicate.py:165-167 sentence into the engine docstring of
  _cross_check_flush_output and delete it from the plugin — keeps S4 engine-only apart from that deletion's one re-pin.
- ADR-009 note uses the report's Sentence A (scoped to undeclared, source-inferred fields); record (b)'s measured false
  positive (in-batch quarantined sole carrier → honest exit 4) and name the compensating exact-row tests.

Q4 S7 (q4-doctrine):
- (b) → SPELLING-RULE unit, endorsed, with conditions: the spec NAMES type_coerce.conversions[].field and projects it onto
  declared_input_fields; its runtime residual ROUTES (not the Tier-1 SchemaConfigModeViolation abort); CHANGELOG + composer
  teaching line for the observed-mode behaviour change.
- (c) AMENDED to the house two-surface pattern (validate_transform_output_field_collisions): ONE shared predicate
  `T ∉ names ∧ normalize(T) ≠ T ∧ normalize(T) ∈ names → normalize(T)`; BUILD refuses where the upstream vote participates
  (source-style message, composer/runtime parity mirror); RUNTIME residual (abstaining upstream) routes with stable reason
  `target_is_header_spelling`, literal T + canonical only (value-free), detected BEFORE the write/with_field — never a
  try/except around with_field. Trigger is the predicate (covers probe B: header NAME, target Name — silent shadow today).
- Both halves live in the SPELLING-RULE unit (one authority). S7 closes on (a).
- NOT asked (deferred, the sweep's §2.8): "must operator-created names be normalization fixed points?" — the amendment does not
  depend on it.

Q5 S0 → runtime projection (q5-architecture + q5-template-safety), direction endorsed by both:
- Key the projection on the OPERATOR DECLARATION only: `required_input_fields` (multi-query `source_row`: the node's
  required_input_fields, config-proved to cover every query's input_fields). Never extractor output, never
  declared_input_fields. Filter name_index() to declared targets so both spellings of a declared field resolve.
- `None` → EMPTY row; `[]` → whole row; list → exactly those. Pin all three.
- Project PARENT-SIDE before `_pack_context_value`, for every PipelineRow in the context incl. nested multi-query source_row;
  the projection is a required typed argument (explicit field set | AllFields marker); an unprojected PipelineRow reaching the
  packer = FrameworkBugError; extend S3's W10 call-site pin (no `to_dict()` into a render). Test the transport bytes.
- web/plugin_policy/coverage.py protected set = the declaration (required_input_fields ∪ multi-query input_fields values);
  `[]` and None unprovable → fail closed. Same unit (it is a security control certifying falsely today — measured).
- Revert ONLY code whose sole consumer is the whole-row gate (`_template_uses_whole_row`/`whole-row` kind, `row_attribute`
  rewrite + its multi-query branch, the message entry, the docs refusal list). KEEP eef47097b (release hang fix) and
  `carrier-limit`. KEEP 0fe7412ce's varargs/kwargs/caller binding minus its whole-row reporting. Criterion: delete a function
  only if a mutation deleting it reddens nothing but whole-row tests.
- The extractor stays a HARD config refusal (early error / teaching signal); it is no longer the confidentiality boundary.
- Undeclared read after projection: its own value-free reason ("reads a field this node does not declare in
  required_input_fields"), distinct from an absent declared optional field. `'x' in row` / `row.get('x', d)` on an UNDECLARED
  name raise that error; on a declared-but-absent name keep False/default. Length/iteration see declared names (documented). This deliberately
  deviates from the Mapping contract for __contains__/get — record it in the ADR; test that dict(row), **row, items,
  dictsort still work on a projected row.
- `_variables_hash` hashes the PROJECTED view (it then means what its docs say; no failure from NaN in an undeclared column).
- RAG/azure_ai_search `query_template` INCLUDED in the lane (fix-related-sites intent) as its OWN unit RAG-projection right
  after S0-projection (same ADR): LLM-shaped validators on RetrievalOutputConfig
  (refs ⊆ required_input_fields ∪ {query_field}; None + refs refused; declared-but-unreferenced dual) with composer agreement,
  then the same projection (pass the PipelineRow, not to_dict()).
- MQ-ALIAS-COLUMNS closes by construction at RUNTIME (the boundary keys on the declaration). The composer's literal
  source_row column check STAYS as the early config error (config already proves required_input_fields covers it,
  llm/base.py:875-887) — do NOT delete it. CARRIER-PRECISION (QUEUE) likewise closes by construction.
- EXTRACTOR-ALIGN (QUEUE) decided: keep the reserved row-api names reserved by policy (the extractor is fail-early UX;
  relaxing it is a later UX decision). No work.
- Keep TemplateRow's Mapping base; build `_values` from declared keys so __iter__ and names agree (closes r1 Finding B).
- NEW ADR "a template sees only its declared fields" (amends ADR-013, ADR-040): items (a)–(g) of the architecture report,
  with RAG now in scope rather than an exception. ADR before merge.
- Tests: r1–r3 leak corpora (1+7+19, r3's 85 forms, r2 macro corpus) become runtime projection tests with 0 sentinel hits;
  mutation dropping the projection → red; end-to-end `elspeth run --execute` with ChaosLLM on the r-round repro configs.

Q2 additions from q2-systems (binding with the q2-engine items above):
- Offset rule: gate on `claimed.attempt > 1` (else the source's attempt 0 pushes every first claim to 1); TOKEN-scoped max.
  q2-engine adds: apply it only on the codec path (resume_attempt_offset == 0) or compute ONE total base — never add
  max+1 twice for restored resume items (would move pinned resume tests). Reconcile the pending-sink comment at
  scheduler_drain.py ~903-907 (it deliberately STEP-scopes) with the reclaim path's token scope: state the tradeoff
  (uniqueness beats attempt-number fidelity on reclaim) in one place.
- Pin the probe (scratch/specialist-q2-systems/test_q2_probe_retry_then_reclaim.py) as a real test with a SAME-follower
  and an OTHER-follower reclaimer; mutant reverting :644 → red. It also closes the latent leader crash + resume variant.
- MEASURE (information gap): after a reclaimer Tier-1, does the live Orchestrator finalisation refuse a run whose token has
  no outcome? The probe showed repository-level complete_run(COMPLETED) ACCEPTED it. If live finalisation also accepts it,
  that is a second defect in this unit (a run must never complete with a non-terminal token) — fix it.
- A2: build_row_processor raises when a follower retry config is passed for a non-FOLLOWER mode (one authority both ways).
- A4: follower retry tests end in success AND in retry_exhausted, separate from the reclaim test.
- W1: the two reviewers DISAGREE on lease refresh during retries (engine: heartbeat after every attempt via
  _verify_ownership_before_terminal_audit; systems: only between node iterations). Measure which is true, correct the
  impl-S3b claim, and add a configuration.md sentence on retry wall time vs lease + stall budget. Validation warning optional.
- Out of scope, recorded: aggregate provider rate is (N+1)×rpm without a shared limiter (pre-existing); no retry telemetry
  (on_retry) in either mode; node_states has no worker column.

## 2026-09-26 — lane-owner decision on E1's needs_ruling (outcomeless FAILED item at resume)
Context: c79757aa9 makes complete_run refuse a run whose FAILED work item's token has no outcome (was: silently COMPLETED,
measured). Left alone, resume and abandon both refuse → no operator exit. DECIDED: option A2 — the LIVE drain keeps its
pinned disposition (an exception escaping a claim → mark_failed + re-raise; test_plugin_timeout_keeps_ordinary_failure_disposition
stays), and RESUME returns each FAILED item whose token has NO terminal outcome to READY through a NEW explicit, recorded
scheduler event, then re-drives it at the collision-free claim base. Reasons: every row reaches a terminal outcome; crash
timing must not change the outcome (SIGKILL parity); resume is the operator's "bug fixed, continue" step; replay of an
external effect is the at-least-once contract ADR-030 already states for takeover. A FAILED item WITH an outcome is decided
and is never re-driven. A1 rejected (reverses the pinned live contract; every exception crash waits out a 300 s lease).
B rejected (new row-level abandon mechanism for no gain). Epoch: the new event type under the CHECK folds into the current
epoch 46 IF a pre-fold store is REFUSED at open (measure it, as S2's fold did); if the schema validator cannot see the
difference, bump to 47 instead — never a store that opens and then fails at the first insert.

## 2026-09-27 — lane-owner decision on P4's needs_ruling (batch plugins under output_mode: passthrough)
DECIDED option A. A class-level capability declaration on every batch-aware plugin states whether its flush emits exactly
one row per buffered row (the ONE authority), read by BOTH surfaces (runtime build + composer, parity-pinned); the composer's
by-NAME rule for batch_replicate is replaced by the declaration (no name dispatch). Reducers (batch_stats, …) and
batch_replicate are refused under passthrough at build with an actionable message ("use output_mode: transform").
Reasons: config-determined failures are refused at construction (S3 doctrine); the composer already refuses batch_replicate
outside transform mode, so A is the parity-preserving choice; identity-replicate (one copy) is a no-op whose only users are
tests — tests do not shape behaviour; they move to a genuine 1:1 plugin (e.g. batch_outlier_annotator) with the same
coverage intent. B rejected (instance-level check keeps a pointless config legal and adds a second rule shape). C rejected
(pays a config error per batch). RUNTIME residual: a plugin that declares 1:1 but returns a different count is a plugin bug
→ still Tier-1, but every buffered token is recorded FAILED before the abort (E4's pattern) — never outcomeless.

## 2026-09-27 — John: newly refused configs that "worked" are not a loss
"the three worked are technically 'only worked because we didn't realise they shouldn't' - its not a meaningful loss of
functionality for an improvement in overall rigour." Applies to: header spellings as declarations, RAG query_template row
reads with no declaration, names-only template forms over undeclared fields. No compatibility path, no deprecation window;
CHANGELOG states each refusal. OWED before merge: ONE refusal inventory (surface, error code, before-behaviour, pinning test,
composer teaching line) for the release notes, plus a check that the planner teaching covers every refusal.

## 2026-09-27 — John: merge authorisation
"you're authorised to merge to 0.8.1 once you've finished your line of effort, the composer fixes will merge onto you and
conform where necessary." → When the lane is finished (round 7 + refusal inventory + Codex re-review + re-rebase + full gate
PASS with every red attributed), merge into release/0.8.1 WITHOUT asking again. The lane lands FIRST; John's composer work
rebases onto it. PUSH is not covered by this — still ask.

## 2026-09-27 — John: "remember we have a no techdebt policy, make sure the functionality is correct first, consult systems thinkers or other experts where needed"
Applied to the F2 refusal-inventory non-convergence: the inventory is PAUSED. Every release-vs-HEAD behaviour change is first
judged for CORRECTNESS (panel2: coalesce-contracts, template-api, systems → specialist2-*.md), wrong behaviour is fixed, and
only then is the inventory written from a systematic differential instrument, not hand enumeration. The 3 F2 commits
(badb15cff, d3c925d69, 95ee47fe1) stay as a draft to be corrected against the instrument's output.

## 2026-09-27 — panel2 verdicts (specialist2-*.md; none needs_operator) — binding for round 9
- Coalesce group failures: ONE helper surfaces an outcome per consumed token on every arm; live == audit (pre-existing exit-4
  bug, also on release). union_collision_policy: fail — certain-from-config collision refused at build (composer + runtime);
  data-dependent collision routes per row (lane-owner decision, trust tiers + S3).
- S1 "coalesce keeps raising" STANDS. Certain conflicts (both branches know a type incl. declared any, both guarantee presence)
  are REFUSED AT BUILD via the stamp table promoted onto NodeInfo + one predicate for build and composer; only the
  observed-upstream residual routes.
- Template row API: attribute/item always read a field; `get` is the only method; whole-row ops are filters/builtins over the
  projected view; row-API/method checks apply unconditionally (incl. []); no config-green-rows-fail shape.
- OPEN, under arbitration (specialist2-arbiter.md): what an untyped computed rewrite declares — `any` (coalesce-contracts) vs
  static result typing over build-declared inputs (systems: `any` erased 31 known types, 7 in shipped examples).
- Instrument: behaviour-diff (agent tooling in lane scratch, never tests/ or docs/); the inventory is written only from
  clusters judged deliberate; regressions become fixes.

## 2026-09-27 — arbiter verdict + Codex final review (binding)
- ARBITER (specialist2-arbiter.md, needs_operator false): option (3). A computed value_transform target declares the type its
  expression PROVABLY computes (one core derivation over the expression grammar; inputs = the node's declared schema fields +
  upstream types the build resolves via resolve_guaranteed_field_type, bound once in the builder's topological pass, recorded
  in the ONE stamp table); nullability DERIVED, not assumed; `any` only when genuinely unknown. Operator-typed target keeps
  the operator type (C2); an authored `any` stays `any`. type_coerce PUBLISHES its conversions to the same table in the same
  change. Plugin tier of "operator > plugin > any" — no ruling reversed. co2 then delivers (fixed, not refused); re-cut the c6
  control on a genuinely unknown expression. Depends on G2's stamp-table promotion.
- CODEX FINAL (codex/codex-review-final.md, FAIL, 3 majors, all to fix in lane):
  C1 source field_mapping alias bypass — the spelling predicate compares normalize(header) instead of resolving the header
     through the upstream contract (Name → b), so field_mapper {Name: given} with `Name: int?` delivers a str under a recorded
     int, exit 0; composer and runtime both admit. Fix at the resolution authority (SchemaContract.resolve_name), not by a
     second predicate.
  C2 RAG `{{ row | tojson }}` under [] fails every row (TemplateRow not JSON-serialisable); release delivered. Whole-row
     filters over the projected view must all work (tojson included) — G3's API rule must cover the full Jinja builtin filter
     set, measured.
  C3 type_coerce CoercionError.reason embeds the row value in transform_errors (inherited from 63ffb7ee1): value-free reasons.
     SWEEP the class (every plugin/engine reason or error text that can interpolate a row value) with a sentinel run, not grep.

## 2026-09-27 — lane-owner decision on G1's needs_ruling (E7 frozen-oracle move)
DECIDED option B: the union-collision-fail corpus case and examples/fork_coalesce/settings_union_fail.yaml are reshaped to the
DATA-DEPENDENT collision shape (observed source, distinct markers per branch) so they stay run cases; the oracle then records
every row routed as union_field_collision with the collision record on each FAILED hold (the audit demonstration the example
exists for). The certain-from-config refusal is pinned separately (already: test_collision_certain_from_config_is_refused_...,
e1 CLI). Reason: the old oracle pinned a defect (abort at row 1); B keeps the example's purpose. Start from
patches/G1-E7-union-collision.patch; manifest rotation + _EXPECTED_COLLISION_UNION re-pin + production-path special cases;
four doc surfaces; CHANGELOG. No other oracle may move (prove it).

## 2026-09-27 ~19:00 — lane-owner decisions from the Astra guidance verification
- P-03 is a LANE REGRESSION, not guidance (measured, scratchpad p03/: release exit 1 routed, lane exit 4 abort). Astra's
  "code-defect classification refuted" is overturned: Astra compared against the Q4 header-spelling residual, not the
  pre-lane `missing_field` route. Fix via the whole-class R2 design (DESIGN-R2-declared-input-miss.md): an ADR-013 miss the
  build could not prove routes (John's B2 principle + Q4 doctrine §4 cond. 2); a miss the build proved stays Tier 1.
  Amends ADR-013's tier section — PENDING specialist review (architect + systems) before implementation; tell John.
- S-02: the existing rulings (this file, lines 124/186; ADR-051(b)) already decide it — both spellings of a declared field
  resolve in templates, so the static check must admit `row['Name']` under `[name]`. My earlier "keep the refusal" lean is
  withdrawn (it contradicted the ruling). Code fix, round 10b after G3.
- field_mapper `strict: false` semantics: separate decision after R2 lands (not folded into R2).

## 2026-09-27 ~20:30 — R2 scope (John) + lane-owner decisions
- John (AskUserQuestion): the transform router lands in MERGE 1; ADR-016 source / ADR-017 sink / created-but-undeclared
  members become MERGE 1b (own unit + review + gate), right after merge 1 and before the six batch plugins.
- Lane owner (following B2 + one-authority): batch-seam parity ADOPTED (shared classifier; proven/divergent miss = Tier 1
  with tokens FAILED first; absent unproven = B2 route). Reason category = existing `missing_field`. Vote-under-proving
  detector = pinned proven map over examples + corpus. json_explode's array option becomes a declaration surface. llm
  image_inputs `required: false` is not a declared required input. Spec: DESIGN-R2 "REVISION 2".

## 2026-09-27 ~20:45 — John: sink-effect evidence cap → design pass + merge 1b; no tech debt until release
- John approved: architect + systems design pass on the sink-effect evidence cap (QUEUE-related.md ~20:30 section), landing
  with MERGE 1b. "remember we have a no techdebt policy until we release".
- Consequence (lane owner): X2's CHANGELOG "KNOWN LIMIT … > ~12.6k rows still stops the run" is TEMPORARY — merge 1b must fix
  the class (every row reaches a terminal outcome at any sink-write size) and delete that bullet. A documented limit is
  not an accepted outcome before release.

## 2026-09-27 ~21:35 — John: R1 governance close — YES, "as a governance option regardless"
- John: "on R1, I think we should offer that feature as a governance option regardless". Operator-decided close of a
  FAILED-but-resumable run is a feature in its own right, NOT tied to S1b.
- ADR-038 amendment required (lane owner's analysis, told to John): (1) ABANDONED gains a recorded cause —
  structural (today) vs operator decision (+ who, when, operator reason, secret-scrubbed); (2) a governance-closed run is
  PERMANENTLY non-resumable — resume refuses with its own cause before touching tokens (ADR-038: resume meeting ABANDONED
  is an audit-integrity crash); (3) a second, fenced writer that closes out an already-terminal FAILED run (status stays
  FAILED; decision recorded beside it), taking the same leadership fence resume uses; (4) reconcile every open sink effect
  (INSPECT) BEFORE failing it — never record failure for an external write that landed.
- Slot: its own governance unit (design + architect/systems review) in MERGE 1b, independent of S1b; S1b must not depend on
  it. Open for the design: CLI-only vs also web (role/authorisation), and the reason text rules.
- ~21:45 John (web authorisation for governance close): "if you have authority to run it, we can infer you have authority
  to close it (with the audit log keeping everyone honest about business rules to the contrary)". Design consequences:
  the web close reuses the SAME authorisation predicate that admits executing that run (one authority — never a parallel
  copy that can drift); CLI = the operator holding the Landscape DB, as today; every close records actor identity, time
  and reason in Tier-1 audit so business rules can be enforced after the fact. No separate governance role.

## 2026-09-27 ~22:00 — lane owner: S1b R2 (remote object size overflow) — DIVERT, visibly
- Both S1b reviewers (specialist4-S1b-{systems,architect}.md) independently recommend diverting now; multipart/rolling
  objects change artifact identity and are a separate feature. Decided as lane owner (experts converge; John told, may
  override): when a remote object sink's cumulative object would exceed its configured max_object_bytes/max_blob_bytes,
  the members that do not fit are diverted through on_write_failure with a DISTINCT reason category, counted separately
  in run accounting (the cumulative cap is otherwise a silent output cliff). Deterministic prefix-fit (accept the members
  that fit, divert the rest) if cheap; else whole-effect diversion. Not a "known limit": the operator configures the
  bound and every row reaches a terminal, audit-visible outcome.
- Both reviews ENDORSE-WITH-CONDITIONS; design revision 2 requested from the S1b design author (implement on the LANE
  branch, not release; X2 committed 5c0c9cada/f11b940e1).
- ~22:05 John: "R2 endorsed" — the divert-visibly decision above is now operator-confirmed.
- ~22:30 lane owner: the whole-tree "Tier-1 record size / statement binds / loop bound independent of N" structural gate is
  a MERGE 1b unit (after S1b), not a follow-up ticket — four recurrences of "cap grows with rows → run dies" in one lane.
- NOTE (measured 21:31 by `date`): the section labels '~21:35'…'~22:30' above were my ESTIMATES and run ahead of the wall clock; their ORDER is right, the times are not. Use `date` for future labels.

## 2026-09-27 21:5x (measured) — slotting of the five S1b adjacent items (VERIFY-S1b-adjacent.md), lane owner
No tech debt before release (John): every DEFECT gets a unit before release; none is ticketed away.
- Q-RESUME (item 3) DEFECT, HIGHEST: a source-quarantined token pending at a crash makes the run permanently unresumable
  and traps valid pending tokens (quarantine_router.py:239 creates no durable work item; recovery.py:699 pushes the raw
  quarantined row through the source schema). Own unit, merge 1b, FIRST; design + architect/systems review now.
- LINEAGE (item 1) DEFECT: sink_effect_identity.py:85-86 serialises the lineage DAG as an expanded tree (exponential in
  fork stages; 2-way×12 exit 4, validate passes, nothing in Landscape). Inside S1b (same identity code, same epoch step).
- MEMORY (item 5) DEFECT (latent): one end-of-source sink write costs ~16–25 KB/token unbounded; masked by the 64 KiB cap.
  BINDING ORDER: S1b's one-effect-per-flush + cap deletion MUST NOT land without the bounded-memory fix (+ a peak-memory
  regression test) in the same merge — otherwise a loud failure at ~12.6k rows becomes a silent OOM kill with no audit.
- DRAIN CAP (item 4, L2) DEFECT: MAX_WORK_QUEUE_ITERATIONS → no-progress detector; own unit after X2; S1b's CHANGELOG
  KNOWN-LIMIT deletion waits for it.
- CSV >1024 columns (item 2) DESIGN-LIMIT (loud, recorded, ADR-038-abandoned, resume refuses cleanly; mode: fixed works):
  one small commit — document the cap; refuse at header read with a message naming `mode: fixed`.

## 2026-09-28 01:33 (measured) — QR (quarantine-resume false SUCCESS) into MERGE 1 (John) + lane-owner decisions
- John (AskUserQuestion): QR moves from merge 1b into MERGE 1, built in parallel in its own worktree; if it is the last thing
  gating merge 1, ask John again rather than hold.
- Both reviews ENDORSE-WITH-CONDITIONS (specialist5-QR-{architect,systems}.md). Binding: architect C1 (5th
  pending_sink_bundle_clause arm for source quarantine), C2 (do NOT flip routes_to_sink; explicit source-quarantine branch
  in accumulate_row_outcomes; discard-pipeline control), H1 (delete the unreachable source-row replay path and every symbol
  listed; amend ADR-025 decision 4 + ADR-003 NullSource clause), H2 (one general pre-sink-write resume check, after
  requeue_undecided_failed_work, recorded in Landscape, value-free), H3 (completion check + replay deletion in the SAME
  unit, deletion first; rebuild the resume tests from real crashes), H4 (probe terminal-coalesce before claiming the class
  / deleting the flag), Q(a) split answer, M1, M3 (QR takes its own epoch step; never two branches claiming the same
  number — the second to merge rebases the constant), M4; systems C1–C4 (quarantine-storm scale case at the drain threshold
  and in the drain-cap unit's acceptance; QR/S1b two-way cross-verification; name the real 16 test ids).
- Lane owner, M2: when the general resume check fires (only reachable by real corruption after the epoch step), ACCEPT the
  trap — the run stays resumable-but-refusing, the refusal is recorded value-free in Landscape; the operator exit is the
  governance close (John's R1, merge 1b). Not a fourth non-resumability case (that sweep would abandon valid tokens).

## Lane-owner decision 2026-09-28 02:01 AEST — G3 residual slotting (no tech debt)
G3 PASSED r3 with 4 low findings; with G3 r2's F2 they are all slotted into round 10b item g (STATUS §3.2.g), none
deferred: r2 F2 is a lane regression (multi-query attribute-resolving filters over an undeclared field: release delivers,
lane admits then routes every row) → refuse at config; r3 F4 (the reverse over-refusal, same outer-binding check) → admit;
r3 F3 element calls on field values → refuse; r3 F1 missing mutant-killing cases; r3 F2 `row | items` teaching → `| list`.

## Lane-owner decision 2026-09-28 03:20 AEST — nested/select coalesce provable absence → merge 1b R2b
R2 remedy text PASSED r1 (af729b413). Its one minor finding (review-R2-remedy-text-r1.md F1) is structural, not wording:
after `merge: nested` (or `select` of a branch that cannot carry the field) a declared field is provably absent, yet the
build proves nothing and every row routes (pre-existing; release routes the same rows). The true fix is a build refusal
from what the coalesce provably emits — the same closed-upstream refusal R2 already uses — plus the remedy clause. It
belongs with R2b (created-field guarantees, merge 1b) because both change what a node publishes as its guarantee; doing
the text now and the refusal later would churn the pinned text twice. Slotted STATUS §4 item 1c. Not deferred past release.

## Lane-owner decision 2026-09-28 03:40 AEST — C3 handoffs + round10e minors slotted (no tech debt)
Measured by round10e §G (impl-C1C3-residuals.md; release 493afe210 vs lane): all pre-existing.
- MERGE 1 (round10g on fix/5887-codex-final): H1 unsafe integer (>2**53-1) in a source row → QUARANTINE at the shared
  source-admission boundary for every source (Tier-3 data; json_source's NaN rule is the precedent; today exit 4 + a token
  with no outcome). database_sink diversion reason carries driver text (PG DETAIL = row key/value) → value-free failure
  kind, demonstrated on PG first (C3 class). H4 hashing repr → type-only. H3 web/composer/audit.py false docstring →
  corrected (minimal; web/ is John's area). field_spelling ~420 mutant-killing test.
- MERGE 1b, new §4 item 1d: sink row-data faults that abort the run (dataverse duplicate alternate key / invalid lookup /
  blank or non-str key; chroma duplicate id / empty id) → divert via on_write_failure with value-free reasons, every token
  terminal. Designed with R2b's sink half (ADR-017), sequenced AFTER S1b (S1b rewrites sink-effect member material).
- H2 (gate WITHOUT on_error aborts on a data-dependent evaluation error): NOT a defect — GateSettings.on_error documents
  "Omission preserves fail-fast execution" (core/config.py ~779), an explicit operator choice; transforms require on_error.
  Resume then re-hits the same row; GOV governance close (1b item 4) is the operator exit. Told John in one line; he may
  overturn (e.g. require on_error for a gate whose condition can fail on row data, which T1 typing could prove).

## Lane-owner decision 2026-09-28 06:33 AEST — field_mapper `strict` (Astra M-04; STATUS 10b item c)
Mapping sources are ALWAYS declared inputs (elspeth-d4ae04b374: a silently vanished column was the bug; declared_input_fields
already derives them regardless of strict). With R2 an unproven miss routes `missing_field`, so strict has no engine-
observable effect — its option text already admits it only "controls direct process() calls". An option that does nothing
in a pipeline while its class docstring (field_mapper.py:~310) claims it "tolerates missing sources" is debt. DECISION:
round 10b MEASURES that strict:true and strict:false behave identically under `elspeth run` (observed and fixed upstreams,
present and missing source, dotted and plain) — if identical, REMOVE the option (a config carrying `strict` is refused by
extra=forbid with a message saying mapping sources are always required and a missing one routes as missing_field;
CHANGELOG "Newly refused configurations" line; planner teaching/skills/MCP schema updated; examples/tests migrated);
if NOT identical, stop and report the difference (then it is a real semantic and I decide again). Told John one line.

## 2026-09-28 07:49 AEST — John CONFIRMS field_mapper `strict` removal
John: "confirm the removal of strict provided its functionally equivalent". The lane-owner decision above stands as a
John ruling: remove `strict` iff round 10b B1 measures strict:true ≡ strict:false under `elspeth run`; any measured
difference → stop and report (no removal).

## Lane-owner decision 2026-09-28 08:50 AEST — MERGE-1 SCOPE FROZEN (John: "tired of how long this is taking")
Merge 1 = what is running now (round10b B2+B3, round10d QR, round10g handoffs) + cherry-picks + guidance unit +
behaviour-diff rel-vs-HEAD triage + Codex re-review + rebase + gate + merge. From now: a NEW finding goes into merge 1
ONLY if it is a lane regression (release behaves correctly, lane does not) or blocks the gate. Every pre-existing finding
goes to merge 1b (still before release — no tech debt). Minor review findings on a passed unit do not spawn a new unit;
they are folded into the next unit already touching that area, or into 1b. Future workflow scripts: at most 2 red-team rounds.

## 2026-09-28 11:48 AEST — John: land the ready fixes first; stretch work must not put them at risk
John: "the additional stretch work we swept up is high value but I don't want to put the original fixes at risk when they're
ready to go". land/5887-merge1 (lane @2e857a14f + codex-final + example-rank + R2 + release + gate-red fixes) is FROZEN in
scope: nothing from round10b B2/B3, round10d QR, round10g handoffs or merge 1b is added to it. Only a release-tip merge and
fixes for reds that merge causes. Stretch work resumes AFTER the landing merge, re-based on the landed release, as its own
separately gated merge(s).

## 2026-09-28 12:13 AEST — John: STANDING approval to skip the trust-tier ratchet hook on release-into-landing merges
Applies ONLY to a merge commit of release/0.8.1 into the landing branch, and ONLY when every finding the ratchet adds is
measured to sit in a file byte-identical to the release tip being merged (git diff --quiet <release-sha> per file, with a
lane-changed control file reporting DIFFERS). Every other hook still runs. The commit message records the measurement.
Not for any other commit.
John (12:14): "we reconcile the signs on the main branch before we merge to main" — trust-tier signing/reconciliation
happens on release before release→main; lane work must only avoid ADDING findings (measured per commit).

## 2026-09-28 12:48 AEST — John: verification pagination = OPTION 1 (composite index)
Add an index on call_verifications (current_run_id, recorded_at, current_call_id); page on run with a
tuple_(...) > (...) keyset condition that seeks the index; keep corruption detection as one indexed scan (not per page).
Schema change → Landscape epoch step. Slotted WAVE 4, folded into S1b's epoch step (one bump, not two). Evidence and
benchmarks: impl-verif-scoped-read.md, logs/land/verif/B-bench-*.log. Not part of the current landing.

## 2026-09-28 14:48 AEST — John: push release/0.8.1 AFTER wave 1 lands.

## 2026-09-28 16:23 AEST — John: two answers
1. The standing trust-tier-ratchet skip EXTENDS to release merges into ANY 5887 lane/side branch, same measured condition
   (every added finding in files byte-identical to the release sha being merged; lane-changed control reports DIFFERS).
2. The 35 release fencing reds from 964378989 are fixed by John's OTHER session; wave 1 gates after that fix lands.

## 2026-09-28 17:19 AEST — lane-owner: differences_json plain json.loads is NOT a defect
verification_reads.py ~116 and execution/calls.py ~879 decode differences_json only to validate its shape (object; empty
when is_match); CallVerification carries the stored STRING (verification_reads.py ~161), so no decoded value is ever read
back — the "stored canonical JSON reads back to the hashed value" guarantee is unaffected. No change.

## 2026-09-28 17:57 AEST — John: EXCLUSIVE custody of the repo and box; no other agents running
Consequences: no sibling-suite contention to wait for (still one broad suite at a time — mine); uncommitted files in the
main checkout (AGENTS.md staged, 5 docs files) are John's, not an active session's — still never touched without asking.

## 2026-09-28 18:34 AEST — John: full custody until 3 packages land (target: within 12 hours)
Packages: (1) wave 1 (land/5887-w1), (2) QR (fix/5887-qr), (3) B2 (round10h, fix/5887-rebased). Order: W1 → QR → B2
(QR moved ahead of wave 2: reviewed and idle). Each: merge release tip, reached tests, full gate, ff release, push.

## 2026-09-28 18:35 AEST — John: up to 16 concurrent test workers across the box (THIS custody period only)
Total across me + all my agents ≤ 16 pytest workers. Granted ONLY because John guarantees I am the sole agent right now —
NOT a universal grant; it ends with this custody period. Allocation: a full gate may take -n 12 while prep/fix agents
share the remaining 4 (one broad suite at a time still; no testcontainer beside another suite).

## 2026-09-28 22:00 AEST — John: proof-catalog v3 amendment AFTER QR lands (before release)
QR deleted read models four v3 claims were proven against: RM-09 (active_row_ids), RM-10 (blocked-barrier token read model;
exclusion now structural via journal restore), PB-01 clause "quarantine exclusions create no scheduler work" (now false:
durable PENDING_SINK item), RC-02 boundary_composition (only owner tested the deleted payload restore). Follow-up unit:
retire RM-09/RM-10 as superseded by QR's durable-work model, rewrite PB-01's clause, assign or retire RC-02 boundary_
composition; re-pin v2/v3 lockstep hashes with their tool. Evidence: impl-QR-landing-prep.md (selector re-bind table).

## 2026-09-29 02:13 AEST — John: STOP spending on package 3 while package 2 is unlanded
"if you keep burning all your tokens on package 3 while you wait for package 2 to land package two is not going to land."
ONE package at a time, to landed. B2 (package 3) is PARKED: no agents, no reviews, until QR is on release. When resumed,
B2 lands WITHOUT S-02 (John chose "Land B2 without S-02"; S-02 goes into the dual-name design pass).
