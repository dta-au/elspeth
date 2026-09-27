# Guided Mode Removal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task by task. Checkboxes track implementation, not approval. Work in an isolated worktree; this document authorizes no database deletion or deployment by itself.

**Goal:** Remove Guided Composer completely, leaving freeform as the only Web Composer authoring surface while preserving ordinary session fork/revert, proposal review, execution, audit, and the freeform first-run tutorial.

**Architecture:** This is a hard cut, not a deprecation shim. Guided routes, workflow, wire types, planner branches, persisted state, preference mode, and guided-specific tests disappear. The `guided_operations` ledger cannot simply be deleted because ordinary session fork and state revert currently use it; first give those operations a narrow, mode-neutral durable idempotency/fencing owner, then remove the guided ledger and all remaining consumers. No compatibility reader, old-session conversion, archived Guided UI, or dual-write period survives the final change.

**Tech stack:** FastAPI, Pydantic, SQLAlchemy/SQLite/PostgreSQL, React/Zustand/TypeScript, pytest, Vitest, Playwright.

**Baseline:** `release/0.8.1` at `48ef86352` (includes freeform tutorial commit `01d96af9a`). Revalidate file ownership and dependencies against the execution HEAD; the sessions and composer service extractions have recently landed. Sessions schema epoch is 70; Landscape epoch is 46. The exact next epoch must come from the execution HEAD, not this snapshot.

---

## Scope and cutover contract

- No new Guided sessions or mode selection; no `/guided` API, frontend Guided workflow, guided planner profile/skill, or guided durable operation.
- No reader, converter, migration, or UI for old Guided session history. Existing Guided records are deliberately discarded at the cutover; do not contaminate freeform state by silently projecting them into a new shape. This is a pre-1.0 schema break.
- Keep mode-independent features that borrowed Guided machinery: session fork, state revert, blob-copy custody, session-operation fences, proposal acceptance/rejection, interpretation review, YAML export, execution admission, and audit. Their authority and replay guarantees must remain at least as strong as today.
- Keep the fixed-script tutorial on the ordinary freeform backend, including explicit Run, evidence-backed Audit, and Graduation. Do not add a tutorial-only authoring branch or server-authored graph.
- Remove obsolete Guided material from **active** product docs, tests, harnesses, and public site. Historical Git commits/changelogs need no compatibility code and are not rewritten.
- No shared-store deletion during plan execution without a separately approved deployment cutover. The prior authorization to reset the local disposable Sessions/Landscape stores does not imply authority over other deployments. Preserve `auth.db` and current user activation on any local paired reset.

## Task 0 — Freeze a measured dependency map

**Read:** `src/elspeth/web/sessions/routes/composer/{guided.py,guided_plan.py,state.py,pipeline_settlement.py}`, `src/elspeth/web/sessions/routes/sessions.py`, `src/elspeth/web/coordination/repository.py`, `src/elspeth/web/blobs/service.py`, `src/elspeth/web/sessions/{models.py,protocol.py,service.py,mutation_capabilities.py}`, `src/elspeth/web/composer/{state.py,planning_application.py,pipeline_planner.py,service.py}`, `src/elspeth/web/frontend/src/{App.tsx,components/chat/ChatPanel.tsx,stores/sessionStore.ts}`.

- [ ] Confirm branch, clean task worktree, exact current schema epochs, and actual import provenance. Read `CONTRIBUTING.md` Whole-tree gates before product edits.
- [ ] Inventory **registered** routes from the FastAPI app and **registered** tables from SQLAlchemy metadata, with known-positive freeform and known-negative fake controls. Record Guided route/table names and exact consumers. Do not use a source regex count as a gate.
- [ ] Trace all `guided_operations` uses by behavior, not name. In particular, `state_revert` in `routes/composer/state.py` and `session_fork` in `routes/sessions.py` are user-visible non-Guided workflows; fork also gates blob copy and cleanup in `blobs/service.py` and `coordination/repository.py`.
- [ ] Record the baseline behavior and PostgreSQL contention tests for fork, revert, blob custody, proposal review, freeform compose, and tutorial. Compare every later gate to the same base if it fails.

**Done when:** The map distinguishes disposable Guided-only code from retained generic behavior, and lists every registered route/table to remove or replace.

## Task 1 — Replace the borrowed operation ledger for fork and revert

**Primary files:** `src/elspeth/web/sessions/{models.py,protocol.py,service.py,mutation_capabilities.py,guided_operation_rules.py}`, `src/elspeth/web/sessions/routes/{guided_operations.py,sessions.py,composer/state.py}`, `src/elspeth/web/coordination/{repository.py,lifecycle.py}`, `src/elspeth/web/blobs/service.py`, `src/elspeth/contracts/blobs.py`.

**Tests:** existing fork/revert/fence/cleanup suites under `tests/unit/web/sessions/`, `tests/unit/web/blobs/`, `tests/integration/web/composer/`, and `tests/testcontainer/web/`; add focused generic-operation tests in the relevant existing suites.

- [ ] Write failing tests for two concurrent identical fork requests (one child and replayed same response), different-request-key conflict, expired-owner takeover, process death during blob copy, cancellation after durable settlement, and cross-session/forged fence rejection. Repeat for revert: one new state, exact response replay, no duplicate interpretation surfacing, and stale-head refusal. Include PostgreSQL writer contention, not SQLite alone.
- [ ] Introduce one **mode-neutral** durable request/receipt contract for `session_fork` and `state_revert` with immutable request binding, terminal result locator/hash, lease/attempt fencing, and terminal audit cohort. Use the existing `SessionOperationContext`/fence for live writer authority; add only the receipt state it cannot currently express. Do not rename `guided_operations` while retaining its guided-only columns or leave an alias to it.
- [ ] Move fork/revert routes, service protocol/results, repository and blob-copy checks to that neutral contract. Preserve parent-plus-child fork fencing, settled replay without fresh writes, post-verified repair of owed interpretation reviews, and cleanup idempotency. Replace `SessionForkParentAuthority.guided_fence` and other nominal Guided types rather than lying about their meaning.
- [ ] Run the focused SQLite and serial PostgreSQL tests. Only after those pass, remove fork/revert kinds and consumers from the Guided ledger. This intermediate tranche may coexist with Guided authoring, but the final tree may not.

**Done when:** Ordinary fork and revert work with their full replay/custody guarantees while the Guided ledger can be deleted without affecting either feature.

## Task 2 — Remove Guided backend authoring and session contracts

**Delete after callers migrate:** `src/elspeth/web/composer/guided/`, `src/elspeth/web/sessions/routes/composer/{guided.py,guided_plan.py,guided_chat_atomic.py,guided_chat_intent_management.py,guided_proposal_rebase.py}`, `src/elspeth/web/sessions/routes/guided_operations.py`, Guided-only modules under `src/elspeth/web/sessions/` (`guided_operations.py`, `guided_operation_rules.py`, `guided_replay.py`, `guided_audit.py`, `guided_payloads.py`, `guided_proposal_authority.py`, `_guided_step_chat.py`), and `src/elspeth/web/composer/guided_blob_refs.py` if no generic custody consumer remains.

**Modify:** `src/elspeth/web/sessions/routes/composer/{__init__.py,state.py,proposals.py,pipeline_settlement.py}`, `src/elspeth/web/sessions/routes/{__init__.py,messages.py,sessions.py}`, `src/elspeth/web/sessions/{schemas.py,converters.py,protocol.py,service.py}`, `src/elspeth/web/composer/{state.py,planning_application.py,pipeline_planner.py,pipeline_proposal.py,service.py,redaction.py,yaml_generator.py}`, `src/elspeth/web/execution/{service.py,routes.py}`, `src/elspeth/web/coordination/{repository.py,library_authority.py}`, `src/elspeth/contracts/{blobs.py,errors.py,composer_audit.py,composer_llm_audit.py,composer_interpretation.py}`.

- [ ] Add a route-registry test that all former `/api/sessions/{id}/guided...` paths are absent and freeform compose/proposal/state/fork/revert/tour routes remain registered. Cover unknown Guided URL requests with the API's normal 404, without replacement compatibility handlers.
- [ ] Delete Guided planner entrypoints (`plan_guided_*`), step skills/profile/state machine, response schemas, route handlers and per-mode transition prompts. Keep the canonical provider-backed freeform planner and shared required-control, validation, proposal, audit, and tool machinery. Move genuinely shared helpers to neutral modules only if a live freeform caller proves the dependency.
- [ ] Remove `GuidedSession` from `CompositionState` and persisted `composer_meta`; remove guided-specific proposal surface variants and their accept/reject exceptions. Retain freeform proposal and interpretation review lifecycles. No server-authored fallback may replace the deleted planner path.
- [ ] Remove Guided-only blob custody admission and error types after the generic fork/blob checks from Task 1 pass. Keep freeform blob ownership/custody proofs and sink/run admission intact.
- [ ] Delete the Guided operation tables, triggers, enums, and JSON schema version from the Sessions model. Bump `SESSION_SCHEMA_EPOCH` for **each** independently runnable schema-changing tranche (including Task 1's neutral receipt, if it lands separately); never accept a changed table set at an old epoch. Update SQLite/PostgreSQL schema identity checks and exact-schema tests. Bump Landscape epoch only if Landscape schema actually changes; still plan a paired Sessions/Landscape recreation where the deployment treats their records as linked disposable state.
- [ ] Run backend import, contract, targeted route, composer, execution, blob, fork/revert, and PostgreSQL tests after each coherent deletion tranche. Do not remove a shared test solely because its name says “guided”; move its invariant to a freeform/generic test before deleting it.

**Done when:** No Guided route, authoring branch, state DTO, provider surface, session table, or runtime Guided dependency remains, and generic flows retain their fail-closed checks.

## Task 3 — Remove the Guided frontend, mode preference, and stale tutorial coupling

**Delete after callers migrate:** `src/elspeth/web/frontend/src/components/chat/guided/` except any component deliberately moved to a neutral path, `src/elspeth/web/frontend/src/types/guided.ts`, `src/elspeth/web/frontend/src/api/guidedDecoder.ts`, `src/elspeth/web/frontend/src/stores/{guidedOperationRetry.ts,guidedReviewedComponents.ts}`, Guided fixtures/tests and Guided-only E2E specs.

**Modify:** `src/elspeth/web/frontend/src/{App.tsx,types/api.ts,api/client.ts,api/preferencesDecoder.ts,stores/sessionStore.ts,stores/preferencesStore.ts,components/chat/ChatPanel.tsx,components/settings/ComposerPreferencesPanel.tsx,components/sidebar/ExecuteButton.tsx,components/workspace/WorkspaceActionBar.tsx,components/tutorial/TutorialWorkspaceFrame.tsx,components/tutorial/tutorialDeparture.ts}` and affected CSS/tests.

- [ ] Add failing tests for a freeform-only app: new/resumed sessions render normal chat, pending compose still blocks Run, review and proposal controls work, and tutorial completion no longer submits a redundant `default_mode="freeform"`. Confirm settings still save theme/advanced/tutorial preferences without a mode field.
- [ ] Remove mode radio controls, conversion/re-entry buttons, Guided state/actions/retry selectors, Guided UI/workspace branches, strict Guided decoder/types and API methods. Make freeform the unconditional authoring surface rather than preserving `ComposerMode = "freeform"` and dead mode branching.
- [ ] Move any still-used generic component (for example `PipelineValidationSummary` imported by `TutorialWorkspaceFrame.tsx`) out of the Guided directory before deleting it. Remove stale Guided state assumptions and names from tutorial tests, copy and CSS; preserve the currently working freeform Build→Run→Audit→Graduation sequence.
- [ ] In the same tranche, remove `default_mode` from `ComposerPreferences` request/response, service, Sessions table, frontend decoder/store, and telemetry that only counts mode opting. Keep tutorial-completion intent validation and progress persistence, but decouple them from `default_mode`.
- [ ] Run focused Vitest, TypeScript checks, accessibility checks, production build and browser E2E for freeform compose/proposals, fork/revert, and tutorial. Assert the built app contains no Guided control or route calls.

**Done when:** There is one composer authoring UI and one preference contract, with no hidden Guided branch or stale default-mode semantics.

## Task 4 — Replace, not merely delete, Guided acceptance coverage

**Files:** `src/elspeth/web/frontend/tests/e2e/tutorial.spec.ts`, `src/elspeth/web/frontend/tests/e2e/helpers/tutorial-harness.ts`, `src/elspeth/web/frontend/tests/e2e/harness/{transition-ledger.ts,transition-ledger.test.ts,tutorial-planner-shape.ts,tutorial-planner-shape.test.ts,guided-driver.ts}`, `src/elspeth/web/frontend/scripts/{staging-tutorial-harness.mjs,staging-tutorial-driver.mjs}`, `scripts/acceptance_battery.py`, `evals/composer-parity/`, `evals/composer-harness/hardmode/`, `evals/2026-05-03-composer/hardmode/`.

- [ ] Retain a per-**transition** provider-call assertion on freeform Build: each transition that publishes proposed structure must have a real provider attempt, not a walk-wide count. Retain source/result/review/audit assertions from the earlier Guided canary where still product-relevant.
- [ ] Port the staging tutorial driver/harness that still sends `/guided/start`, `/guided/chat`, `/guided/respond` and sets `default_mode="guided"` to the current freeform tutorial. Remove Guided-only drivers, shape tests, snapshots and calibrated battery lanes after their generic expectations have moved.
- [ ] Keep at least one route-mocked full tutorial browser test and one real-provider, real-run acceptance recipe. Verify the existing hardmode harnesses cover needed generic Composer behavior before retiring Guided-specific parity/battery files; do not create a third ad-hoc harness.
- [ ] Delete any test that exists only to pin a removed Guided wire shape. Move shared graph, collector, approval, blob, cancellation, and replay invariants to freeform/generic suites first.

**Done when:** Test deletion does not erase the invariant that the deleted Guided tests happened to exercise.

## Task 5 — Active documentation and release contract cleanup

**Files:** `AGENTS.md`, `CONTRIBUTING.md`, `README.md`, `website/{authoring.html,get-started.html}`, `docs/architecture/adr/031-tutorial-is-a-fixed-script-canary.md`, current Composer/API/runbook documentation, schema inventory/contract manifests and governed baselines discovered in Task 0.

- [ ] Remove the obsolete Guided collector parity covenant and Guided frontend/backend instructions from active guidance. Keep the two non-negotiable Composer rules (provider authors structure; tutorial uses ordinary backend) and rewrite ADR-031's current rule around the freeform canary.
- [ ] Remove public Guided authoring claims and examples, obsolete mode-setting API examples, Guided-specific acceptance commands, and active “guided schema version” release instructions. Do not rewrite historical changelog entries as if Guided never existed.
- [ ] Update exact route/schema/DTO inventories and generated API docs from the live registries. Update trust-tier/attribute/masquerade baselines only for real deletions or moves; do not hand-edit or sign judge metadata. The operator signs only after merge under the normal package-level workflow.

**Done when:** A new contributor sees one current Composer path and no instructions to call deleted APIs or resurrect Guided.

## Task 6 — Integration, cutover, and acceptance

- [ ] Freeze the candidate and run `scripts/full-suite-gate.sh --execute --detach --stages ruff,mypy,contracts,pytest,testcontainer` from the task worktree after confirming host test capacity. Read `summary.txt` and require `frozen=yes`; run the keyless trust-tier lint and compare its finding set with base without signing. Run frontend typecheck, unit/E2E, and production build. Keep default pytest and serial PostgreSQL results separate.
- [ ] Verify absence by registered routes, SQLAlchemy metadata, OpenAPI, frontend built entrypoints and actual settings/tutorial UI—not solely by `rg`. Use positive controls that must find freeform endpoints/tables and negative controls that must reject a synthetic Guided endpoint/table. Review any remaining `guided` text individually: a historical release note is not live functionality, but a retained import, API type, branch, fixture, or current instruction is a failure.
- [ ] Verify ordinary freeform compose (provider call per structure-producing transition), proposal/review, validation, run, audit, YAML export, session fork/revert under contention, blob custody, and tutorial Welcome→Build→Run→Audit→Graduation. Confirm the final app never makes a Guided request.
- [ ] Before any deployment reset, inventory the target's actual Sessions/Landscape/Auth stores and active users. Obtain deployment-specific approval, quiesce writers, archive the paired Sessions/Landscape stores, recreate at the new epochs, preserve `auth.db`, and re-admit legitimate users as required. Do not turn the local disposable-store permission into a blanket production deletion instruction.
- [ ] Merge the verified candidate locally to the intended release branch after branch-safety checks; reverify ancestry, integrated frontend bytes and app readiness. Publication/deployment is a separate authorization. Do not claim the cut is complete while any Guided route, table, UI path, or deployment still serves old bytes.

**Final definition of done:** The shipped Composer has only freeform authoring; the tutorial still completes with a real run and audit; fork/revert retain their durable safety properties; active contracts/docs/harnesses contain no Guided mode; old Guided state has no runtime reader or compatibility path.
