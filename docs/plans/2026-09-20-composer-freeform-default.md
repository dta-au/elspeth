# Composer Freeform Default Implementation Plan

**Goal:** Make Freeform the default for new accounts and the destination of every tutorial departure, with Guided available only through deliberate discovery.

**Architecture:** Account preferences determine new-session mode; persisted session state determines the surface of an existing session. Tutorial departure must coordinate both through the existing preferences API and authoritative Guided exit action. Keep the tutorial on the ordinary Guided backend and retain all provider, validation, and audit invariants.

**Tech stack:** React, TypeScript, Zustand, Vitest, Playwright; Python, Pydantic, SQLAlchemy, SQLite and PostgreSQL.

**Status:** Implemented and merged locally into `release/0.8.1` at `9da7ffe340342b3d572f26abf1964c9c2f5b489e`. See the adjacent implementation note for validation and release boundaries. No shared database reset or deployment performed.

**Validation scope amendment (operator, 2026-09-20):** Run only the relevant unit and integration tests. This supersedes the broad-suite instructions below. The broad Python gate was stopped before pytest; focused preferences, tutorial, schema, and PostgreSQL race checks are the completion evidence.

**Source baseline:** `release/0.8.1`, `5acc3eda74632fe7dda730cf0d4e37e9d7343af0`, inspected 2026-09-20. This is the implementation base assumed by this plan, not permission to merge or deploy. Reconfirm the target branch and release before execution; do not silently switch the shared checkout.

**Prerequisites:** Read `AGENTS.md` and all of `CONTRIBUTING.md` under “Whole-tree gates and conventions you will hit.” Use a dedicated implementation worktree and the repository toolchain. Docker is required for PostgreSQL validation. Keep both worktree source roots on `PYTHONPATH` and verify import provenance before Python tests. Browser acceptance needs an isolated test deployment and test accounts.

## Product contract

| Trigger | Expected result |
| --- | --- |
| Account with no preferences row | API returns Freeform without inserting a row or inventing `updated_at`. |
| First unrelated preferences write | Newly inserted row uses Freeform. |
| New session | Uses the explicitly saved mode, defaulting to Freeform. |
| First visit / tutorial retake | Tutorial still appears and uses the ordinary Guided authoring flow. |
| Tutorial completion | Open the built tutorial pipeline in Freeform, preserving its history, outputs, and audit links. |
| Tutorial skip | Persist the opt-out on the initial skip click; the farewell action opens a fresh Freeform session. Closing the tab between clicks must not restart the tutorial. |
| Early tutorial exit | Switch the tutorial session to Freeform with available context; if no session exists, create a usable Freeform session. Preserve run teardown. |
| Reload after successful departure | Tutorial stays dismissed; the exited session restores Freeform; new sessions use Freeform. |
| Explicit Guided selection | Deliberately accessible through a secondary session action; preference selection can make future sessions Guided. |
| Existing Guided work | Still resumes correctly unless the user explicitly exits it. |

Leaving a tutorial sets the account default to Freeform, including on a retake by a previously Guided-default account. Outside tutorial departure, do not rewrite saved mode choices. Preservation of existing preferences refers to supported current-schema data; it is not a promise to migrate disposable pre-release databases across the schema change.

## No-tech-debt constraints

- Deliver defaults, tutorial transitions, discoverability, tests, and associated cleanup together. Do not ship a default-only patch and defer broken tutorial landing or obsolete UI.
- Remove obsolete banner components, API fields, persistence columns, store actions, storage-event branches, telemetry attributes, fixtures, and comments in the same package. Do not hide dead code behind flags or retain deprecated wire fields for old clients.
- No migrations, startup repair, dual schemas, old-default fallbacks, compatibility aliases, or temporary feature flags. Recreate disposable databases at the schema boundary under the existing pre-release policy.
- Shared database deletion requires explicit operator authorization naming the deployment and affected data. The implementation and validation must be reviewable before requesting that approval. Never reset a shared database as part of tests.
- Preserve the independent Freeform introduction and its dismissal state; it is not the obsolete default-change banner.
- No lint suppressions, forged judge signatures, planner bypasses, or tutorial-specific backend pipeline construction. No signed plan packages or approval sidecars.

## Source findings that drive the work

- `src/elspeth/web/preferences/service.py`: `_DEFAULT_MODE = "guided"` supplies no-row GET, empty PATCH, and omitted-mode insertion defaults.
- `src/elspeth/web/sessions/models.py`: `user_preferences.default_composer_mode` has `server_default="guided"`; `SESSION_SCHEMA_EPOCH` is currently 60.
- `src/elspeth/web/frontend/src/components/tutorial/TutorialTurn7Graduation.tsx`: `onFinish` calls `saveTutorialMode("guided")`, then reselects the built session or creates a new one.
- `src/elspeth/web/frontend/src/components/tutorial/HelloWorldTutorial.tsx`: early exit starts `exitToFreeform()` and completion persistence independently. The completion write does not save Freeform.
- `src/elspeth/web/frontend/src/stores/sessionStore.ts`: selection restores persisted Guided state regardless of the account default; `exitToFreeform()` can resolve to `not_applied`, including while an operation is pending.
- `src/elspeth/web/frontend/src/stores/preferencesStore.ts`: `saveTutorialMode` can silently return while another write is active; graduation has a separate bounded wait and publication mechanism. Consolidate this tutorial write path rather than layering another caller around it.
- `src/elspeth/web/frontend/src/App.tsx`: any preferences `writeError` suppresses the tutorial. A failed departure must not accidentally publish a successful-looking landing through this gate.
- `src/elspeth/web/frontend/src/components/common/DefaultModeChangedBanner.tsx`: Freeform plus an undismissed banner is enough to show an unsolicited mode-change notice. That condition would include new accounts after this change.
- `src/elspeth/web/preferences/service.py`: tutorial telemetry partly infers completion category from whether `default_mode` is present. Adding mode to a skip PATCH would otherwise misclassify it.
- `src/elspeth/core/schema_shape.py`: schema comparison checks server defaults; changing only model metadata is not a supported upgrade for an existing database.

## Task 1 — Change defaults and remove the obsolete banner contract

**Modify:**

- `src/elspeth/web/preferences/models.py`, `service.py`, `routes.py` where affected by the removed field or telemetry.
- `src/elspeth/web/sessions/models.py` and `schema.py`.
- `src/elspeth/web/frontend/src/stores/preferencesStore.ts`.
- `src/elspeth/web/frontend/src/api/preferencesDecoder.ts` and `src/types/api.ts`; regenerate any affected generated API declarations with the repository's generator.
- `src/elspeth/web/frontend/src/App.tsx`.

**Delete:** `src/elspeth/web/frontend/src/components/common/DefaultModeChangedBanner.tsx` and its dedicated test file. Remove banner-only style rules only after checking their consumers.

**Tests:** `tests/unit/web/preferences/test_service.py`, `test_schema.py`, `test_models.py`; `tests/integration/web/test_preferences_routes.py`; `tests/unit/web/composer/test_preferences_decoder_parity.py`; frontend preferences store, client, and decoder tests.

1. Add/update behaviour tests for no-row GET, empty PATCH without insertion, unrelated first PATCH, explicit Guided choice, and preservation of existing current-schema preferences. Run them red before implementation; the expected failure is Guided where Freeform is required.
2. Change `_DEFAULT_MODE` and the database column's `server_default` to `"freeform"`. Keep both permitted enum values and strict corruption checks.
3. Delete `banner_dismissed_at` from the table, Pydantic request/response models, select/upsert/returning paths, decoder key set, TypeScript payloads, and all fixtures. Delete `bannerDismissedAt`, `optedOutAtSessionId`, `dismissDefaultChangedBanner`, banner-only localStorage synchronization, and now-unused action parameters. Retain unrelated cross-tab synchronization.
4. Remove the banner import/render and obsolete telemetry attribute. Keep actual preferences failures visible through the preferences/error surfaces; banner removal must not discard their only presentation path.
5. Bump the session schema epoch once for this package and update its history and the coordination epoch pin. At execution, choose the next epoch from the actual branch, not blindly 61. Update all live epoch consumers, schema tests, deployment acceptance expectations, and affected operational documentation together. Do not bump Landscape unless its own schema actually changes.
6. Run focused tests green. Test old-schema rejection and new-schema creation on SQLite and PostgreSQL. Do not merely assert a string in SQLAlchemy metadata: inspect a created database and exercise insertion without the mode field.

**Done:** Fresh/default preferences and real database defaults agree; obsolete banner contract is removed end to end; strict decoder parity passes; obsolete schemas fail with the normal recreation instruction.

## Task 2 — Coordinate tutorial departure and authoritative session handoff

**Modify:**

- `src/elspeth/web/frontend/src/components/tutorial/HelloWorldTutorial.tsx`.
- `src/elspeth/web/frontend/src/components/tutorial/TutorialTurn7Graduation.tsx`.
- `src/elspeth/web/frontend/src/stores/preferencesStore.ts` and `sessionStore.ts` where shared transition semantics require it.
- `src/elspeth/web/frontend/src/App.tsx` for explicit departure/error gating.
- `src/elspeth/web/preferences/models.py`, `service.py`, and frontend `src/types/api.ts` for completion intent.

**Tests:** `HelloWorldTutorial.test.tsx`, `TutorialTurn7Graduation.test.tsx`, `TutorialTurn4Run.cancel.test.tsx`, `preferencesStore.test.ts`, `sessionStore.guided.test.ts`; backend preference models/service/routes telemetry tests.

1. Write regressions for completed, skipped, cancelled-run graduation, and early exit at welcome, startup, active Guided, run, audit, and graduation. Assert active session identity, persisted preference, persisted Guided terminal, and rendered Freeform surface—not only a mock call count.
2. Make one tutorial-completion preferences operation persist `default_mode="freeform"` and completion together. Update local mode from the returned payload. Remove `saveTutorialMode` once callers are converted. Serialize departure with in-flight preference writes rather than silently dropping it; duplicate clicks share/observe the same operation.
3. Make completion intent explicit (`complete`, `skip`, `exit`) in the request contract and all callers. Replace payload-shape inference for non-null completion writes. Reject missing/invalid intent for those writes after updating all clients and tests; no legacy inference fallback. Preserve reset/retake semantics and accurate repeat telemetry. This is operational telemetry, not a new Landscape audit event.
4. Centralize the departure orchestration used by the tutorial shell and graduation card in a small tutorial-local helper if needed. Its inputs must name the tutorial session and departure intent; no graph synthesis or backend tutorial special case.
5. For completion: finish the existing rename/list/select steps, verify the expected session is active, exit its active or completed Guided state through the existing authoritative action, persist completion plus Freeform, then publish the tutorial dismissal. Preserve the built pipeline. Do not satisfy the requirement by landing on an empty session.
6. For skip: persist completion plus Freeform on the first skip click while retaining the farewell card. The final action creates a Freeform session and then publishes dismissal. Retries must not double-count completion or create duplicate sessions. No built tutorial session should be discarded by mistakenly classifying it as a skip.
7. For early exit: retain startup-exit intent handling and in-flight run abandonment/cancellation. Resolve or explicitly report a pending handoff; verify `status === "applied"` or an already-authoritative exited state before calling the departure successful. Preserve session identity across awaits; a response for the tutorial must not mutate a subsequently selected session.
8. Keep failure states actionable with retry and visible error. Do not let generic `writeError` unmount the tutorial into a still-Guided session while presenting that as success. Distinguish bootstrap failure from departure persistence failure. Do not invent completed timestamps, clear Guided state locally as a substitute for durable transition, or spin/retry indefinitely.
9. Cover partial success: authoritative exit followed by failed preferences save, successful save followed by interrupted UI publication, reload between steps, duplicate exit callbacks, and a concurrent preferences write. Reuse existing authoritative state on retry. A closed tab may interrupt a multi-endpoint sequence; on return the app must recover honestly, not claim cross-endpoint atomicity.

**Done:** Every successful departure saves Freeform and opens the intended session in Freeform. Failure remains diagnosable/retryable; pending replies cannot restore abandoned Guided UI; existing run teardown and reload recovery still pass.

## Task 3 — Move Guided behind deliberate discovery

**Modify:** `src/elspeth/web/frontend/src/components/chat/ChatPanel.tsx`, `chat.css`, `guided/ModeSwitchButton.tsx`; `src/components/settings/ComposerPreferencesPanel.tsx`; `src/components/common/CommandPalette.tsx` if needed to keep command discovery consistent; tutorial `copy.ts` and affected session-store error copy. All these frontend-relative paths are under `src/elspeth/web/frontend/`.

**Tests:** Corresponding component tests, `src/components/common/commandRegister.test.tsx` if present at implementation, preferences panel tests, and browser preferences/workspace flows.

1. Write tests showing no always-visible Guided CTA on the Freeform surface; the action becomes available only after opening a secondary menu. Test keyboard access, Escape, focus return, mobile width, and disabled reasons.
2. ~~Replace the Freeform header's direct Guided button with a clearly named secondary “Composer options” menu containing “Switch to guided.”~~ **Not shipped; superseded by ruling D1 (John, 2026-09-20,** `2026-09-20-composer-ux-review-remediation.md`**):** the Freeform header carries no in-session switch to Guided. Guided is reached through Preferences, for new sessions; the in-session switch is to be revisited after the soft launch. `ModeSwitchButton`'s `target="guided"` arm is kept, unmounted, for that revisit.
3. Keep “Exit to freeform” visible inside Guided. Preserve confirmation for work-bearing transitions and the distinction between fresh Guided conversion and resuming saved Guided work.
4. Put Freeform first in account preferences; label Guided neutrally. Focus the selected radio on modal open rather than hardcoding Guided. Account preference selection affects new sessions, not the currently edited pipeline.
5. Update header references in error/help copy, tutorial graduation copy, and command palette descriptions. Remove obsolete Guided-recommendation claims. Keep the existing Freeform introduction useful and avoid adding a replacement unsolicited Guided promotion.

**Done:** Guided is accessible by deliberate discovery; its existing safeguards work; normal Freeform use carries no Guided recommendation or default-change banner.

## Task 4 — Update browser fixtures and prove complete user journeys

**Modify:** `src/elspeth/web/frontend/tests/e2e/composer-preferences.spec.ts`, `tutorial.spec.ts`, `setup/global-setup.ts`, and `helpers/api.ts`; affected Guided-specific fixtures such as `guided-collector.spec.ts`. Add cases to existing suites instead of introducing another harness.

1. Update default-account fixtures to Freeform. Guided-specific scenarios must opt into Guided explicitly; do not globally substitute every occurrence of `guided`.
2. Remove obsolete banner fields from API mocks. Make mock PATCH responses body-aware for completion intent, default mode, and progress clearing.
3. Exercise fresh account → tutorial complete → same pipeline in Freeform → reload → new Freeform session.
4. Exercise skip and early exit, including an active operation, failed save, retry, and reopening the same session. Confirm no extra “Open freeform editor” click is required.
5. Exercise intentional Guided discovery, confirmation, continued Guided session restore, and return to Freeform. Check narrow viewport and keyboard navigation.
6. Use existing tutorial reliability harness procedures for provider-backed acceptance. Record exact account/deployment/revision and separate mocked browser proof from real provider-backed results. Never run acceptance against shared accounts whose preferences would be changed without authorization.

**Done:** Browser flows prove both rendered state and server persistence; fixtures no longer manufacture the old default or accept removed fields.

## Validation commands and evidence

Run commands from the implementation worktree, with `ELSPETH_TASK_ROOT` set to its absolute root and `ELSPETH_TASK_LOG_DIR` set to a unique lane-private directory. Every invocation starts with an explicit `cd`; capture logs and exit codes, never infer success from filtered output. The commands below are for execution, not claims that tests have run.

Focused Python gate:

```bash
cd "$ELSPETH_TASK_ROOT" && PYTHONPATH="$ELSPETH_TASK_ROOT/src:$ELSPETH_TASK_ROOT/elspeth-lints/src" .venv/bin/python -m pytest -n 0 -o pythonpath="$ELSPETH_TASK_ROOT/src $ELSPETH_TASK_ROOT/elspeth-lints/src" tests/unit/web/preferences tests/integration/web/test_preferences_routes.py tests/unit/web/composer/test_preferences_decoder_parity.py > "$ELSPETH_TASK_LOG_DIR/preferences.log" 2>&1
result=$?
printf 'exit=%s\n' "$result"
```

Expected after implementation: exit 0. Before implementation, the new-default and handoff regressions must fail for the intended behavioural reason. Add the affected schema and telemetry suites discovered through live consumers; include PostgreSQL default insertion and stale-schema rejection proof.

Frontend validation (run each separately, with its own log/exit-code capture as above):

```bash
cd "$ELSPETH_TASK_ROOT/src/elspeth/web/frontend" && npm test
cd "$ELSPETH_TASK_ROOT/src/elspeth/web/frontend" && npm run typecheck
cd "$ELSPETH_TASK_ROOT/src/elspeth/web/frontend" && npm run lint
cd "$ELSPETH_TASK_ROOT/src/elspeth/web/frontend" && npm run lint:css
cd "$ELSPETH_TASK_ROOT/src/elspeth/web/frontend" && npm run build
cd "$ELSPETH_TASK_ROOT/src/elspeth/web/frontend" && npm run test:e2e -- tests/e2e/composer-preferences.spec.ts tests/e2e/tutorial.spec.ts
```

Expected: all exit 0 against the configured isolated test environment. Run focused Vitest file selections while developing; the complete frontend run catches strict fixture and contract fallout.

Before merge, freeze the candidate and run the canonical gate:

```bash
cd "$ELSPETH_TASK_ROOT" && scripts/full-suite-gate.sh --execute --detach --root "$ELSPETH_TASK_ROOT" --log-dir "$ELSPETH_TASK_LOG_DIR/full-gate" --stages ruff,mypy,contracts,lints,pytest,testcontainer
```

Poll the printed `.done` path; inspect `summary.txt` and require frozen-tree evidence. Full default pytest and serial testcontainer selections are mandatory for this schema/persistence change. Compare the known fail-closed judge lint corpus against the exact base; do not globally clear, restage, or sign it. Any new drift remains a package signing obligation, not permission to weaken the gate.

Check removed identifiers with controlled searches (positive control in the baseline and a known absent negative control); inspect semantic consumers before declaring cleanup complete. Regenerate affected contracts and fix whole-tree gates honestly. Before each implementation commit, inspect the staged paths and run `scripts/branch-safety-check.sh --intent commit`; stage only owned paths.

## Release and completion

1. Review the complete implementation and validation evidence together. No partial release with the tutorial still saving Guided or with obsolete banner contracts retained.
2. Document the exact new session schema epoch and affected deployment stores. Recreate only disposable stores that actually require it; do not reset Landscape merely because the session schema changed.
3. Request operator approval before destructive shared-state recreation, identifying loss of accounts/preferences/sessions or other affected data according to the actual store. No automatic migration or hidden reset on startup.
4. Deploy matched frontend/backend artifacts so strict preferences decoders never meet an old payload contract. Revalidate a fresh account and tutorial departure on the target deployment after the approved reset/deployment.
5. Report local implementation, tests, merge, operator signing, reset, and deployment as distinct measured states. This plan does not authorize those later shared-state actions.

The work is complete when the product-contract table is proven, obsolete implementation is removed, required checks are accounted for, and no functional part of this request is deferred as technical debt. A blocked deployment must be reported as blocked; local test success is not live acceptance.
