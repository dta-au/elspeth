# Acceptance Run 2 Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all 18 verified findings from the 2026-07-31 Web Composer AWS acceptance run 2 (tracker label `acceptance-2026-07-31`) before soft launch.

**Architecture:** Six independent workstreams, each in its own worktree, merged to `release/0.7.2` with `--no-ff` after the full `pytest tests/` reconciliation run. Every finding was independently verified at HEAD `2b47e5658`; root causes and anchors below come from that verification. Two operator product decisions are binding: R2-F15 is fixed by making the product match the manual (retain-and-defer), and R2-F10 is fixed by auto-wiring deployment-mandatory guardrails with disclosure.

**Tech Stack:** Python 3.12 / FastAPI / pydantic (backend), React + TypeScript + zustand (frontend under `src/elspeth/web/frontend/`), Terraform (deploy/aws-ecs), pytest + vitest.

## Global Constraints

- Verified baseline: all anchors below are valid at `release/0.7.2` commit `2b47e5658`.
- Full `pytest tests/` (plain default selection) must pass at reconciliation before merge; scoped runs during development are fine.
- `elspeth-lints check` must pass before push.
- Frontend: `npm test` (vitest) for touched components; `npm run build` must succeed.
- Never shape code around trust-tier signature churn — top-level imports; rotation is absorbed at release.
- Worktrees symlink `.venv` from the main checkout; never run bare `uv pip install` inside one.
- If any fix changes persisted session-state shape, bump `SESSION_SCHEMA_EPOCH` (`src/elspeth/web/sessions/models.py:189`, currently 40) — sessions.db wipe on deploy is pre-authorized; `auth.db` is never wiped.
- Tracker: every task cites its `elspeth-*` issue; close it in the merge commit message or via `filigree` on completion.
- Guided-mode transitions are replay-verified: any route-side change to guided state must be mirrored in the service-side expected-transition construction (`settle_*` / back-edit verifier) or settlement verification fails.

---

## Workstream A — guided planner (worktree `wt-guided-planner`)

### Task 1: R2-F16 — make planner-authored source omission repairable, not terminal (elspeth-bcc6bdac99, P1)

**Root cause (verified):** the terminal candidate schema requires only `['nodes','edges','outputs']` (`src/elspeth/web/composer/tools/schema_contract.py:302`), so a re-plan "delta" candidate that omits sources is schema-legal. `candidate_finalizer(pipeline)` at `src/elspeth/web/composer/pipeline_planner.py:2123` sits OUTSIDE the try that converts candidate defects into repair feedback, so `bind_guided_reviewed_components` (`src/elspeth/web/composer/guided/planning.py:584-587`) raises `AuditIntegrityError("guided planner candidate does not identify reviewed sources")` → terminal 500 `integrity_error` instead of one budgeted repair turn.

**Files:**
- Modify: `src/elspeth/web/composer/pipeline_planner.py` (~2076-2135)
- Test: `tests/unit/web/composer/test_pipeline_planner.py`
- Test: `tests/unit/web/composer/guided/test_bind_reviewed_components.py`

**Interfaces:** Produces repair feedback (existing `_canonical_schema_feedback()` mechanism) when a terminal candidate has neither `sources` nor `source`.

- [ ] **Step 1: Write the failing test** — in `test_pipeline_planner.py`, drive `_plan_pipeline_inner` with a stub provider whose terminal candidate contains only `nodes/edges/outputs` (no `sources`, no `source`) and a `candidate_finalizer` that mimics the guided binder (raises `AuditIntegrityError` when sources are absent). Assert: the loop consumes one repair turn (the stub's second response includes sources and succeeds) and no `AuditIntegrityError` escapes.

- [ ] **Step 2: Run it** — `pytest tests/unit/web/composer/test_pipeline_planner.py -k sources_omitted -x`. Expected: FAIL with `AuditIntegrityError`.

- [ ] **Step 3: Implement** — in `_plan_pipeline_inner` at the terminal-payload guard (after `SetPipelineArgumentsModel` validation, ~line 2076-2089): if the candidate has neither `sources` nor `source`, set `terminal_feedback = _canonical_schema_feedback()` (same repairable path used for schema violations) and continue the loop instead of proceeding to finalize.

- [ ] **Step 4: Second line of defense** — move the `candidate_finalizer(pipeline)` call inside the existing rejection try so any remaining binder `AuditIntegrityError` whose message begins `"guided planner candidate"` is reclassified as repair feedback (preserve genuine integrity errors — those not caused by candidate shape — as terminal).

- [ ] **Step 5: Pin the binder classification** — in `test_bind_reviewed_components.py`, assert `bind_guided_reviewed_components` still raises `AuditIntegrityError` on a sources-free candidate (the binder's contract is unchanged; only the planner loop's handling changed).

- [ ] **Step 6: Run both test files; commit** — `git add -A src/elspeth/web/composer/pipeline_planner.py tests/unit/web/composer/ && git commit -m "fix(composer): treat planner source omission as repairable, not terminal (elspeth-bcc6bdac99)"`

### Task 2: R2-F17 — reject degenerate gates; fix control-coverage field credit (elspeth-5c0c09db31, P1)

**Root cause (verified):** the planner authored `condition: "True"` with both boolean routes forking to BOTH outputs; `CompositionState.validate()` accepts constant conditions (`state.py:1421`) and identical-destination boolean routes (`state.py:1445-1490`). Display is faithful — the stored condition IS `"True"`. Separately, the spurious `prompt_shield` warning: `_control_covers_fields` (`src/elspeth/web/plugin_policy/coverage.py:127-142`) returns False whenever the LLM's provable prompt fields are EMPTY (bare `{{ text }}` template extracts nothing) — checked BEFORE the `fields == "all"` shortcut, so a correctly-placed shield is never credited.

**Files:**
- Modify: `src/elspeth/web/composer/state.py` (gate validation, ~1421-1490)
- Modify: `src/elspeth/web/plugin_policy/coverage.py` (~127-142)
- Test: `tests/unit/web/composer/guided/test_gate_projection_fixture.py` (fixtures exist here)
- Test: `tests/unit/web/plugin_policy/test_coverage.py`

- [ ] **Step 1: Failing tests for degenerate gates** — two new validation cases: (a) gate with `condition: "True"` → expect repairable finding code `gate_condition_constant`; (b) gate with boolean routes whose resolved destination sets are identical → expect `gate_condition_dead`.

- [ ] **Step 2: Run** — expect both FAIL (validation currently green).

- [ ] **Step 3: Implement** — in `CompositionState.validate()` gate section: after `_validate_gate_expression`, add `gate_condition_constant` when the parsed expression is the literal `True`/`False` constant, and `gate_condition_dead` when the gate is boolean-routed and both routes resolve to the same destination set (reuse the fork-destination resolution at ~1312-1335). Both are repairable (planner loop bounces the candidate) and blocking at review.

- [ ] **Step 4: Failing test for coverage credit** — in `test_coverage.py`: LLM node with template `"Classify: {{ text }}"` (no provable `row.*` fields) + `prompt_shield` directly upstream with `fields: "all"` → currently fires `input_not_dominated`; assert clean after fix. Also assert a mismatched-fields shield still fires, with a message naming the protected and scanned field sets.

- [ ] **Step 5: Implement** — in `_control_covers_fields`: hoist the `fields == "all"` check above the empty-protected-fields bail-out; when prompt fields are unprovable, emit the distinct reason (`input_fields_unprovable`) whose message names both sets instead of the generic domination failure.

- [ ] **Step 6: Run both suites; commit** — `git commit -m "fix(composer): reject degenerate gate candidates; credit all-fields shields (elspeth-5c0c09db31)"`

### Task 3: R2-F4 — never present an unsatisfiable 0-transform sketch as complete (new issue, P3)

**Root cause (verified):** the server-synthesized rootless pass-through sketch (`src/elspeth/web/composer/service.py:2588-2684`) builds outputs at :2619-2626 WITHOUT `_sink_options_with_declared_required_fields` (`guided/planning.py:524-567`) — the seam the planner path applies at planning.py:633-637 — so declared output fields never reach the sink contract and the check skips.

**Files:**
- Modify: `src/elspeth/web/composer/service.py:2588-2684`
- Test: `tests/integration/web/composer/guided/test_respond.py`

- [ ] **Step 1: Failing test** — guided flow: source with observed columns `{order_id, region}`, output stage declares `required_fields: [order_id, region, client, amount_aud]`, finish outputs with no root intent. Assert the response is NOT a bare "complete" sketch: either a planner proposal (server routed to provider with the gap named) or a sketch carrying a blocker naming `client, amount_aud`.

- [ ] **Step 2: Run** — expect FAIL (sketch presented complete today).

- [ ] **Step 3: Implement** — (a) run sketch outputs through `_sink_options_with_declared_required_fields` for parity; (b) compute `missing = set(required_fields) - (observed_columns | declared_fields)` from `reviewed_sources` (planning.py:425,435) and `reviewed_output.required_fields`; if nonempty, skip the sketch and route to the provider planner with the gap in `reviewed_context`.

- [ ] **Step 4: Run; commit** — `git commit -m "fix(composer): block unsatisfiable zero-transform guided sketches (R2-F4)"`

---

## Workstream B — intent custody (worktree `wt-intent-custody`)

### Task 4: R2-F15 — retain-and-defer wrong-stage instructions (elspeth-a96b2f1b0a, P1; operator decision: product matches manual)

**Root cause (verified):** the retained-intent machinery exists and is fully wired (`retain_deferred_intent` → `create_deferred_stage_intent` → settlement verification → planner consumption). It fails at the entry gate: (i) the step solvers demand exactly ONE terminal tool call (`chat_solver.py:2169-2180` step-2, :1631-1643 step-1), so a message mixing sink values + future-stage intent discards BOTH; (ii) `_parse_deferred_intent_tool_arguments` shape errors (chat_solver.py:629-650) are terminal with NO repair turn, unlike the sink-config path (:2198-2201); (iii) `is_private_future_instruction` (`guided_chat_atomic.py:1285-1291`) applies the `[Future-stage instruction submitted privately.]` placeholder even on FAILED retains.

**Files:**
- Modify: `src/elspeth/web/composer/guided/chat_solver.py`
- Modify: `src/elspeth/web/sessions/routes/composer/guided_chat_atomic.py:1285-1291`
- Modify: `src/elspeth/web/sessions/_guided_step_chat.py` (rejection message path :237-240, :477, :679)
- Test: `tests/integration/web/composer/guided/test_wrong_stage_intent.py`
- Test: `tests/unit/web/composer/guided/test_chat_solver.py`

- [ ] **Step 1: Failing test — repair loop** — stub the solver model to return a malformed `retain_deferred_intent` first, then a valid one on receiving the validation error as a tool result. Assert the intent is retained (no `DeferredIntentActionShapeError` escapes) and one repair turn was consumed.

- [ ] **Step 2: Implement repair-before-reject** — on `DeferredIntentActionShapeError` from `_parse_deferred_intent_tool_arguments`, thread the error back as a tool-result message and retry within the existing bounded repair loop (mirror the config-invalid `resolve_sink` pattern at chat_solver.py:2198).

- [ ] **Step 3: Failing test — mixed message** — model returns the PAIR `{resolve_sink, retain_deferred_intent}`. Assert both apply: sink options staged AND deferred intent created.

- [ ] **Step 4: Implement pair acceptance** — extend the step-2 (and step-1 analog) terminal-call validation to permit that pair; add optional `deferred_action` to `Step2SinkResolvedOutcome`; apply both in the atomic route. The settlement command already carries `retained_deferred_intent_id` and `_verify_guided_deferred_intent_append` (service.py:970-993) verifies the append independently — no settlement schema change.

- [ ] **Step 5: Failing test — transcript custody** — after a retain that FAILS all repairs, assert the user `ChatTurn` contains the author's verbatim text (placeholder literal `[Future-stage instruction submitted privately.]` appears nowhere in `chat_history`).

- [ ] **Step 6: Implement** — drop the placeholder: record the author's verbatim text in the user ChatTurn for both success and failure (privacy is enforced at the correct boundary already — later-stage LLM prompts see only the rendered `durable_summary`; the verbatim row already exists in `chat_messages`).

- [ ] **Step 7: Last-resort retention** — if repair exhausts, degrade to a durable clarification intent instead of discarding: reuse `DeferredStageIntent` with an empty-constraint clarification status (schema thought needed → bump `SESSION_SCHEMA_EPOCH` to 41). The rejection message then says the intent was kept and asks for the missing structural constraint.

- [ ] **Step 8: Update the manual if wording drifts** — `docs/guides/user-manual.md:639-653` stays true after this fix; adjust only if the clarification-degradation wording needs a sentence.

- [ ] **Step 9: Run suite; commit** — `git commit -m "fix(composer): retain wrong-stage instructions through repair, pair-accept, and clarification fallback (elspeth-a96b2f1b0a)"`

### Task 5: R2-F6 — persist transform/wire-stage instructions in the transcript (new issue, P2)

**Root cause (verified):** `chat_history` is appended only by `/guided/chat` (step-1/2) and the planner-decline path; step-3/4 prose travels via `/guided/respond` as `revision_instruction` (`guided.py:2528-2547`), consumed verbatim (:3255-3268) but recorded only as `TurnRecord.summary="Guided pipeline proposal revision requested."` (:3117). Two-seam edit: guided transitions are replay-verified, so the service-side expected-transition construction (back-edit verifier, service.py:~11240) must mirror the route change.

**Files:**
- Modify: `src/elspeth/web/sessions/routes/composer/guided.py` (~3100-3130, `is_prose_revise` settlement branch)
- Modify: `src/elspeth/web/composer/service.py` (expected-transition construction for prose-revision settlement, ~11009 / ~11240)
- Test: `tests/integration/web/composer/guided/test_respond.py`, `test_respond_schema8_atomic.py`

- [ ] **Step 1: Failing test** — submit a prose revision at step 3; assert `guided_session.chat_history` gains a USER turn with the instruction verbatim (`step == step_3_transforms`) and an ASSISTANT turn summarizing the outcome, and that settlement/replay verification passes.

- [ ] **Step 2: Implement route side** — in the `is_prose_revise` settlement branch, append `ChatTurn(role=USER, content=revision_instruction, step=guided.step)` + assistant outcome turn; bump `chat_turn_seq` by 2 on the successor `GuidedSession`.

- [ ] **Step 3: Implement service side** — extend the expected-transition construction identically so `candidate_guided != expected_guided` comparison still holds.

- [ ] **Step 4: Run; commit** — `git commit -m "fix(composer): record transform/wire-stage instructions in the guided transcript (R2-F6)"`

---

## Workstream C — advisor & fidelity (worktree `wt-advisor`)

### Task 6: R2-F12 — stop the composer rebutting the advisor at the user (elspeth-bff8fe6864, P2; reclassify: new backend channel, not a regression of 874080b4b)

**Root cause (verified):** the END advisor gate injects FLAGGED findings as a synthetic USER-role message (`service.py:4014-4027`) with no output contract; the model's next no-tool reply — a rebuttal of an exchange the real user never saw — IS the genuine answer row (persisted via `_finalize_no_tool_response` service.py:2118, or verbatim-prefixed into blocked results at :5306). The `874080b4b` frontend filter (`turns.ts:169-170`) is working as designed; do not touch it.

**Files:**
- Modify: `src/elspeth/web/composer/service.py:4014-4027`
- Test: alongside the existing END-gate advisor tests under `tests/unit/web/composer/` (the `_evaluate_terminal_no_tool_advisor_gate` suite)

- [ ] **Step 1: Failing test** — assert the injected advisor message contains the output contract clause (exact text below).

- [ ] **Step 2: Implement** — extend the injected control message: `"Fix the findings via tool calls. The end user has NOT seen these findings; your final reply is shown to them and must state only the outcome — never reference, quote, or rebut the advisor."`

- [ ] **Step 3 (belt-and-braces, M): finalize-context elision** — after a FLAGGED→CLEAN repair cycle, run the finalize turn with the advisor argument messages elided from context so the answer cannot anchor on them. Gate behind a service-level constant so it can be reverted independently.

- [ ] **Step 4: Run; commit** — `git commit -m "fix(composer): advisor findings carry a user-facing output contract (elspeth-bff8fe6864)"`

### Task 7: R2-F13 — fence advisor findings only at the LLM boundary; neutralize embedded sentinels (new issue, P2, security)

**Root cause (verified):** `_advisor_signoff_blocked_validation` (`service.py:6372`) interpolates `_fence_advisor_findings` output (sentinels `_ADVISOR_FINDINGS_UNTRUSTED_BEGIN/END`, :6433-6434) into the user-facing wire detail; AND the fence helper does not neutralize embedded sentinel lines, so advisor output parroting `END_UNTRUSTED_ADVISOR_FINDINGS` prematurely closes the fence on the LLM re-injection path (:4023) — a fence escape.

**Files:**
- Modify: `src/elspeth/web/composer/service.py` (:6372, :6420-6440)
- Test: unit tests beside the advisor-gate tests; `src/elspeth/web/frontend/src/components/chat/MessageBubble.test.tsx` if fixtures carry the fence

- [ ] **Step 1: Failing tests** — (a) blocked-result detail contains no `BEGIN_UNTRUSTED_ADVISOR_FINDINGS`/`END_…` substring; (b) `_fence_advisor_findings` neutralizes embedded sentinel lines (e.g. prefix with `\\` or strip) so the wrapped payload cannot close its own fence.

- [ ] **Step 2: Implement** — human channel: interpolate truncated findings with plain framing `"Advisor findings (untrusted, quoted):"`; LLM channel: neutralize embedded BEGIN/END lines inside the payload before wrapping.

- [ ] **Step 3: Run; commit** — `git commit -m "fix(composer): advisor fence sentinels never reach users and cannot be escaped (R2-F13)"`

### Task 8: R2-F14 — advisor verdict parsing, retry, and honest surfacing (new issue, P2)

**Root cause (verified):** (1) `_parse_advisor_checkpoint_guidance` (service.py:5968, regex :5816) declares malformed on markdown bold, `Verdict:` prefixes, preambles, or any reply containing both CLEAN and FLAGGED; (2) `_run_advisor_checkpoint` retries only exceptions (attempts=2, :5387) — parse failure is terminal at :5400, and the END gate terminal-blocks on first `ok=False` (:3977) ignoring remaining budget; `AdvisorCheckpointVerdict.failure_class` is written but never read (malformed surfaces as "unavailable", :4001); (3) `_advisor_blocked_result` reuses the "Runtime preflight failed" header (`no_tool_policy.py:57`) when preflight actually passed.

**Files:**
- Modify: `src/elspeth/web/composer/service.py` (:5816, :5354-5445, :3977, :4001)
- Modify: `src/elspeth/web/composer/no_tool_policy.py` (:57 header selection)
- Test: `tests/unit/web/composer/test_advisor_checkpoint.py` (create if absent, beside the END-gate suite)

- [ ] **Step 1: Failing parser tests** — `**CLEAN**`, `Verdict: FLAGGED`, preamble-then-verdict, and FLAGGED-mentioning-CLEAN must all parse to their verdicts.

- [ ] **Step 2: Implement parsing** — strip markdown emphasis; scan the first N (5) lines for the first verdict marker; first-marker-wins (drop the both-words tripwire).

- [ ] **Step 3: Failing retry tests** — a parse-malformed response consumes a retry (re-prompt: `"Reply with exactly CLEAN or FLAGGED on line 1."`); first-pass `ok=False` does not terminal-block while checkpoint budget remains.

- [ ] **Step 4: Implement retry + budget honor; resolve `failure_class`** — either implement the malformed/unavailable differential where the verdict is consumed, or delete the phantom field; stop labeling malformed as "(unavailable)".

- [ ] **Step 5: Failing surfacing test** — when `validate_pipeline` is green and only sign-off fails, the system note reads `"Advisor sign-off could not be completed — built and validated; final sign-off pending. Retry to complete."` and does NOT use the runtime-preflight header.

- [ ] **Step 6: Implement surfacing; run; commit** — `git commit -m "fix(composer): tolerant advisor verdict parsing, budgeted retries, honest sign-off surfacing (R2-F14)"`

### Task 9: R2-F8a — give the END advisor the user's message and a constraint-fidelity rubric (new issue, P3)

**Root cause (verified):** `_build_checkpoint_arguments` (service.py:5235-5280) sends a fixed problem summary + state-derived schema excerpt; the user's message is never included, so "user said fixed, config says flexible" is undetectable by the one gate positioned to catch it. `message` is already in scope at the call sites (:3422-3450).

**Files:**
- Modify: `src/elspeth/web/composer/service.py:5235-5280`
- Test: `tests/unit/web/composer/test_advisor_checkpoint.py`

- [ ] **Step 1: Failing test** — END-phase checkpoint arguments include the originating user message (bounded truncation, reuse the `_ADVISOR_PROBLEM_SUMMARY_MAX_CHARS` pattern) and the rubric line.

- [ ] **Step 2: Implement** — thread `message` into `_build_checkpoint_arguments(phase="end")`; extend the rubric: `"Quote each explicit configuration constraint in the user's message (schema mode, field names/types, named plugins/values) and verify the pipeline satisfies it; FLAG any mismatch."` Confirm advisor redaction policy applies to the added text (patterns in `test_advisor_tool_prose.py`).

- [ ] **Step 3: Run; commit** — `git commit -m "feat(composer): END advisor verifies explicit user constraints (R2-F8a)"`

---

## Workstream D — freeform loop (worktree `wt-freeform-loop`)

### Task 10: R2-F9 — surface the already-salvaged partial state on timeout (new issue, P2)

**Root cause (verified):** the 422 handler already persists `partial_state` (`routes/_helpers.py:2123-2155`, provenance `convergence_persist`) and the next turn resumes from it. But both wall-clock raises in `_call_llm_before_deadline` (`service.py:5731-5738`, :5747-5754) hardcode `max_turns=0` and omit `failed_turn` — and `failed_turn != null` is exactly what the existing RecoveryPanel gates on (`frontend/src/types/recovery.ts:22-26`); the store never refreshes `compositionState` on the 422 (`sessionStore.ts` catch), and the copy at `sessionStore.ts:1643`/`2125` is static ("after multiple attempts").

**Files:**
- Modify: `src/elspeth/web/composer/service.py` (:5731-5754 + the caller that owns the turn counters)
- Modify: `src/elspeth/web/frontend/src/stores/sessionStore.ts` (:1643, :2125, 422 catch path)
- Test: `tests/unit/web/composer/test_compose_loop_carriers.py`; frontend `sessionStore.test.ts`, `useRecoveryPanel.test.ts`

- [ ] **Step 1: Failing backend test** — timeout raise carries real `turns_used` > 0 and a `failed_turn`; route test asserts the 422 body reflects both.

- [ ] **Step 2: Implement** — thread `composition_turns_used`/`discovery_turns_used` and `failed_turn` as parameters into `_call_llm_before_deadline`; populate both raise sites (mirror the budget raises at :3499-3506).

- [ ] **Step 3: Failing frontend tests** — on 422 with `reason === "convergence_wall_clock_timeout"`: copy reads `"ELSPETH ran out of time (Ns). Your partial pipeline was saved — continue from it or retry."` + renders body `recovery_text`; and when `partial_state` is present the store refreshes `compositionState` (graph shows the partial pipeline, not the stale one).

- [ ] **Step 4: Implement frontend; run; commit** — `git commit -m "fix(web): timeout 422 carries real turn context and surfaces the salvaged draft (R2-F9)"`

- [ ] **Step 5: Deployment guidance** — add to the AWS deploy docs (`deploy/aws-ecs/terraform/README.md` composer section): recommend `composer_timeout_seconds = 240` for multi-node authoring (validator ceiling 270; local dev runs 240). No code default change.

### Task 11: R2-F10 — auto-wire deployment-mandatory guardrails with disclosure (new issue, P2; operator decision)

**Root cause (verified):** required-control enforcement is prose-only at authoring time (`planner_authoring_aids.py:291-321`) and blocking only at execution (`web/execution/validation.py:234-235`); composer-side coverage findings are demoted to warnings (`plugin_policy/validation.py:434,459-475`) so the loop feels no repair pressure. Both insertion hooks exist and currently pass identity: freeform `candidate_finalizer` at `service.py:3021`, guided `_identity_pipeline_candidate` at `service.py:2473`.

**Files:**
- Create: `src/elspeth/web/composer/required_controls.py` (`wire_required_controls(candidate, snapshot, catalog)`)
- Modify: `src/elspeth/web/composer/service.py` (:3021, :2473 — pass the new finalizer), `src/elspeth/web/composer/interpretation_state.py` (register user_term `required_control_auto_wired`), `src/elspeth/web/composer/implicit_decisions.py` (category `policy_control`, provenance `policy_required`), `src/elspeth/web/composer/planner_authoring_aids.py` (:291-321 REQUIRED-mode branch)
- Test: Create `tests/unit/web/composer/test_required_control_autowire.py`; extend `tests/unit/web/composer/test_planner_authoring_aids.py`, `tests/integration/web/test_plugin_policy_end_to_end.py`

- [ ] **Step 1: Failing test** — textract→llm→output candidate under a snapshot with `ControlMode.REQUIRED` + selected shield/safety: `wire_required_controls` splices the selected implementations on the offending edges (deterministic ids `{capability}_auto_{n}`), stages a pending `pipeline_decision` interpretation with user_term `required_control_auto_wired` per inserted node, and records an implicit-decision entry (`category="policy_control"`, `provenance="policy_required"`). Idempotence: an already-covered graph is untouched. REQUIRED-but-unselected capability inserts nothing (leaves `required_control_unavailable`).

- [ ] **Step 2: Implement** — drive insertion from `control_coverage_findings` (`plugin_policy/coverage.py`, the single source of truth); options from the exemplar machinery (`planner_authoring_aids.py:607-667` `_selected_control_profile` + `_direct_control_options`); splice via the existing `splice_transform` mechanics.

- [ ] **Step 3: Wire the finalizers** — replace the identity lambdas at service.py:3021 and :2473; also run the pass pre-persist on the commit path (`pipeline_commit.py`) so incremental authoring gets the same guarantee.

- [ ] **Step 4: Aids cleanup** — retire the advisory-vs-wire fork for REQUIRED mode (keep advisory for RECOMMEND); drop the shield-recommendation row when auto-wired.

- [ ] **Step 5: End-to-end test** — auto-wired graph passes `web/execution/validation.py` required-control checks. Run; commit — `git commit -m "feat(composer): auto-wire required controls with acknowledgeable disclosure (R2-F10)"`

### Task 12: R2-F18 — make policy prohibitions discoverable in chat (new issue, P2)

**Root cause (verified):** `WEB_SURFACE_PROHIBITED` plugins are silently filtered from discovery (`web/catalog/policy_view.py:45-47`), while `skills/pipeline_capabilities.md:33-35` forbids claiming policy denial "only when live discovery proves it" — the model structurally cannot answer "tell me exactly why". The attempt path already explains fully (`_PLUGIN_UNAVAILABLE_EXPLANATIONS`, `tools/_common.py:1422-1425`).

**Files:**
- Modify: `src/elspeth/web/catalog/policy_view.py`, tool `_discovery_result` shaping, `src/elspeth/web/composer/skills/pipeline_capabilities.md`
- Test: Create `tests/unit/web/composer/test_discovery_prohibited_listing.py`

- [ ] **Step 1: Failing test** — `list_sources` result includes a `prohibited` section listing `aws_s3` with reason `plugin_not_allowed_on_web` and the policy explanation string; available section unchanged.

- [ ] **Step 2: Implement** — surface `WEB_SURFACE_PROHIBITED` entries (only that closed reason — static policy prose already shown on attempts) in `list_sources`/`list_transforms`/`list_sinks`.

- [ ] **Step 3: Skill update** — `pipeline_capabilities.md`: denial claims must cite the prohibited listing or an attempt failure; when the user names a prohibited plugin, state the policy reason explicitly. (Verify by re-running, per no-tests-for-skill-prompts doctrine.)

- [ ] **Step 4: Run; commit** — `git commit -m "fix(composer): prohibited plugins are discoverable with their policy reason (R2-F18)"`

---

## Workstream E — disclosure & telemetry (worktree `wt-disclosure`)

### Task 13: R2-F11 — stop the blob path leaking through implicit_decisions (elspeth-b5180a9630, P2, security)

**Root cause (verified):** two redaction passes that don't compose. `_state_response` (`src/elspeth/web/sessions/routes/_helpers.py:547-562`) redacts `sources` via `redact_source_storage_path` (keyed on the `blob_ref` marker) but the raw absolute path was already flattened verbatim into `composer_meta.implicit_decisions.entries[].value` by `merge_implicit_decisions_meta` (`_helpers.py:1817` → `composer/implicit_decisions.py:80-110`). The only implicit-decisions redaction (`redaction.py:4139-4155`) is fed exclusively by guided `private_path_projections`, empty on the freeform/`composer_selected` path. Leak is not 422-specific — every state response for a freeform blob-backed source carries it.

**Files:**
- Modify: `src/elspeth/web/composer/implicit_decisions.py:80-110` (`_source_entries`/`_flatten_options`)
- Test: Create `tests/unit/web/composer/test_implicit_decisions.py`; extend `tests/unit/web/sessions/test_routes.py`; `tests/unit/web/composer/test_redaction_completeness_property.py`

- [ ] **Step 1: Failing test** — a blob-backed source's implicit-decisions entry records `blob:<blob_ref>` for the storage-path carrier keys (`path`, `file`), never a `/var/lib/elspeth/blobs/...` path; and a full `json.dumps` of a convergence-422 body contains no blob storage root.

- [ ] **Step 2: Run** — expect FAIL (raw path present today).

- [ ] **Step 3: Implement (can't-regress design)** — in `_flatten_options`, when `source.options` carries `blob_ref`, record `blob:<blob_ref>` as the value for the storage-path carrier keys instead of the filesystem path. Raw paths then never enter `composer_meta`, so no outbound serializer can regress field-by-field. Guided sources (blob_ref stripped at commit) stay covered by the existing projection.

- [ ] **Step 4: Schema epoch** — persisted `composer_meta` rows already contain raw paths; bump `SESSION_SCHEMA_EPOCH` (coordinate with Task 4's bump — one increment for the whole sprint) so the deploy wipe clears them.

- [ ] **Step 5: Run; commit** — `git commit -m "fix(web): implicit_decisions records blob refs, never absolute paths (elspeth-b5180a9630)"`

### Task 14: R2-F7 — enumerate transform-level external effects in the run-consent dialog (new issue, P2)

**Root cause (verified):** the consent dialog's effect list is built client-side by `buildRunEgressSummary` (`src/elspeth/web/frontend/src/components/sidebar/ExecuteButton.tsx:39`) from a hardcoded Azure-only set `NETWORK_FETCH_PLUGINS = {web_scrape, azure_content_safety, azure_prompt_shield, azure_document_intelligence}` — the AWS externals (Textract, Bedrock content_safety/prompt_shield, all `Determinism.EXTERNAL_CALL`) are absent. The backend already ships the correct classification: `PluginSummary.audit_characteristics` includes `"external_call"` (`frontend/src/types/index.ts:367`), already loaded in `pluginCatalogStore`.

**Files:**
- Modify: `src/elspeth/web/frontend/src/components/sidebar/ExecuteButton.tsx:25-75`
- Test: `src/elspeth/web/frontend/src/components/sidebar/ExecuteButton.test.tsx` (`describe("buildRunEgressSummary")`, ~:723)

- [ ] **Step 1: Failing test** — a composition with an `aws_textract_document_analysis` node + a catalog stub carrying `external_call` produces a network-egress line; a deterministic transform does not.

- [ ] **Step 2: Implement** — make `buildRunEgressSummary` read the catalog transforms from `usePluginCatalogStore` and include any configured transform whose `audit_characteristics` contains `"external_call"`; keep the hardcoded set only as a catalog-not-yet-loaded fallback (or hide the line until loaded). Same source of truth as the audit-readiness row (parity test at `tests/unit/web/audit_readiness/test_boundary_predicate_parity.py`).

- [ ] **Step 3: Run; commit** — `git commit -m "fix(web): run-consent dialog enumerates transform-level external calls (R2-F7)"`

### Task 15: R2-F16b — request_id on every composer error envelope (new issue, P2)

**Root cause (verified):** guided routes consume terminal exceptions in-route and re-raise a closed `HTTPException` (`src/elspeth/web/sessions/routes/composer/guided.py:4504-4564` → `routes/guided_operations.py:137-151`), bypassing the app-level `_audit_integrity_error_handler` (`web/app.py:938-985`) that would inject request_id. The middleware stamps `X-Request-ID` but no guided log line carries it, so the header correlates to nothing. (The "docs promise `http_audit_integrity_error`" half is REFUTED — no doc names that token; do not rename the shipped event.)

**Files:**
- Modify: `src/elspeth/web/app.py` (register an app-level HTTPException handler)
- Modify: `src/elspeth/web/sessions/routes/composer/guided.py` (the four `guided.operation_terminal_failure` slog sites)
- Test: Create `tests/unit/web/test_composer_exception_handlers.py`; extend `tests/unit/web/sessions/routes/test_guided_operations.py`; update body-shape pins in `tests/integration/web/composer/guided/test_respond.py`, `test_respond_schema8_atomic.py`, `test_convert.py`, `test_step_chat.py`, `tests/unit/web/sessions/test_guided_start.py`, `test_fork.py`

- [ ] **Step 1: Failing tests** — (a) a dict-detail `HTTPException` without `request_id` gets `request.state.request_id` injected into the body; string-detail is untouched; (b) the guided terminal-failure envelope now includes `request_id`.

- [ ] **Step 2: Implement (one boundary)** — register an app-level `HTTPException` handler that, when `exc.detail` is a dict lacking `request_id`, injects `request.state.request_id` before default rendering. Covers guided 500s, freeform convergence 422s, and every future composer envelope — can't regress route-by-route.

- [ ] **Step 3: Add request_id to the four guided slog sites** — `request: Request` is already in scope (e.g. guided.py:2329); add `request_id=request.state.request_id` to the `guided.operation_terminal_failure` events.

- [ ] **Step 4: Operator vocab doc (optional)** — add an operator-runbook section listing the terminal-error event vocabulary (`http_audit_integrity_error`, `guided.operation_terminal_failure`) rather than renaming a shipped token.

- [ ] **Step 5: Run affected suites; commit** — `git commit -m "fix(web): request_id on all composer error responses (R2-F16b)"`

### Task 16: R2-F1 — make the aws_s3 policy layering legible (new issue, P2, docs + boot)

**Root cause (verified):** `source:aws_s3` is a deliberate, test-pinned defense-in-depth carve-out — a categorical credential-egress ban (`web_aws_s3_source_policy_error`, `provider_config_policy.py:43-74`) adjudicated before the allowlist (`plugin_policy/availability.py:106-118`). Keeping it in `default_plugin_allowlist` is guarded by `tests/unit/deployment/test_aws_ecs_terraform_package.py:398-424`. NOT dead config. The defect is legibility: `docs/reference/configuration.md:107-110` states unqualified that allowlisting makes a plugin available.

**Files:**
- Modify: `docs/reference/configuration.md:107-110`
- Modify: `deploy/aws-ecs/terraform/modules/scenario/locals.tf:88` (comment only)
- Modify: `src/elspeth/web/plugin_policy/compiler.py:80` (`compile_web_plugin_policy` — boot WARN)
- Test: `tests/unit/web/plugin_policy/test_compiler.py`

- [ ] **Step 1: Doc fix** — amend configuration.md:107-110 to note that categorical web-surface prohibitions override the allowlist (allowlist authorizes; it cannot un-ban a categorically-prohibited plugin, which surfaces as a *declined authorization*), cross-linking the aws_s3 sink section (configuration.md:621-636).

- [ ] **Step 2: Terraform comment** — add an inline comment at locals.tf:88 citing the ban and the guard test, so the entry is not mistaken for a mistake.

- [ ] **Step 3: Failing test + boot WARN** — `test_compiler.py`: compiling a policy whose allowlist contains a categorically `WEB_SURFACE_PROHIBITED` entry emits a one-line WARN naming it (predicate is name-static, needs no principal). Implement the WARN in `compile_web_plugin_policy`.

- [ ] **Step 4: Run; commit** — `git commit -m "docs+web: make categorically-prohibited allowlist entries legible at config and boot (R2-F1)"`

---

## Workstream F — forms, review UI & docs (worktree `wt-forms-ui`)

### Task 17: R2-F2 — surface conditionally-required collision_policy (new issue, P3)

**Root cause (verified):** the knob schema lowers only pydantic field-level requiredness (`src/elspeth/web/catalog/knob_schema.py:168` `info.is_required()`); `collision_policy` is `default=None` (`config_base.py:524`) so it lowers optional, while the composer rule (`tools/_common.py:2314`, enforced at `guided.py:2080-2082`) requires it explicitly when `mode='write'`. The form's `fieldsNeedingAttention` (`frontend/.../SchemaFormTurn.tsx:52-65`) counts only `field.required`. Help text says "Optional" (config_base.py:524); the correct guidance exists only as an LLM-only hint (`json_sink.py:572`).

**Files:**
- Modify: `src/elspeth/web/catalog/knob_schema.py` (extend `KnobField` with `required_when`; lower it in `_base_field`)
- Modify: `src/elspeth/plugins/infrastructure/config_base.py:524-533` (`composer_description` + `composer_required_when` json_schema_extra; correct the description)
- Modify: `src/elspeth/web/frontend/src/components/chat/guided/SchemaFormTurn.tsx:52-65` (honor `required_when`)
- Test: `tests/unit/web/catalog/test_knob_schema_composer_help.py`, `test_knob_schema_golden.py`; `SchemaFormTurn.test.tsx`

- [ ] **Step 1: Failing backend test** — `LocalFileSinkConfig.collision_policy` lowers with `required_when={"field":"mode","equals":"write"}` and a `composer_description` naming `auto_increment` as the safe default.

- [ ] **Step 2: Implement lowering** — extend `KnobField` with optional `required_when: VisibilityPredicate` (mirror the existing `visible_when` mechanism); add the json_schema_extra to the field; correct the YAML-facing description ("Required when mode='write': 'fail_if_exists' … 'auto_increment' …; 'append_or_create' only with mode='append'").

- [ ] **Step 3: Failing frontend test** — with `mode` defaulting to `write`, `collision_policy` counts in `fieldsNeedingAttention` and Continue is disabled until set; the required marker renders.

- [ ] **Step 4: Implement frontend** — treat a field as required when `required || (required_when && values[required_when.field] === required_when.equals)`; render the marker under the same predicate.

- [ ] **Step 5: Golden update; run; commit** — `git commit -m "fix(web): surface conditionally-required collision_policy in the sink form (R2-F2)"`

### Task 18: R2-F3 — render key transform options at the review surfaces (new issue, P3)

**Root cause (verified):** both review surfaces render only the behavior discriminant + contract fields, never options — proposal card hardcodes "Transforms each incoming item." (`ProposePipelineTurn.tsx:132-135`), wire review "Policy: transform each input row" (`WireStageTurn.tsx:184-185`), and `_build_wire_projection` (`composer/guided/emitters.py:660-736`) projects options for `llm` only (:727). `field_mapper`'s `mapping`/`select_only` are never projected. "(contract unchecked)" is cosmetic (`state.py:2681-2694` emits no contract row when nothing is required) — reword, don't re-engineer.

**Files:**
- Modify: `src/elspeth/web/composer/guided/emitters.py:660-736` (add allowlisted `node_options_summary`)
- Modify: `src/elspeth/web/frontend/src/components/chat/guided/WireStageTurn.tsx`, `ProposePipelineTurn.tsx`
- Test: `tests/unit/web/composer/guided/test_emitters.py`; `WireStageTurn.test.tsx`, `ProposePipelineTurn.test.tsx`

- [ ] **Step 1: Failing backend test** — `_build_wire_projection` includes a `node_options_summary` for a `field_mapper` node carrying `mapping` and `select_only`, and excludes non-allowlisted options (path/secret hygiene — same rationale as `_wire_schema`).

- [ ] **Step 2: Implement backend** — generalize the llm-only special case into a small per-plugin key-options projection; start with `field_mapper` (`mapping`, `select_only`) on a new optional `node_options_summary` field.

- [ ] **Step 3: Frontend render** — render the pairs in `WireStageTurn` and `ProposePipelineTurn` when present; reword the null-contract case to "(no required fields — contract not applicable)".

- [ ] **Step 4: Run; commit** — `git commit -m "fix(web): review cards render key transform options (R2-F3)"`

### Task 19: R2-F5 — clear the stale progressbar; add loading affordances (new issue, P4)

**Root cause (verified):** `ProgressView.tsx:174-204` renders `role="progressbar"` with hardcoded `aria-label="Pipeline execution in progress"` in every state; `InlineRunResults.tsx:204` keeps it mounted with no terminal check; `GraphMiniView.tsx:42-48` shows "No pipeline yet" whenever compositionState is empty, including during load.

**Files:**
- Modify: `src/elspeth/web/frontend/src/components/execution/ProgressView.tsx`, `InlineRunResults.tsx`, `src/elspeth/web/frontend/src/components/sidebar/GraphMiniView.tsx`
- Test: `ProgressView.test.tsx`, `InlineRunResults.test.tsx`, `GraphMiniView.test.tsx`

- [ ] **Step 1: Failing tests** — terminal status ⇒ no element with `role="progressbar"` named "in progress"; terminal ⇒ summary rendered without the in-progress bar; a loading state ⇒ GraphMiniView shows a neutral skeleton, not "No pipeline yet".

- [ ] **Step 2: Implement** — render the bar only when `!isTerminal` (or keep the strip but switch `aria-label` by state and drop `role="progressbar"` at terminal); prefix mid-run counters with an "in progress" affordance ("Source Rows (so far)"); give `GraphMiniView` an `isLoading` discriminator rendering "Loading pipeline…".

- [ ] **Step 3: Run; commit** — `git commit -m "fix(web): clear stale run progressbar and add loading affordances (R2-F5)"`

### Task 20: R2-F8b + Summarise-label bonus — doc + node-name label (new issues, P3)

**Root cause (verified):** configuration.md:1095-1099 says the Textract manifest "must declare the fields as a fixed schema"; the real gate (`core/dag/schema_validation.py:141-153` via `contracts/schema.py:694-712`) only requires the producer to GUARANTEE the fields — satisfied by `fixed` OR `flexible`-with-declared-required-fields, failing only for `observed`. Separately, `interpretationStepLabel.ts:20-24` maps `llm → "Summarise"` and discards the node's own name, so `extract_invoice` is titled "Summarise" on the acknowledgement card.

**Files:**
- Modify: `docs/reference/configuration.md:1095-1099`
- Modify: `src/elspeth/web/frontend/src/components/chat/interpretationStepLabel.ts:20-24,68-77`
- Test: `src/elspeth/web/frontend/src/components/chat/interpretationStepLabel.test.ts`; update `AcknowledgementCard`/`validationHumaniser` snapshots (locked-in-expectation discipline)

- [ ] **Step 1: Doc fix** — replace "must declare the fields as a fixed schema" with: the source must GUARANTEE `doc_bucket`/`doc_key` as required declared fields — `mode: fixed` or `mode: flexible` both work (optional `?` fields are not guaranteed); only fully-dynamic `mode: observed` fails graph validation.

- [ ] **Step 2: Failing label test** — a named llm node (`extract_invoice`) yields "Extract Invoice"; a generated id (`guided_xform_1`) still yields "Summarise".

- [ ] **Step 3: Implement label** — in `humaniseStepLabel`, when `nodeId` doesn't match the generated-id pattern (`/^guided_[a-z]+_\d+$/`), title-case the node id; fall back to `stepLabelForPlugin(plugin)` for generated ids. One choke point; all surfaces inherit it.

- [ ] **Step 4: Update snapshots; run; commit** — `git commit -m "docs+web: correct Textract schema guidance and use node names in review labels (R2-F8b)"`

---

## Workstream G — deployment (worktree `wt-terraform-teardown`)

### Task 21: R2-D3 — Container Insights log-group orphan blocks same-namespace redeploy (elspeth-a229c247a1, P2)

**Root cause (verified):** `module.scenario.aws_cloudwatch_log_group.container_insights` (`deploy/aws-ecs/terraform/modules/scenario/iam_observability.tf:199-203`) shares the exact name ECS auto-creates; the ECS service-linked role re-creates it minutes AFTER the cluster goes INACTIVE (a final Container Insights flush), so `terraform destroy` leaves an untagged, unmanaged orphan and the next apply's `CreateLogGroup` throws `ResourceAlreadyExistsException`. The `depends_on` ordering fix was already tried (commit `427c864f4`) and is structurally insufficient. The tag-based teardown sweep is blind to the orphan (it carries no run tags). No KMS involved; this is the only package resource with an AWS auto-create name collision.

**Files:**
- Modify: `deploy/aws-ecs/terraform/scenario-a/main.tf`, `scenario-b/main.tf` (variable-gated `import` block), `scenario-a/variables.tf`, `scenario-b/variables.tf` (`adopt_container_insights_log_group`, default false)
- Modify: `docs/runbooks/aws-ecs-cold-install.md` (Teardown §, ~708, and completion checklist ~761-764); `docs/runbooks/aws-ecs-deployment.md` (`destroy_scenario()` ~3352); `deploy/aws-ecs/terraform/README.md` (teardown §)
- Test: `tests/unit/deployment/test_aws_ecs_terraform_package.py` (extend `test_container_insights_performance_log_group_is_terraform_owned`, ~:298)

- [ ] **Step 1: Runbook — teardown cleanup + verification** — add an idempotent post-destroy step deleting `/aws/ecs/containerinsights/acceptance-<ns>-cluster/performance` (`aws logs delete-log-group`, ResourceNotFound-tolerant, run at verification time after the final flush; re-runnable). Add log groups (`aws logs describe-log-groups --log-group-name-prefix`) to the completion checklist — the tag sweep cannot see this orphan.

- [ ] **Step 2: Failing package test** — assert both root `main.tf`s contain a gated `import` block targeting `module.scenario.aws_cloudwatch_log_group.container_insights`, both root `variables.tf`s declare `adopt_container_insights_log_group` defaulting false, and the cold-install runbook Teardown section mentions `containerinsights`/log groups.

- [ ] **Step 3: Implement the gated import** — `import { for_each = var.adopt_container_insights_log_group ? [1] : [] ; to = module.scenario.aws_cloudwatch_log_group.container_insights ; id = "/aws/ecs/containerinsights/${...}-cluster/performance" }` in each root (`required_version >= 1.14` supports for_each-gated imports); default false so fresh accounts are unaffected, set true only on the documented redeploy-retry path. Formalizes the tester's non-destructive `terraform import` workaround.

- [ ] **Step 4: Run the terraform-fmt/validate-gated package test + the static assertions; commit** — `git commit -m "fix(deploy): tolerate orphaned Container Insights log group on same-namespace redeploy (elspeth-a229c247a1)"`

---

## Findings register

| Finding | Sev | Verdict | Workstream / Task | Effort | Tracker |
|---|---|---|---|---|---|
| R2-F16 | P1 | confirmed | A / T1 | S | elspeth-bcc6bdac99 |
| R2-F17 | P1 | confirmed (display theory refuted) | A / T2 | M | elspeth-5c0c09db31 |
| R2-F4 | P3 | confirmed | A / T3 | S | new |
| R2-F15 | P1 | confirmed | B / T4 | M–L | elspeth-a96b2f1b0a |
| R2-F6 | P2 | confirmed | B / T5 | M | new |
| R2-F12 | P2 | confirmed (reclassified: new backend channel, not a regression) | C / T6 | S–M | elspeth-bff8fe6864 |
| R2-F13 | P2 | confirmed (+ fence-escape vuln) | C / T7 | S | new |
| R2-F14 | P2 | confirmed | C / T8 | M | new |
| R2-F8a | P3 | confirmed (structural-guardrail gap) | C / T9 | S–M | new |
| R2-F9 | P2 | confirmed (partial IS salvaged) | D / T10 | S + config | new |
| R2-F10 | P2 | confirmed | D / T11 | M | new |
| R2-F18 | P2 | confirmed (tooling gap) | D / T12 | S | new |
| R2-F11 | P2 | confirmed | E / T13 | M | elspeth-b5180a9630 |
| R2-F7 | P2 | confirmed | E / T14 | S | new |
| R2-F16b | P2 | confirmed (doc-token half refuted) | E / T15 | M | new |
| R2-F1 | P2 | confirmed (dead-config theory refuted; legibility gap) | E / T16 | S | new |
| R2-F2 | P3 | confirmed | F / T17 | M | new |
| R2-F3 | P3 | confirmed | F / T18 | M | new |
| R2-F5 | P4 | confirmed | F / T19 | S | new |
| R2-F8b + label | P3 | confirmed | F / T20 | S | new |
| R2-D3 | P2 | confirmed (depends_on fix refuted) | G / T21 | S–M | elspeth-a229c247a1 |

Not defects (verified, no code change): R2-F1's terraform allowlist entry is intentional (kept, documented); the "docs promise `http_audit_integrity_error`" claim in R2-F16b (no doc names the token).

## Sequencing & parallelization

**The serialization constraint is `src/elspeth/web/composer/service.py`.** Workstreams A, B, C, and D all edit it (different functions, but a shared 11k-line file). Options:

1. **Recommended — one composer worktree, sequential tasks A→B→C→D**, then the frontend/telemetry/deploy worktrees (E, F, G) in parallel alongside it. E touches `service.py` only via `implicit_decisions.py`/`_helpers.py`/`app.py` (Task 13/15 — no `service.py` body edits), F and G don't touch it at all, so E/F/G parallelize safely with the composer stream.
2. If parallelizing A–D, assign each an explicit disjoint function range in `service.py` and reconcile at the release boundary (per the parallel-worktree flow doctrine) — higher merge risk; only worth it under time pressure.

**P1-first order within the composer stream:** T1 (F16) → T2 (F17) → T4 (F15) are the three P1s and the "unreachable-and-unrepairable guided routing" cluster — land them first. T6/T8 (advisor) unblock T9's shared constraint-fidelity rubric, so do C before finalizing T9.

**Single schema-epoch bump:** T4 and T13 both need a `SESSION_SCHEMA_EPOCH` increment — do it once (40 → 41) in whichever lands first; the deploy wipes `sessions.db` once.

## Verification gate (operator decision: full 10-exercise re-run)

After all workstreams merge to `release/0.7.2` and the full `pytest tests/` + `elspeth-lints check` pass:

- [ ] Rebuild + push images from the merged HEAD; roll the stack (the live `cf548430` stack is the target; epoch bump ⇒ `sessions.db` wipe on deploy).
- [ ] Re-drive all ten exercises through the Web Composer UI with Playwright, both surfaces, per the run-2 prompt (`docs-archive/acceptance/2026-07-31-web-composer-acceptance-r2/`), including the four never-attempted ones: **6** (2+ chained gates → 3+ sinks), **7** (non-LLM `on_error` → quarantine + the llm-on_error negative probe), **9** (fork → same field renamed → union coalesce — the prior run's killer), **10** (invented).
- [ ] Re-verify every regression item: F16 correction re-plans (no `AuditIntegrityError`); F17 gate routes each row to exactly one sink and echoes the threshold; F15 wrong-stage intent retained and the author's words preserved; F9 timeout offers the salvaged draft; F10 guardrails auto-wired and disclosed; F7 consent dialog lists Textract/Bedrock/S3; F11 no blob path in any error body; F12/F13 no advisor monologue or sentinel in the transcript.
- [ ] Record run IDs and content-verify outputs (a run ID over an adjective). File any new finding under `acceptance-2026-07-31`.
- [ ] Tear down or hand back the stack per the operator's decision; if torn down, run the Task-21 log-group cleanup and DNS-preserving teardown.

---

## Execution handoff

**Plan complete and saved to `docs/superpowers/plans/2026-08-01-acceptance-r2-remediation.md`.** 21 tasks across 7 workstreams; every finding verified at HEAD `2b47e5658` with a live stack for the final gate. Recommended: the composer stream (A→B→C→D) sequential in one worktree, with E/F/G in parallel worktrees, P1s (T1/T2/T4) first.
