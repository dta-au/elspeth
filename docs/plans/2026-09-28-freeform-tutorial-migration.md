# Freeform first-run tutorial implementation plan

> **For agentic workers:** Use the test-driven-development and executing-plans workflows task by task. Each checkbox records a concrete action and its verification.

**Goal:** Replace the tutorial's guided Build with ordinary provider-backed freeform composition, retaining the live Run, Audit, and Graduation capstone and its saved session.

**Architecture:** The browser obtains the configured public sample-page URLs from an authenticated, read-only tutorial data route, then submits one fixed complete brief through the normal freeform message action. Ordinary Composer proposals and interpretation reviews remain user-owned. A read-only tutorial readiness query applies the same saved-state admission as the Run endpoint; Run rechecks and executes the exact committed state. No tutorial-specific authoring path or server-authored graph is introduced.

**Tech stack:** FastAPI/Pydantic/SQLAlchemy backend; React/Zustand/TypeScript frontend; Pytest, Vitest, Playwright.

**Reviewed base:** `release/0.8.1` at `493afe210`. Composer extraction `c38d50da6` leaves `ComposerServiceImpl.compose()` coordinating freeform turns and `PlanningApplication` owning empty-pipeline planning. Sessions extraction `6f2ccb32d` leaves the public session service current-state and ownership APIs stable. This migration uses those APIs, not their newly extracted internals.

---

### Task 1: Read-only tutorial data and readiness

**Files:** `src/elspeth/web/composer/tutorial_run_routes.py`, `tutorial_service.py`, `tutorial_models.py`, `tests/integration/web/test_tutorial_routes.py`, `src/elspeth/web/sessions/routes/composer/guided.py`, `tests/integration/web/composer/guided/test_get_guided_tutorial_sample.py`.

- [x] Add a route test that an owned freeform session receives exactly the three runtime sample URLs at `GET /api/tutorial/{session_id}/sample`; another user's session and unknown IDs return 404. Run that test and observe a route-not-found failure.
- [x] Add the read-only route using `verify_session_ownership`, `tutorial_sample_base_url`, and `resolve_tutorial_sample_urls`. Run the focused route tests and observe success.
- [x] Add a route test for `GET /api/tutorial/{session_id}/readiness`: missing/unsupported state yields a typed not-ready result, and an accepted state returns its exact state ID. Run it red.
- [x] Expose the existing `_require_tutorial_launch_readiness` decision through that route without mutating or authoring state. Keep `POST /api/tutorial/run` rechecking. Run focused backend tests green.
- [x] Remove the old guided-only sample route and rewrite its tests for the new owner-scoped route; run both route suites.

### Task 2: Freeform Build and lifecycle

**Files:** `src/elspeth/web/frontend/src/components/tutorial/TutorialFreeformShell.tsx` (new), `HelloWorldTutorial.tsx`, `tutorialMachine.ts`, `tutorialDeparture.ts`, `TutorialWorkspaceFrame.tsx`, `copy.ts`, `src/elspeth/web/frontend/src/api/client.ts`, frontend API types/decoders, relevant `.test.tsx` and `.test.ts` files.

- [x] Write failing tests for the complete fixed brief containing live sample URLs; a single click submits it with `sessionStore.sendMessage`, never `/guided/start` or `/guided/respond`. A reload displaying existing user messages does not re-submit.
- [x] Implement a freeform shell that `selectSession`s the new or resumed session, renders ordinary `ChatPanel` and proposal/review controls, and uses the normal workspace frame. The shell does not auto-accept, normalize, or repair planner output.
- [x] Write failing tests that Build cannot advance with in-flight compose, pending proposals, pending interpretation reviews, stale session selection, or failed tutorial readiness. Advance only after explicit user acceptance and readiness of the current committed state; recheck after state changes.
- [x] Replace guided terminal completion with the readiness-driven Build→Run handoff. On Run's typed 409, return to Build with an actionable message and the same session.
- [x] Replace guided departure assertions with session-bound freeform/in-flight-operation assertions, preserving run cancellation, Skip, stale-session recovery, and idempotent completion publication. Run focused frontend tests green.

### Task 3: Persisted stages and retirement cleanup

**Files:** `src/elspeth/web/preferences/models.py`, `service.py`, `src/elspeth/web/sessions/models.py`, schema epoch facts/tests, `src/elspeth/web/frontend/src/types/api.ts`, `api/preferencesDecoder.ts`, `tutorialMachine.ts`, tutorial copy and tests, `docs/architecture/adr/031-tutorial-is-a-fixed-script-canary.md`.

- [x] Write failing contract tests for persisted `build` and rejection of obsolete `guided` tutorial stage; verify red.
- [x] Change the stage union, DB CHECK, frontend decoder, and schema epoch consistently. Prefer a clean pre-1.0 schema cut over a semantic compatibility alias; the operator approved a paired local Sessions/Landscape reset for acceptance.
- [x] Remove guided-only tutorial shell/copy/tests after equivalent freeform coverage exists, and update ADR-031 to name the freeform fixed-script canary. Run schema/contract/frontend tests green.

### Task 4: Capstone and integration acceptance

**Files:** `TutorialTurn4Run.tsx`, `TutorialTurn5AuditStory.tsx`, `TutorialTurn7Graduation.tsx`, `tests/e2e/tutorial.spec.ts`, focused backend and frontend capstone tests.

- [x] Prove Run remains explicit and uses the committed state. Persist the real run ID and source hash before Continue; reloading Run/Audit must not execute again.
- [x] Prove Audit reads that exact run's Landscape summary and Graduation renames/reopens the same finished session before publishing completion.
- [x] Cover Exit during compose and run, missing resumed session, failed readiness, retry, Skip, and Cancel. Run focused backend/frontend tests, typecheck, lint, and production build.
- [x] Run affected whole-tree gates and PostgreSQL tests for the schema change; compare trust-tier findings against base without signing. Use the frozen full-suite gate before merge where required by shared contracts/schema reach.
- [x] Commit only task paths, run branch safety, merge into local `release/0.8.1`, verify ancestry and integrated tests. Do not push.
- [x] Rebuild served frontend and restart the local application. Create one disposable local account, complete Welcome→Build→Run→Audit→Graduate in a real browser, verify persisted session/audit evidence, then delete only that account.

**Local acceptance:** `01d96af9a` was fast-forward merged into `release/0.8.1`; no push. The served build and `/api/ready` passed. A fresh account completed the real browser tutorial through Graduation. Sessions run `f630cfda-becb-4d7f-b164-85283149aab8` completed with 3/3 successful rows; its matching Landscape run contains 3 source rows and recorded LLM calls. The completed preference was persisted, the disposable credential removed and identity disabled, and `dta_user` remained active with admin role. The paired pre-reset databases were archived under `data/archives/freeform-tutorial-epoch70.0Z2OuANV/`.

## Acceptance invariants

- Every tutorial Build mutation reaches the ordinary freeform provider-backed Composer; no `provider="server"` structure authoring or tutorial-special backend authoring branch.
- Run starts only on user click, consumes the approved current state, and creates real audit and output evidence.
- Audit and Graduation remain bound to the same session and run; reload does not repeat LLM spend.
- Local validation preserves unrelated checkout changes, existing accounts, and any shared session data not explicitly authorized for reset.
