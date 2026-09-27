# Composer Application Owners Implementation Plan

> **Execution:** Implement this plan in the dedicated worktree only after Astra reviews it. Complete each owner and its callers before beginning the next; do not retain forwarding aliases or whole-service dependency bundles.

**Goal:** Reduce `src/elspeth/web/composer/service.py` from 9,886 lines toward a real turn coordinator of roughly 4,700–5,200 lines while preserving authoring, provider, audit, cancellation, persistence, and review behavior.

**Architecture:** `ComposerServiceImpl` remains the freeform turn driver. Cohesive application owners handle interpretation surfacing, provider attempts, advisor checkpoints, runtime preflight, guided/freeform planning, and completion/repair. Existing modules that receive the entire service (`tool_batch`, `turn_audit`, `no_tool_finalize`, `availability`) receive the specific owned collaborators and data they use instead. A construction-only factory shared by the web app and `ComposerServiceImpl.for_trained_operator` assembles long-lived owners; per-compose facts stay per call. No server path authors graph structure in place of the LLM, and Guided/tutorial paths use their existing shared planner/backend contracts.

**Tech stack:** Python 3.13, asyncio, FastAPI session routes, LiteLLM, SQLAlchemy-backed session service, pytest/xdist, Ruff, mypy, `elspeth-lints`.

**Prerequisites:** Base is `release/0.8.1` at `b6a94d73b`. Worktree is `.claude/worktrees/composer-application-owners-20260927`; `.venv` symlinks to the main venv. Every test/lint command exports `PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src"`. The operator-held judge HMAC key remains unavailable to agents. Read `CONTRIBUTING.md` whole-tree gates and `docs/agents/recent-code-hints.md` before edits.

---

## Non-negotiable invariants and scope

1. The planner/provider authors every proposed graph. No server-authored proposal, rootless shortcut, tutorial branch, or changed per-transition provider-call count.
2. Preserve exact provider request/response bytes where no contract change is intended, quota admission before dispatch callback before provider call, pricing identity, error classification, and recorded attempt counts.
3. Preserve writer/lease authority, evidence lookup over all interpretation resolution statuses, mutation/operation fencing, audit-before-publication, cancellation settlement, and message insertion/withdrawal order.
4. Preserve one nominal identity per result/exception type. Update every production import, direct test import, monkeypatch, protocol, exact-source gate, whitelist and census to the new owner in the same tranche. Do not leave test-only accepted raw provider objects or compatibility exports solely to keep old test seams alive; replace those tests with production-boundary assertions.
5. No new `service` pointer in an extracted module, mixin, callback bag standing in for the service, duplicated planner/provider/policy implementation, lint suppression, staged signing work, schema change, or unrelated policy change. An explicit immutable deployment dependency and a per-call mutable turn fact are separate types when needed.
6. A source-body comparison may prove only a pure move. Rewired dependencies, altered result ownership and lifetime must have behavioral tests and independent review.

## Task 0 — Baseline and contract map

**Files:** Read `src/elspeth/web/composer/service.py`, `protocol.py`, `tool_batch.py`, `turn_audit.py`, `no_tool_finalize.py`, `availability.py`, `pipeline_planner.py`, `src/elspeth/web/app.py`, `src/elspeth/web/sessions/routes/composer/{guided,state}.py`; write evidence only under ignored `.claude/lanes/composer-application-owners-20260927/`.

1. Verify clean worktree, both source-root imports, release ancestry, and no concurrent broad suite ownership conflict. Record a live AST inventory of moved definitions with a positive control (`compose`) and negative control (nonexistent definition), and save original source bytes/hash.
2. Inventory actual production imports, `ComposerService` protocol methods, service-field reads in existing extracted modules, and test monkeypatch targets via Python AST. Separate genuine runtime contracts from test seams. Build the **transitive definition/dependency map** before the first move: every selected method's called top-level helper, constant, carrier, exception/result identity, constructor collaborator and mutable field; mark its owner and whether its lifetime is deployment, service-instance, compose-call, or turn. Include `_plugin_policy_context`, `_require_chargeable_admission`, `for_trained_operator`, and direct session route consumers. A tranche is not ready if one selected body would import back from service or require a bound service callback.
3. Run a baseline of focused tests for interpretation, provider, advisor, preflight, guided planning, completion, and tool/audit behavior. Log each suite and its exit code; diagnose any baseline red before modifying source. Do not run the full default or PostgreSQL suite yet.

**Definition of done:** A bounded source-backed transitive ownership map and baseline logs identify every migration site and negative-control behavior. Astra checks this map before the first extraction. No production edits.

## Task 0A — Shared application policy and admission prerequisites

**Create:** `src/elspeth/web/composer/application_policy.py` for the small authenticated/trained-operator policy-context factory; `src/elspeth/web/composer/chargeable_admission.py` for sessions-backed COMPOSE admission with explicit operation context.
**Modify:** `service.py`, `src/elspeth/web/composer/protocol.py` (canonical `ComposerAdmissionRefused`), route imports in `src/elspeth/web/sessions/routes/{_helpers.py,messages.py,composer/compose.py,composer/guided.py,composer/guided_plan.py}`, and construction entrypoints `src/elspeth/web/app.py` and `ComposerServiceImpl.for_trained_operator`.

1. Move `_plugin_policy_context` (2699–2712) and `_require_chargeable_admission` (4093–4111) as two distinct operations. Policy-context construction preserves the fresh per-call user snapshot and trained-operator catalog behavior; admission preserves exact session-operation-kind checks and refusal/error identity. Do not conflate application chargeable admission with physical-provider quota admission.
2. Use one construction-only owner assembly path for web and non-web composition. Preserve `for_trained_operator` as a working entrypoint but do not give new owners the service object. Update every route that imports `ComposerAdmissionRefused` to the canonical protocol type.
3. Migrate the coordinator's calls and run authenticated/trained-operator policy and admission tests, plus relevant PostgreSQL admission authority cases. Astra reviews this prerequisite before the first larger owner move.

**Definition of done:** Policy snapshot and application admission have one owner each; future preflight/planning owners can receive them directly without a callback into service.

## Task 1 — Interpretation review owner

**Create:** `src/elspeth/web/composer/interpretation_surfacing.py`.
**Modify:** `service.py`, `src/elspeth/web/composer/protocol.py` (`ComposerService`), `src/elspeth/web/sessions/routes/composer/guided.py`, `src/elspeth/web/sessions/routes/composer/state.py`, `src/elspeth/web/sessions/routes/composer/pipeline_settlement.py`, `src/elspeth/web/execution/routes.py`, `src/elspeth/web/app.py`.
**Tests:** `tests/unit/web/composer/test_surface_pending_interpretation_reviews.py`, `test_request_interpretation_review_kind_boundary.py`, `test_compose_loop_interpretation_review_dispatch.py`; `tests/integration/web/composer/guided/test_respond.py`, `test_guided_interpretation_run_backstop.py`; relevant `tests/testcontainer/web/` interpretation/session-fence cases.

1. Move the eight standalone functions at service lines 1985–2462 **with** `_matching_requirement_draft` and `_has_pending_prompt_template_requirement` from the class. Their current static class calls at lines 2112–2213 make a standalone-only move incomplete. Move the four service-bound operations `_rate_capped_vague_term_sites` (3020–3067), `_missing_pending_interpretation_review_sites` (3069–3103), `_auto_surface_prompt_template_reviews` (3105–3149), and `surface_pending_interpretation_reviews` (3190–3248). Freeform completion/query call sites use the owner directly.
2. Give the owner `SessionServiceProtocol`, rate-limit settings and fixed provenance inputs explicitly; pass state, session/state ids, and `SessionOperationContext` per call. Keep state-only draft calculations as local pure functions. Preserve the read-before-writer sequence and `only_missing_evidence` all-status lookup.
3. Migrate Guided/state routes, `execution/routes.py:970,992`, `pipeline_settlement.py:221,435`, and all monkeypatch targets directly to the owner. Update `ComposerService` consumers so no service forwarding method remains. Update trust-boundary signature/census paths honestly and do not sign.
4. Compare pure moved bodies, run the named tests and PostgreSQL session operation/mutation-fence tests. Astra reviews the complete tranche; fix findings before commit.

**Definition of done:** One interpretation owner handles evidence, draft and publication; no service-class or route import points backward to the moved functions, and loss-of-writer tests still fail closed.

## Task 2 — Provider gateway and advisor checkpoint owner

**Create:** `src/elspeth/web/composer/provider_gateway.py`, `src/elspeth/web/composer/advisor_checkpoint.py` (use one canonical verdict type).
**Modify:** `service.py`, `src/elspeth/web/composer/boot_probe.py`, `src/elspeth/web/composer/guided/chat_solver.py`, `src/elspeth/web/sessions/_auto_title.py`, `src/elspeth/web/composer/tool_batch.py`, `src/elspeth/web/sessions/routes/_helpers.py`, `src/elspeth/web/composer/protocol.py` as contracts require.
**Tests:** `tests/unit/web/composer/{test_boot_probe,test_provider_quota,test_provider_quota_end_to_end,test_advisor_checkpoint,test_advisor_structured_checkpoint,test_advisor_terminal_publication,test_advisor_call_is_text_only,test_compose_loop_llm_audit,test_wire_fidelity_matrix}.py`, `tests/integration/web/composer/test_boot_probe_production_parity.py`, `tests/integration/web/composer/test_composer_against_gateway.py`, guided chat and automatic-title tests.

1. Establish a physical provider request/response seam covering selected definitions at service lines 607–938, 8001–8063 and 9384–9618. `boot_probe`, guided chat and automatic title call its real request/adapter owner directly. Redirect existing planner completion callables in Guided/freeform (4500, 4914, 5423) to that same physical transport now. `pipeline_planner.py` retains its distinct request, attempt, budget and custody implementation; do not route planner attempts through the primary/text audited wrappers. Endpoint secret values never enter repr-bearing carriers. The compose deadline/convergence method at 9620–9745 remains with composition because it owns turn state.
2. Preserve primary/text/advisor distinctions: cache markers before primary hash, none for advisor; requested pricing model identity; raw response admission; malformed-but-chargeable outcome and provider failure class. Convert tests that patch private service adapter symbols to patch the production gateway, with no service alias left behind.
3. Move advisor argument admission, hint/checkpoint execution, typed verdict, findings/result construction and checkpoint publication as a coherent application responsibility (selected lines 8065–8114, 8441–9381, 9807–9886). Reuse `advisor_context.py`, `advisor_policy.py`, `advisor_request.py`, `advisor_audit.py`; do not duplicate them. Advisor owns `_AdvisorCheckpointComposeDeadlineExpired` (772–778), verdict and checkpoint parsing; coordinator and completion import the one identity. Put `_advisor_preflight_shape` (1453–1461) and `_outstanding_findings_detail` (1279–1295) in the existing pure advisor policy module if Task 0 confirms no ownership conflict. Tool dispatch holds the advisor collaborator explicitly and migrates its calls in this tranche, not in Task 6.
4. Run exact request-byte, role/fence, quota-order, pricing, boot-probe parity, audit/failure/cancellation and terminal-publication tests. Run PostgreSQL chargeable admission, progress/quota lock-order and checkpoint authority cases because publication moves. Astra reviews each of the gateway and advisor sub-tranches; fix findings before commits.

**Definition of done:** Every external completion path uses the one gateway owner; advisor has one nominal verdict and owned attempt/publication path; no direct import of moved private provider helpers from `service.py` remains.

## Task 3 — Runtime preflight owner

**Create:** `src/elspeth/web/composer/composer_preflight.py`.
**Modify:** `service.py`, `tool_batch.py`, `no_tool_finalize.py`, `src/elspeth/web/app.py` if construction needs it.
**Tests:** `tests/unit/web/composer/{test_runtime_preflight_pending_review_verification,test_planner_stage_runtime_preflight,test_pending_review_mutation_preflight,test_review_preflight_failure_boundary,test_no_tool_finalize_universal_qualification}.py` and relevant integration/session cases.

1. Move selected preflight definitions at service lines 2972–3018, 3291–3497 and 3779–3908 into one owner, plus the runtime metric/failure classification helpers identified by Task 0's transitive map, including recursive `_contains_blob_ref` with `_state_contains_blob_ref`. Share the existing `RuntimePreflightCoordinator` from `app.py`; keep per-compose result cache per compose invocation. Blob-containing states continue to bypass completed-result caching; request timeout does not cancel shared worker ownership. Optional-snapshot fallback asks the shared application-policy owner for a fresh policy context.
2. Migrate planner preview, completion and `tool_batch` to explicit preflight operations/results. `no_tool_finalize` receives the computed result or this collaborator, never the whole service. Keep validation settings, catalog/policy snapshot and user/session identity explicit.
3. Run preflight/cache/failure/cancellation tests, applicable trust-tier/soft-mapping gates and Astra tranche review; fix defects before commit.

**Definition of done:** Preflight policy and cache lifetime have one owner, with no new coordinator singleton or service back-reference.

## Task 4 — Planning application owner

**Create:** `src/elspeth/web/composer/planning_application.py`.
**Modify:** `service.py`, `src/elspeth/web/composer/protocol.py`, `src/elspeth/web/sessions/routes/composer/guided_plan.py`, `src/elspeth/web/sessions/routes/composer/guided.py`, `src/elspeth/web/app.py`; other direct callers identified in Task 0.
**Tests:** `tests/integration/web/composer/guided/{test_shared_planner_surfaces,test_generic_linear_optimal_path,test_guided_full,test_progressive_disclosure,test_compose_checkpoint_anchor}.py`, `tests/integration/web/composer/test_freeform_proposal_prevalidation.py`, planner-stage and rootless tests.

1. Move the eight complete guided/empty-pipeline application definitions at service lines 4386–5528, plus `_await_pipeline_staging_write_with_deferred_cancellation` (392–424), `_required_controls_candidate_finalizer` (465–486), `_PlannerPreviewPreflightCallbacks` (1654–1664), `_freeform_planner_conversation_context` (1771–1819) and `_log_guided_planner_failure` (296–329). Call the existing canonical `pipeline_planner.py` instead of creating another planner. Route Guided directly to the owner; the freeform coordinator delegates only the empty-state planning branch, retaining ingress and turn sequencing.
2. Keep distinct custody rules: Guided full planning defers finalization for the originating-message FK; freeform failure persists recorder slices under deferred cancellation. Preserve admission, catalog/profile snapshot, proposal identity, and honest `PlannerDeclined` translation.
3. Migrate protocol/app wiring/test monkeypatches atomically; one schema-disclosure tracker owner is shared by planning, freeform message building and tool-batch disclosures immediately. The owner uses the policy-context and chargeable-admission collaborators from Task 0A, not bound service callbacks. Check provider calls **per transition**, especially rootless and tutorial entrypaths, rather than per walk. Run focused Guided/freeform parity and failure tests, then Astra review.

**Definition of done:** Guided and empty-state freeform planning use the same planner contract with their existing distinct settlement rules; no Guided wrapper stays on `ComposerServiceImpl`.

## Task 5 — Completion, repair and publication owner

**Create:** `src/elspeth/web/composer/composition_completion.py`.
**Modify:** `service.py`, `no_tool_finalize.py`, `turn_audit.py`, `tool_batch.py`, related result carriers and `src/elspeth/web/app.py` only where needed.
**Tests:** `tests/unit/web/composer/{test_no_tool_finalize_universal_qualification,test_no_tool_finalize_disclosure,test_rootless_no_tool_recovery,test_compose_loop_persistence,test_compose_loop_audit_wiring,test_compose_loop_interpretation_review_dispatch,test_advisor_terminal_publication}.py`, `tests/unit/web/execution/test_completion_gates.py`, recovery and operation-fence tests.

1. Move the completion/repair/publication definitions selected by Task 0's AST map from service lines 3499–4091 and 6294–7272, **excluding** preflight methods already owned by Task 3. Include `_CrossTurnRepairKey` (1592–1604), `_ProofRepairOutcome` (1705–1712), `_TerminalNoToolAdvisorGateOutcome` (1636–1650), `_replace_advisor_repair_public_result` (1464–1588), and the repair-message/validation constructors in 941–1371 and 1873–1982 that these methods call. The cross-turn repair ledger currently initialized around 2609–2614 moves with them. The owner consumes explicit immutable deployment policy plus per-turn state/evidence and returns existing typed continue/return outcomes. It cannot construct graph structure.
2. Preserve prior durable gate facts, advisor repair budgets, assistant-message insertion/withdrawal indexes, terminal no-tool disclosure, and audit-before-publication. Retain `_compose_loop` and `_classify_and_budget_turn` as real coordinator methods; do not merely hide them in a new file.
3. Remove `no_tool_finalize(service)` and `turn_audit(service)`. Turn audit receives sessions/redaction/provenance explicitly and returns diagnostic facts through its result instead of writing `_phase3_last_*` service fields. Migrate `_run_one_turn_for_test` (2714–2782), `ComposeLoopTestResult` (9767–9799), service's compose-loop write at 7633 and tool-batch's write at 849 to per-call diagnostics or production recorder/result assertions in the same change. Include an overlapping-compose negative control; do not replace the service fields with another singleton holder. Completion consumes the facts. `availability.compute_availability` receives explicit model/endpoint/settings inputs rather than a service object.
4. Run cancellation/fault-injection, no-tool, recovery, audit and completion-gate tests; run PostgreSQL settlement and fencing tests. Astra reviews before commit.

**Definition of done:** The turn driver owns sequencing; completion owns decision and publication; no-tool, audit and availability modules no longer take the whole service. `ToolBatchContext.service` may exist only for remaining unmoved session-tool/resource accesses assigned to Task 6; advisor/preflight/completion calls already use their owners directly.

## Task 6 — Tool-batch dependency inversion and final integration

**Modify:** `src/elspeth/web/composer/tool_batch.py`, `service.py`, owner modules from Tasks 1–5, `src/elspeth/web/composer/protocol.py`, `src/elspeth/web/app.py`; tests that construct `ToolBatchContext`.

1. Remove `ToolBatchContext.service` (currently at `tool_batch.py:656`) and every `ctx.service` read/call. Supply concrete gateway/advisor/preflight/interpretation/session-tool collaborators, owned configuration/custody resources and turn facts. Move `_dispatch_session_aware_tool` (service lines 8116–8378), `_build_session_aware_kwargs` (8380–8439), and `_SessionAwareDispatchOutcome` (1608–1633) coherently to a real session-tool owner with explicit dependencies. The large `tool_batch.py` dispatch algorithm remains in that module; invert its dependencies without an unrelated rewrite.
2. Retire obsolete service methods, reverse imports, test-only compatibility shapes, module-tail monkeypatches and duplicate schema-disclosure state. Keep the app composition root as construction only. Review every touched file in full, run import-cycle checks and exact changed-path searches with positive/negative controls.
3. Run affected tool-batch, advisor, provider, planning, preflight, Guided, audit and recovery tests. Astra performs a full cross-tranche implementation review; repair all findings and repeat affected gates.

**Definition of done:** No extracted Composer application module receives or imports `ComposerServiceImpl`; direct callers and tests use the real owners. Remaining service methods perform freeform turn coordination rather than forward every old operation.

## Task 7 — Frozen release gate and integration

1. Freeze the tree and record HEAD/content hash. Run Ruff, Ruff format, mypy, `scripts.check_contracts` (regenerate the soft-mapping census only for real moved annotations), exact error-code inventory, dynamic-attribute, masquerade, wire-fidelity and applicable trust-boundary checks. Compare keyless trust-tier JSON finding corpus with the release baseline using positive/negative controls; do not alter signatures or clear the global signing backlog.
2. Confirm host test ownership/capacity. Run the canonical `scripts/full-suite-gate.sh --execute --detach --stages ruff,mypy,contracts,pytest` once at the integrated tree and read `summary.txt` plus frozen-tree result. Record the separately invoked keyless lint gate exit/corpus and relevant exact-source gates; the script's default `ruff,pytest` is insufficient for this plan. Run `pytest tests/ -m testcontainer -n 0` serially with Docker, including targeted early PostgreSQL checks for each persistence tranche. Diagnose any red against the exact release baseline rather than declaring a rerun sufficient.
3. Verify Astra's final GO, staged path set, `scripts/branch-safety-check.sh --intent commit`, then commit the final tranche. Re-check release ancestry and integration conflicts; merge locally into `release/0.8.1` only after reviewed gates. Reverify HEAD containment, integrated imports and smoke behavior. Do not push or deploy without separate authorization.

**Definition of done:** The full Option C boundary is implemented without new technical debt, every required gate has recorded exit status, Astra has no outstanding actionable finding, and local release integration is measured separately from remote and deployment state.
