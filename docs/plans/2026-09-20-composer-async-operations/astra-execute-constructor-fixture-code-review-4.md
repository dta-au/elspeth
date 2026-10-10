<!-- Durable copy: machine-specific prefixes normalized; private original retained. -->

# Actual Astra constructor fixture review 4

**Verdict: NO-GO for the frozen constructor-only package.** Two helper defects prevent even bounded source approval: finalizer shutdown starts before lifecycle draining, and Task allocation failure can abandon cleanup custody. The constructor argument migration and preservation checks are acceptable as source transformations, but are not independently usable without a corrected helper. No candidate was applied or imported and no runtime, collection, SQL, provider, network or external-review action was performed.

Reviewed package: `frontend-execute-constructor-fixture-migration-1/constructor-only-package-4/report.md`, complete patch SHA-256 `d12ae467f408a0a1c31f49efdb078d7a737a8bbae085d8692910596e45c88e98`. Companion evidence: [astra-execute-constructor-fixture-code-review-4-evidence.json](astra-execute-constructor-fixture-code-review-4-evidence.json). Held worktree HEAD re-read as `d2b73990d137e9725200c897544eebc5b958fdf4`.

## F1 — shutdown enters the finalizer bridge before marking the lifecycle draining

Blocking source defect in new `tests/helpers/execution_custody.py:78–82,84–117,139–145`.

`shutdown_service()` seals the owner and directly awaits `service.shutdown()`. `close()` seals owner/registries and schedules returned-service shutdown and partial-constructor finalizers. Neither establishes draining first. Only after those joins and registry completion checks does `close()` call `self.recovery.begin_shutdown()`, and only for a standalone owner.

The selected core4 `async_workers.run_application_finalizer_in_worker` starts at line 318 and rejects when `_INSTANCE_DRAINING` is not set (lines 320–321). Consumer3 `_finish_executor_join` invokes that bridge at service line 1661; service shutdown does not establish draining itself. Actual `ProcessRecovery.begin_shutdown()` delegates to `_begin`, which synchronously sets its exact `instance_draining` Event. Consequently a normal standalone fixture with its initially clear Event takes the integrity-error path before the finalizer executes. This is a source-derived failure path, not a claimed measured test failure.

The borrowed child lifecycle has the same ordering problem: `ChildExecutorLifecycle.close()` sets draining only after the target returns. The target now awaits `execution_fixture.shutdown_service()` or `execution_fixture.close()` before returning, so the outer child scope cannot establish the prerequisite in time. A completed real `os._exit` never reaches these finally blocks and remains a genuine death seam; that does not repair healthy child return.

A successor must coordinate the actual selected recovery owner's draining transition before any finalizer bridge, without replacing the owner, resetting unknown custody or manufacturing completion. It must preserve borrowed-owner responsibility and ensure a failure establishing recovery does not skip already-owned cleanup.

Required causal controls: real standalone healthy construction and shutdown; real borrowed child owner healthy return; actual prebound executor capability after constructor failure; exact existing selected owner retained. Each must observe the actual shutdown callable and physical callback receipts, all admissions/registries settled, and no unexpected finalizer-integrity error. A deliberate late-drain mutant must fail the intended assertion. Merely checking Event state or mocking service.shutdown is insufficient.

## F2 — allocation failure escapes before independent joins acquire custody

Blocking source defect in new helper `close.issue`, lines 103–117.

`issue()` constructs `observe(awaitable)` and calls `asyncio.create_task` without a predeclared producer owner or allocation exception handling. The Task is appended only after the call returns. A failure before allocation immediately exits `close`, skipping later service/registry joins, registry completion checks and lifecycle shutdown. A seam that allocates the Task and then raises additionally leaves the actual Task outside `tasks`; `observe` records only its eventual outcome and cannot recover ownership during entry. No outer handler continues independent cleanup or joins that hidden Task.

The failure also bypasses the helper's explicit original-outcome aggregation. Previously issued siblings can remain running after close raises. A pending Unknown sibling must retain custody, but cannot justify losing known independent siblings or replacing the original allocation error. This is precisely the allocation boundary already treated explicitly in the production app/consumer work; the new helper cannot claim that guarantee through a bare create_task call.

Required successor controls: controlled allocation failure before creation and after actual creation, an already-issued held sibling, and a later independent sibling. Prove actual later close entry, exact hidden Task custody/join when allocated, retention of original allocation failure and all outer cancellations, and refusal of completion while any physical owner remains Unknown. A real finite owned process must bound intentionally pending cases and be joined; a timeout alone is not the intended red oracle. Do not use cancellation-retaining wait_for as a bound without proving it can terminate.

## Accepted source properties and limits

The complete patch was read; all guarded preimages and replacements were fully parsed and byte-checked, with focused semantic inspection of affected constructor/fixture and spawned-child paths and the full new helper. This is not a claim that unrelated assertions in the large existing test modules were independently re-audited. Relevant core4, consumer3 and app5 lifecycle source had been reviewed in the preceding reports; the actual core finalizer precondition, consumer shutdown call, ProcessRecovery, test watchdog, child lifecycle, autouse lifecycle fixture and parity fixture were inspected for this integration.

The helper creates actual nominal `ApplicationFinalizerOwner`, `ProcessRecovery` and `ExecutionLeaseReleaseRegistry` objects. It uses the existing nominal `OwnedTestProcessWatchdog`; it does not add an optional production shim or manufacture a public finalizer capability. It refuses installation after an existing generation/admission, and its borrowed-owner branch checks the exact bound recovery method and nominal owner. The service binds the registry before its actual executor finalizer is used. Partial-constructor cleanup selects the registry's actual prebound capability instead of constructing one from a DTO.

For successful Task allocation, cleanup schedules independent joins before waiting; it stores the producer's original exception before Task.result observation, retains caught caller cancellations, and checks real `registry.assert_completed`. These useful properties do not cover F1 or F2. Unknown work remains pending; the no-signal watchdog does not make this helper independently finite or prove physical supervision. No native watchdog/process clearance follows.

ApplicationFinalizerOwner permits one capability per kind. The helper's service/registry lists do not create authority for multiple EXECUTION_EXECUTOR_JOIN registrations. The inspected parity fixture uses a bare FastAPI rather than a started production lifespan, so duplicate registration there is not an additional demonstrated selected-path defect. Any claimed multi-service support or borrowing from an already populated/sealed app owner needs an explicit control and scope; do not widen production authority to accommodate a test helper.

The module-level alias exposes the actual pytest fixture. The `test_service` live-lease fixture depends on it, establishing fixture setup ordering before that lease's generation use. Existing recorded authorities, adopted leases, fake executors/Futures and old watcher/callback fixtures are deliberately not converted by this package. The retained original normal-shutdown cancellation golden has not been approved for replacement. These canonical fixtures remain unfinished and unapplyable from the mutable parent directory.

## Verification evidence

The independent stdlib-only instrument reconstructed the complete patch byte-for-byte. All 13 final preimage/replacement hashes matched the final guards; all current repository preimages matched; the new helper's hash matched and its target path remained absent. The source inventory found 25 direct constructor/factory AST sites across these guarded files. This is not a registry/runtime inventory and not a collected/passed-test count.

Each file passed an explicit registry-keyword positive check, and removing a required keyword was detected. Existing test name/decorator ASTs matched; changing one existing name in every file was detected. Assertion ASTs in source order matched; changing an assertion in every file was detected. All actual `os._exit` call ASTs matched. Test parametrization/decorator preservation does not prove fixture resolution, setup order across the full suite or runtime behavior.

The initial assertion instrument used breadth-first ast.walk order and reported two mismatches caused by existing assertions moving deeper inside new try/finally wrappers. The raw results remain in the evidence. Source-order comparison corrected the instrument and was separately mutation-controlled. A first follow-up invocation used the repository cwd and failed FileNotFoundError before writes; the successful rerun used absolute workspace paths. No failed instrument run was described as a successful gate.

No whole-tree contract, masquerade, trust-tier, Ruff, mypy or runtime clearance is inferred from these AST checks. The package's author-reported static logs remain author evidence; native fixture/helper ABI and whole-tree gates must be measured by root on a coherent frozen composition. Existing test casts and doubles were retained, so preservation alone cannot certify their current contract compatibility.

## Remaining hold

Preserve this immutable package and its NO-GO. Correct F1/F2 in a separately frozen successor, then review the actual controls before root runs them. Canonical authority/Future/callback fixtures, app known-failed-exit integration, consumer integration, fresh shared async_workers/lifecycle composition and the function-only storage overlay remain independent holds. Separate observer5 control successors were not cleared by this constructor review. Earlier core2/observer4 NO-GO reports remain historical custody; narrow core4/observer5 source assessments do not clear this helper.

All original 102 obligations, four collection obligations and two UNKNOWNs remain uncleared by this review. No merge, deployment, production or paid-provider testing occurred. John retains local testing, manual Daybreak and merge decisions; no external Daybreak claim is made.
