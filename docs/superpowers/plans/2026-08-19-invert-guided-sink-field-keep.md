# Invert Guided Sink Field Review — Post-Validation "Keep" Stage

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps
> use checkbox (`- [ ]`) syntax for tracking.
>
> **Plan fidelity note:** this plan is DESIGN-LOCKED, not fence-locked. Tasks marked
> `[FENCE-AT-EXECUTION]` name their reference implementation sites and acceptance tests
> exactly, but their code fences must be authored against a fresh read of those sites and
> dry-run before dispatch (house rule: plans' code fences are dry-run before hand-off).
> Design decisions D1–D7 below are locked unless the operator vetoes them.

**Goal:** Move sink field selection from step-2 pre-commitment (candidates = source
observed columns only, custom names as predictions) to a post-validation review stage
where the validated candidate's *actual* arriving-field inventory is enumerated per sink
and the user chooses what to keep, with a three-way mode: keep exactly these / keep these
plus anything new / keep whatever arrives.

**Architecture:** Step 2 keeps sink plugin selection + schema form and drops the
field-review multi-select. After a planner candidate passes validation (and before any
`TerminalState(completed)` is constructed), a new `STEP_5_KEEP` stage emits one
`field_keep` turn per sink in `output_order`, built from guarantee propagation
(`walk_definite_emitted_fields`, core/dag/guarantees.py) plus the extras-firewall /
ADR-007 abstention facts. The response amends `SinkOutputResolved.required_fields`
(and only that — reviewed authority, materialized through the existing
`guided_reviewed_sink_options` seam), the candidate is rebound and revalidated
server-side. `mode="exact"` is the one path that changes graph semantics (fixed schemas
kill rows, they don't project — guarantees.py:421-427), so it routes through the
existing guided revision-proposal machinery as a structured revision request; the
planner authors the projection. The server never authors structure (composer
invariant 1); the tutorial runs the same flow (ADR-031).

**Tech Stack:** Python 3.12 backend (frozen dataclasses + `freeze_fields`), React/TS
frontend with hand-mirrored wire types (`types/guided.ts`), pytest + vitest + Playwright
staging e2e, parity name-presence gate (`scripts/cicd/parity_harness.py`).

**Spec:** This plan doubles as the spec; the design rationale is the 2026-08-19
conversation record (sink schema modes: `fixed`=exact, `flexible`=at-least-these,
`observed`=infer — contracts/schema.py:458-466; fixed-mode firewall semantics —
guarantees.py:421-427; step-2 information poverty — stage_transitions.py:654
`_candidate_fields`). Operator (John) directed the inversion 2026-08-19.

## Global Constraints

- **Before writing any code, read `docs/agents/recent-code-hints.md`** — whole-tree AST
  gates pin dynamic-attribute sites, masquerade sites (tests included), and wire-shape
  templates; a scoped green run proves nothing about them.
- Composer invariants are absolute: **no server-authored pipeline structure**
  (`mode="exact"` MUST route through the planner; extending
  `tests/unit/web/composer/guided/test_no_chain_authoring_path.py` is mandatory), and
  **no tutorial-special paths** (ADR-031 — the tutorial re-record is a script update,
  never a branch).
- Work on `release/0.7.2` in the shared checkout; stage by pathspec only; commit only
  your own hunks. `git stash` is blocked.
- Every new dataclass with container fields calls `freeze_fields` in `__post_init__`
  (the FG3 lesson, elspeth-8014ff5bbb) — run
  `ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing
  .venv/bin/elspeth-lints check --rules immutability.freeze_guards --root src/elspeth`
  after each backend task (the pre-commit hook does NOT fire on src/ edits).
- Wire-shape changes mirror into `src/elspeth/web/frontend/src/types/guided.ts` in the
  SAME task (parity harness is name-presence over the web tree; unmirrored ratchet is
  at ceiling 10/10 — do not add an 11th).
- Full `pytest tests/ -n 12` before declaring done; trust-tier corpus compare (not
  zero); wardline gate of record with `--fail-on-inert` (6-ERROR baseline by
  fingerprint, not count).
- The keep-stage response (`chosen` field names, mode) is Tier-3 user input: validate
  against the server-enumerated inventory (subset check), never trust it into options
  unvalidated. New boundaries get `@trust_boundary` metadata where the pack expects it.
- Skills text edits require `sudo -n systemctl restart elspeth-web` to go live
  (lru_cached); restart is pre-authorized.

## Design Decisions (locked; operator may veto)

- **D1 — Step 2 sheds field review entirely.** Sink plugin select + schema form remain;
  the `MULTI_SELECT_WITH_CUSTOM` field-review turn, `_candidate_fields`, custom-field
  overlap validation, and the passthrough escape are deleted (pre-release: no tech
  debt). The raw `schema` JSON knob in the schema form stays (YAML-parity surface).
- **D2 — Distinct stage, not an overload of proposal review.** New
  `GuidedStep.STEP_5_KEEP = "step_5_keep"` between candidate validation and
  terminal-completed. Zero provider calls on the base path; a user-interaction turn is
  not a provider call, and folding keep controls into the accept action would tangle
  the exact-mode revision loop.
- **D3 — New turn type `field_keep`,** not a reuse of `MULTI_SELECT_WITH_CUSTOM`
  (custom free-typed names are meaningless post-validation; the payload carries
  epistemics multi-select cannot).
- **D4 — Keep-mode vocabulary is `"exact" | "plus_new" | "all"`** (a *keep* decision,
  not a schema mode). Materialization: `all` → no required_fields, schema untouched;
  `plus_new` → `required_fields = chosen` (presence contract; extras still flow —
  today's chosen-path semantics, same merge seam); `exact` → structured revision
  request to the planner (Task 6). If the user authored an explicit fixed/flexible
  schema at step 2, the keep list is constrained to its declared fields
  (`reviewed_schema_declared_field_names` precedence — the 398f150859 guard moves
  here, it does not die).
- **D5 — Inventory epistemics are first-class wire facts.** Payload carries
  `guaranteed` fields (from propagation) and `open_remainder: bool` (true iff any
  contributing schema `allows_extra_fields` / abstains under ADR-007). An empty
  remainder is "no gap provable", never "coverage proven" — the UI must render
  "+ anything else your source carries" when `open_remainder` is true.
- **D6 — Pre-planning field machinery is deleted, not preserved.**
  `guided_unproducible_output_fields`, its service.py planner-context enrichment, and
  the step-3 no-transform consumer go (with nothing declared pre-planning, the gap is
  always empty). Planner briefs are updated instead (Task 8): the planner is TOLD the
  field contract is end-reviewed, so it plans transforms from intent prose alone.
- **D7 — Chat and wizard paths converge on the keep stage.** `resolve_sink` (solver
  tool, chat_solver.py:2660-2960) drops `required_fields` and `schema_mode` from its
  output schema; `SinkOutputResolved.schema_mode` is always derived from options via
  `_sink_schema_mode` (stage_transitions.py:667). The revision projection serializer
  (chat_solver.py:1396-1438) reflects the new shape.

## File Structure

| Area | Files |
|---|---|
| Protocol / wire | `src/elspeth/web/composer/guided/protocol.py` (TurnType, GuidedStep, FieldKeepPayload, allowlists :671/:715, validator :2291), `frontend/src/types/guided.ts` |
| Inventory | `src/elspeth/web/composer/guided/planning.py` (new `sink_arriving_field_inventory`, near guarantee helpers; rebind at :2744; merge seam :1244) |
| Transitions | `src/elspeth/web/composer/guided/stage_transitions.py` (new keep transition; deletions :654, :1240-1320), `state_machine.py`, `intent_management.py` |
| Routes hook | `src/elspeth/web/sessions/routes/composer/guided.py` (terminal sites :1177 restore, :2532, :3562, :4601), `guided_chat_atomic.py`, `_guided_step_chat.py`, `guided_replay.py` |
| Solver | `src/elspeth/web/composer/guided/chat_solver.py` (:1396-1438, :2660-2960) |
| Deletions | stage_transitions field review + `_candidate_fields`; emitters step-2 multi-select + `escape_label`; `guided_unproducible_output_fields` + `service.py` enrichment; `ControlSignal.PASSTHROUGH` (verify-then-delete) |
| Frontend | `FieldKeepTurn.tsx` (new; reference: `MultiSelectWithCustomTurn.tsx`), `GuidedTurn.tsx` dispatch, `api/guidedDecoder.ts`, `test/guided-fixtures.ts` |
| Briefs / docs | `skills/pipeline_composer.md`, `skills/pipeline_capabilities.md`, new ADR, `docs/agents/recent-code-hints.md` entry |
| e2e / canary | `tests/e2e/composer-guided-live.staging.spec.ts`, `composer-guided-ab-live.staging.spec.ts`, `tutorial-probe.staging.spec.ts`, `composer-capability-parity.staging.spec.ts` |

**Name-collision trap:** most `resolve_sink` grep hits (engine/executors/sink.py,
runtime_factory.py, landscape tests) are the engine's *sink-effect* resolver —
unrelated; do not touch. Composer scope is chat_solver.py + guided tests only.

---

### Task 1: Protocol — turn type, step, payload, response record `[FENCE-AT-EXECUTION]`

**Files:** Modify `protocol.py` (+ TS mirror `types/guided.ts` same commit).
**Produces:** `TurnType.FIELD_KEEP = "field_keep"`; `GuidedStep.STEP_5_KEEP = "step_5_keep"`;
`FieldKeepPayload = {question: str, output_stable_id: str, output_name: str,
guaranteed_fields: list[str], open_remainder: bool, declared_fields: list[str] | None,
default_mode: "plus_new"}`; frozen `FieldKeepResponse(mode: Literal["exact","plus_new","all"],
chosen: tuple[str, ...])` with `freeze_fields(self, "chosen")` in `__post_init__`.
**Reference sites:** existing payload allowlists protocol.py:671/:715, validator table
:2291, `MultiSelectWithCustomPayload` as the shape template.

- [ ] Failing tests in `tests/unit/web/composer/guided/test_protocol.py`: payload
      validator accepts the exact key set and rejects missing/extra keys (mirror the
      existing per-turn-type validator tests); `FieldKeepResponse` rejects empty field
      names, duplicate chosen, unknown mode; `chosen` is deep-frozen (tuple identity).
- [ ] Implement; run `immutability.freeze_guards` scoped rule (rc 0).
- [ ] Mirror `FieldKeepPayload` in `types/guided.ts` with the wire-comment convention
      (`/** Wire: FieldKeepPayload (protocol.py:NNN). */`).
- [ ] Scoped pytest + vitest `types/guided.test.ts`; commit by pathspec.

### Task 2: Inventory enumeration `[FENCE-AT-EXECUTION]`

**Files:** Modify `planning.py`; test `tests/unit/web/composer/guided/test_planning.py`
(or the module's existing test home for guarantee helpers).
**Produces:** `sink_arriving_field_inventory(graph, sink_node_id) ->
tuple[tuple[str, ...], bool]` — (sorted guaranteed arriving fields, open_remainder).
Wraps `walk_definite_emitted_fields` over the sink's in-edges (skip DIVERT, mirroring
guarantees.py:415-445) ∪ upstream declared fields; `open_remainder` true iff any
contributing node's output schema `allows_extra_fields` or abstains (ADR-007 observed
sources). **Consumes:** the validated candidate's graph (the same structure candidate
validation walked).

- [ ] Failing tests: fixed-source pipeline → exact inventory, remainder false;
      observed source → observed columns present, remainder true; transform-added
      field appears; extras-firewall transform truncates upstream arrivals
      (guarantees.py:429 semantics); multi-edge sink unions.
- [ ] Implement; scoped pytest; commit.

### Task 3: Keep transition + rebind `[FENCE-AT-EXECUTION]`

**Files:** Modify `stage_transitions.py`, `state_machine.py`, `planning.py`.
**Produces:** `apply_field_keep_response(session, target_id, response) -> GuidedSession`:
requires `STEP_5_KEEP`; validates `chosen ⊆ inventory` (server-enumerated, stored on the
session or re-derived — Tier-3 boundary); enforces explicit-schema precedence (chosen ⊆
`reviewed_schema_declared_field_names(schema)` when declared fields exist — relocated
398f150859 guard, same error text discipline); amends
`reviewed_outputs[id] = replace(..., required_fields=selected)` for `plus_new`
(`all` → `()`); per-sink iteration over `output_order` (one pending keep at a time,
mirroring `_require_no_other_pending`); after the last sink, rebind via
`guided_reviewed_sink_options` + full candidate revalidation (server-side validation —
allowed); `mode="exact"` produces a `FieldKeepRevisionRequest` record (Task 6 consumes)
instead of amending.

- [ ] Failing tests (extend `test_stage_transitions.py`, 63 existing tests as pattern
      reference): happy-path plus_new; all; chosen outside inventory rejected; explicit
      fixed schema constrains; wrong step rejected; terminal/consumed-session guards
      (mirror `_require_component_review_stage` postures); frozen-at-rest assertions
      (nested lists surface as tuples — the FG3 test lesson).
- [ ] Implement; scoped pytest + freeze_guards rule; commit.

### Task 4: Routes choke point — no terminal without keep review `[FENCE-AT-EXECUTION]`

**Files:** Modify `routes/composer/guided.py`, `guided_chat_atomic.py`,
`_guided_step_chat.py`, `guided_replay.py`.
**Produces:** one helper (e.g. `require_field_keep_complete(session)`) that every
`TerminalState(completed)` construction site calls — guided.py:2532/:3562/:4601 and the
restore path :1177 (restore must tolerate historical pre-keep sessions per the
pre-release wipe posture: sessions.db may be wiped, auth.db never). `field_keep` turns
are emitted after candidate validation succeeds on every path (wizard, chat, atomic).
Replay handles `field_keep` turns.

- [ ] Failing integration test: a candidate that validates does NOT reach
      terminal-completed until keep responses for every sink in `output_order` are
      recorded — parameterized across the wizard and chat submission paths (this is
      the R2-F4 seam-divergence regression test; a path that skips the stage must
      fail loudly).
- [ ] Implement; scoped pytest; commit.

### Task 5: Deletions — step-2 field review and pre-planning machinery

**Files:** `stage_transitions.py` (:654 `_candidate_fields`, :1240-1320 field-review
transition), `emitters.py` (multi-select builder, `escape_label`), `protocol.py`
(`MULTI_SELECT_WITH_CUSTOM` + payload + validator + `ControlSignal.PASSTHROUGH` after
verifying no other transition accepts it), `planning.py`
(`guided_unproducible_output_fields` :1266), `service.py` (enrichment consumer),
`chat_solver.py` (resolve_sink drops `required_fields`/`schema_mode` from its output
schema :2660-2960; revision projection :1396-1438 reflects), frontend
(`MultiSelectWithCustomTurn.tsx` + test, decoder arm, fixtures, `guided.ts` payload),
and the 9 backend test files referencing the old turn.

- [ ] Enumerate first, delete second: `git grep -n "MULTI_SELECT_WITH_CUSTOM\|
      multi_select_with_custom\|FieldSelectionResponse\|escape_label\|
      ControlSignal.PASSTHROUGH\|guided_unproducible_output_fields"` — every hit is
      either deleted or justified in the commit message (verify absence with git grep,
      not memory).
- [ ] Update/delete the 9 referencing test files; solver tests
      (`test_chat_solver.py`, `test_sink_discovery_loop.py`,
      `test_step_chat_sink_driver.py`) update to the narrowed resolve_sink schema.
- [ ] Full guided-scope pytest + vitest; parity harness green (no 11th unmirrored
      site; removed names must be removed from BOTH trees); commit.

### Task 6: `mode="exact"` → planner revision routing `[FENCE-AT-EXECUTION]`

**Files:** `stage_transitions.py`, `planning.py` (revision binding), `chat_solver.py`.
**Produces:** the keep-stage exact request enters the existing guided revision-proposal
machinery as a structured revision intent ("output `<name>` keeps exactly [fields];
propose a revision achieving that") — planner round → rebind → revalidate → keep stage
re-presented **iff** any sink's inventory changed (loop bounded by the existing turn/
budget limits; no new budget mechanism). **The server authors no nodes** — the planner
decides projection-transform vs fixed sink schema.

- [ ] Failing tests: exact request produces a revision intent record (not an amended
      sink); extend `test_no_chain_authoring_path.py` — no `provider="server"` graph
      authoring on this path; inventory-unchanged short-circuit (no re-present);
      budget exhaustion surfaces the existing decline/exhaustion affordance.
- [ ] Implement; scoped pytest; commit.

### Task 7: Frontend `FieldKeepTurn` `[FENCE-AT-EXECUTION]`

**Files:** Create `frontend/src/components/chat/guided/FieldKeepTurn.tsx` (+ `.test.tsx`);
modify `GuidedTurn.tsx` dispatch, `api/guidedDecoder.ts`, `test/guided-fixtures.ts`.
**Reference implementation:** `MultiSelectWithCustomTurn.tsx` (checkbox list, submit
plumbing) — read it fully before authoring.
**Renders:** per-sink checkbox list of `guaranteed_fields` (default all checked), the
three-mode control (default `plus_new`), and — when `open_remainder` — the mandatory
"+ anything else your source carries" line (D5: epistemics are not optional chrome).
`exact` disabled with explanation when `declared_fields` constrains it away.

- [ ] Vitest cases mirroring the MultiSelect test file's structure: renders inventory;
      mode switch; open_remainder line present/absent both directions; submit shape
      matches `FieldKeepResponse` wire contract; declared-fields constraint.
- [ ] Implement; vitest + parity harness; commit.

### Task 8: Planner briefs + skills convergence

**Files:** `skills/pipeline_composer.md`, `skills/pipeline_capabilities.md`.
The planner is told: the sink field contract is reviewed at the end against the real
inventory — plan transforms from the user's intent; do not interrogate for sink field
lists; do not pre-declare `required_fields` (the option no longer exists in its tools).
Per the redundant-turns doctrine, this is brief *sufficiency* work: state what the
model will already have (the end review), so it doesn't spend turns asking.

- [ ] Update both skills; grep for stale "custom field"/step-2-field-review guidance.
- [ ] `sudo -n systemctl restart elspeth-web`; verify with a live status probe.
- [ ] Commit.

### Task 9: Canary re-record, ADR, hints entry

**Files:** the four staging e2e specs (guided-live, guided-ab-live, tutorial-probe,
capability-parity), new `docs/architecture/adr/03X-sink-field-keep-at-validation.md`,
`docs/agents/recent-code-hints.md`.

- [ ] Re-record the tutorial fixed script for the new flow (ADR-031: script update,
      never a code branch); update exact phase-string pins and the ≤2-provider-call
      pin (base keep path adds ZERO provider calls — a pin increase is a defect
      signal, not an expectation to relax).
- [ ] ADR records the inversion rationale (information-timeline argument, fixed-mode
      firewall constraint, invariant-1 routing for exact).
- [ ] recent-code-hints entry: the keep stage is the ONLY field-contract authority;
      any new pipeline builder must pass `guided_reviewed_sink_options` AND the keep
      choke point (extend the R2-F4 note).
- [ ] Full `pytest tests/ -n 12`; trust-tier corpus compare; wardline fingerprint
      compare; `npm run test:e2e:staging` (operator-gated if it needs the live edge).
- [ ] Commit.

### Post-merge measurement (operator-fired, not a task)

Battery A/B on the unix socket (`--base unix:///run/elspeth/uvicorn.sock`, never the
hostname; never overlap rounds): planner call count and discovery-turn count on the
1×1 and transform scenarios, before/after. Expected: no provider-call regression on
the base path; exact-mode adds exactly one revision round. Count tool calls, not
seconds.

---

## Risk Register

| # | Risk | Sev | Likelihood | Mitigation |
|---|---|---|---|---|
| R1 | **Invariant-1 breach**: exact-keep tempts a server-authored projection ("it's just a field list") | Critical | Medium | D4/Task 6 route through planner revision only; `test_no_chain_authoring_path.py` extended; zero-provider-call e2e gate already fails server-authored flows |
| R2 | **Seam divergence**: 4 terminal-construction sites (guided.py:1177/2532/3562/4601) — one path skips the keep stage | High | High if unmitigated | Task 4 single choke helper + parameterized cross-path integration test (the R2-F4 lesson applied in advance) |
| R3 | **Planner under-production surfaces late** → revision loops at the end | Medium | Medium | Bounded by existing turn/wall budgets; inventory-unchanged short-circuit; better UX than today's opaque `sink_contract_violation` repair burn |
| R4 | **Epistemic misrender**: observed-source remainder rendered as a complete list → user false confidence ("that's all my fields") | High | Medium | D5 `open_remainder` is a wire fact with tests in BOTH directions (frontend + payload builder); "no gap provable" ≠ "coverage proven" |
| R5 | **Planner-efficiency regression**: briefs lose sink-field steering → more discovery turns | Medium | Medium | Task 8 brief sufficiency (tell it the contract is end-reviewed); battery A/B post-merge; F4-style honest target (no regression on base path) |
| R6 | **Tutorial canary churn**: frozen script + phase pins + ≤2-call pin all move | Medium | Certain | Deliberate, scripted (Task 9); fragility IS the canary — breakage during re-record is signal, not noise |
| R7 | **Replay/restore of historical sessions** carrying the deleted turn type | Low | Certain | Pre-release posture: sessions.db wipe authorized (auth.db never); restore path tolerates pre-keep sessions (Task 4) |
| R8 | **Whole-tree gate escapes** (freeze_guards, AST pins, parity ratchet at 10/10 ceiling) | High | Medium | Hints doc first; scoped freeze rule per backend task; TS mirror in-task; full suite before merge |
| R9 | **Release-PR redaction-direction grade**: new/removed wire paths re-trip the same-count `weaken` heuristic | Low | High | Known operator item — `policy-weaken-justified` label + "Redaction policy weakening rationale" section (already owed for the epic) |
| R10 | **Deletion blast** (Task 5): `ControlSignal.PASSTHROUGH` or enrichment has an unenumerated consumer | Medium | Low | Enumerate-first discipline; verify absence with `git grep`; full suite catches cross-cutting consumers |

## Risk Comparison — inversion vs. the current design (2026-08-19)

The register above must be read against the status quo's risk profile, not against
zero. Several of the planned risks are relocations of risks the current design
already carries — and has already shipped as defects.

| Planned risk | Current-design counterpart | Verdict |
|---|---|---|
| R1 exact-keep invariant pressure | Identical pressure already breached once: the server-synthesized sketch (elspeth-b4a286d517, P1 open) authored pipelines with zero provider calls | Same pressure, one more surface; the mitigations (no-chain-authoring tests, zero-call e2e gate) exist because the current design already needed them |
| R2 terminal-site bypass | This class **already materialized**: R2-F4 — the sketch builder skipped `guided_reviewed_sink_options`, declared fields never reached `options.schema.required_fields`, the sink-contract check silently passed | Same class, relocated — pinned by a cross-path test in advance instead of discovered by incident, concentrated at one named choke instead of distributed across builders |
| R3 late discovery → end-stage revision loops | Current failure is worse and paid now: premature commitments become opaque `sink_contract_violation`s burning repair budget, compounding with first-defect-only prevalidation (F16) into deterministic `REPAIR_EXHAUSTED`. Open P1 elspeth-398f150859 is exactly this class | Strictly better: machine-facing budget exhaustion becomes a human-facing review choice with a revision affordance |
| R4 open-remainder misrender | Today's candidate list is **known-incomplete by construction** (omits every transform-produced field; the escape is the user free-typing predictions); the unknown-remainder asymmetry already exists, managed by advisory machinery | Better epistemics: the residual risk becomes a testable rendering property; the current one is structural and no rendering can fix it |
| R5 planner-efficiency regression | The current design pays a **measured** cost today (2-call vs 10-call spread; repair burn on unmeetable contracts; enrichment-channel complexity) | Certain measured cost traded for an uncertain, measurable one (battery A/B) — a favorable bet |
| R6–R10 transition costs | No counterpart — the status quo costs nothing here | Genuinely new, but one-time, bounded, gate-checked; none permanent |

**Structural difference.** The current profile's risks are *latent and distributed*:
the compensating machinery (custom-field prediction, overlap validation, the
unproducible-fields advisory, the review-time guard) is itself the defect surface,
with a live ticket run-rate to show for it — elspeth-398f150859 (P1),
elspeth-826765af90, elspeth-d293c5d139, plus the R2-F4 incident. Each is the same
root cause in different clothes: a contract authored before its facts exist needs
machinery to reconcile it with reality, and that machinery keeps breaking. The
inversion's profile is *concentrated and visible*: two permanent risks demanding
standing vigilance (the exact-mode invariant boundary; the terminal choke
discipline), both testable properties with their tests written into this plan, on a
smaller total surface (the plan deletes more machinery than it adds). It also
retires the standing UX/misconfiguration risk that motivated the design: "strict"
is currently unreachable except by hand-authoring JSON in the schema blob knob.

The one risk that genuinely worsens in count is invariant-1 exposure — one more
surface where "the LLM does the job" must be defended. It is the only risk here
whose failure mode is a policy breach rather than a bug; it carries permanent
vigilance (Task 6's tests, the e2e zero-call gate).

**Sequencing caveat.** The transition costs (R6–R10) land all at once — tutorial
re-record, gate churn, deletions — in a branch already carrying substantial
unpushed work. *Whether* is clearly favorable; *when* is an operator call.

## Blast Radius (measured 2026-08-19, release/0.7.2)

- **Backend guided core:** 13 files consume `GuidedStep` (170 refs) — the new enum
  member ripples all of them (parity-sweep discipline: new variant of an existing kind).
  Field-review surfaces concentrate in 3 files (27 refs); the multi-select turn in 5
  backend files; passthrough/escape plumbing in 5 backend + 5 frontend files.
- **Terminal choke:** 4 `TerminalState` construction sites in
  `routes/composer/guided.py` — ALL must route through the keep gate (R2).
- **Solver surface:** `chat_solver.py` (49 resolve_sink refs) + 3 dedicated test files
  (~52 refs). Engine `resolve_sink` hits are a name collision — out of scope.
- **Tests:** 9 backend test files reference the old turn directly;
  `test_stage_transitions.py` alone holds 63 tests; integration drivers
  (`test_sink_discovery_loop.py`, `test_step_chat_sink_driver.py`) rewrite; frontend:
  4 test files + fixtures.
- **e2e canary:** 4 staging specs re-recorded (guided-live, guided-ab-live,
  tutorial-probe, capability-parity) — phase pins + provider-call pins.
- **Docs/briefs:** 2 planner skills, 1 new ADR, recent-code-hints entry.
- **Estimate:** ~25–35 files touched; **net-negative guided-flow complexity** — the
  inversion deletes more machinery than it adds (field-review transition + candidates
  + custom validation + escape + unproducible advisory + solver schema fields, against
  one new stage's protocol/transition/emitter/frontend).

## Addendum (2026-08-19, post-recon — carried into the executable plan)

Deep recon of the routes/frontend layers amended four design points; the executable
plan (`2026-08-19-sink-field-keep-executable.md`) is authoritative where they differ:

1. **D2 amended — distinct turn type, not distinct step.** Keep turns are legal at
   `STEP_3_TRANSFORMS`, between the proposal accept and the wire turn. Only ONE site
   mints a fresh COMPLETED (guided.py:4601, inside the fence-laden atomic confirm) —
   the keep stage must sit BEFORE the wire review, not inside the confirm, so the
   user confirms the post-keep contracts and the confirm path stays untouched. R2's
   "four terminal sites" framing narrows accordingly: the enforcement moved to a
   `GuidedSession.__post_init__` invariant (COMPLETED ⇒ keep coverage), which every
   construction and load re-checks — stronger than any route choke.
2. **D4 wire encoding refined.** `TurnResponse` is unchanged: keep-mode rides
   `control_signal` (`None`=plus-new, `PASSTHROUGH`=keep-all repurposed,
   new `KEEP_EXACT`=exact). `GuidedRespondAction` (closed TS union) gains one arm.
3. **Keep decisions are a NEW session field (`keep_decisions`), not an amendment of
   `reviewed_outputs`** — the planning anchor hash (state_machine.py:1122-1128) pins
   reviewed facts to what the planner consumed; keep decisions are post-planning
   authority carried beside the anchor and materialized at candidate binding through
   `guided_reviewed_sink_options`. Schema cut 11 → 12.
4. **Replay needs zero edits** (payload-id-keyed, turn-type-agnostic), and the
   inventory is recorded by candidate validation itself rather than a new walker.
