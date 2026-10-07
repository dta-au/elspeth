<!-- Durable copy: machine-specific prefixes normalized; private original retained. -->

# Actual Astra app failed-physical review 2

**Production narrow source GO is preserved; successor-2 control package is NO-GO.** Production bytes are unchanged from reviewed successor 1 and the exact immutable registry dependency is now available. Three control defects remain: the partial baseline reclassifies its expected callback error as unexpected cleanup failure; parent process cleanup can skip wait and mask the causal original; and successful returned-service cleanup can stop its physical-marker observer before publication.

Evidence: [astra-execute-app-failed-physical-code-review-2-evidence.json](astra-execute-app-failed-physical-code-review-2-evidence.json). All prior reports and frozen packages remain intact. No project import, child process, runtime test, collection, SQL, provider, network or repository write was performed.

The complete report, ten-file manifest, production patch/preimages/replacements, prior test preimage, both child modules, parent test and static guard were read. Production app replacement remains `08ee4e26a0d1d2713e7ba7ff2b3580133ac8372b9796597148a52a7b5445aedf`; the exact registry and consumer dependencies remain `49cc0c…` and `566a107…`, as fully recorded in evidence. All manifest hashes matched and all Python files parsed. The whole app replacement equals the previously reviewed source; no unrelated production behavior was introduced.

## A1 — expected partial-owner failure makes the baseline exit nonzero

Blocking defect in `app_failed_partial_child.py:145–153,170–171`.

The separately scheduled `existing_owner` invokes the actual finalizer bridge. The deliberately injected callback fault must make that Task raise the exact `callback_original`. The main assertion path correctly expects startup to retain constructor and callback originals and verifies one actual private executor join. It then prints `ACTUAL_PARTIAL_NO_REPLAY_CONTROL_PASS`.

Finally retrieves each existing-owner Task result and unconditionally appends every raised exception to `cleanup_failures`. On the intended path that is the expected callback original. With no primary test failure, the function then raises `BaseExceptionGroup("Partial control cleanup retained originals", cleanup_failures)`. Thus the child exits nonzero after its PASS marker, while the parent requires exit 0. This is a source-derived baseline failure, not an executed result.

A successor must explicitly observe and verify the expected owner failure by identity, then distinguish it from unexpected cleanup failures. Do not suppress arbitrary exceptions or drop the original; assert the expected owner outcome and retain unexpected originals. Publish final success only after all expected owner outcomes and cleanup have been verified.

## A2 — parent kill/join can abandon wait and replace causal failure

Blocking custody defect in both parent tests' finally blocks in `test_app_failed_physical_exit.py`.

Each checks `child.poll()`, conditionally calls `os.killpg`, then calls `child.wait`. If the child exits between poll and kill, `ProcessLookupError` escapes before wait. Other kill errors likewise skip wait. The saved `primary` failure is not combined with the cleanup error, so the intended causal assertion can be replaced. The static count of two `child.wait(timeout=5)` calls cannot establish that either call executes on every cleanup path.

The exact owned child must be waited even when signaling races its exit, and any unexpected signal/wait failure must retain the original causal failure. Keep the owned process-group boundary; do not broaden killing or treat an unjoined child as a pass. Add controlled exit-between-poll-and-kill and wait-failure checks for the cleanup instrument before claiming this custody guarantee.

## A3 — required physical marker can be lost on a healthy fast path

Control reliability defect in `app_failed_physical_child.py`, `observe_physical_join` and finally line 210.

Only the independent monitor thread emits `ACTUAL_EXECUTOR_PHYSICAL_JOIN_OBSERVED`. It polls every 5ms. The main coroutine can finish all actual physical/telemetry assertions and enter finally before that thread next runs; finally immediately sets `monitor_stop`. The monitor may then exit without publishing its otherwise true physical observation. Parent baseline requires `physical_seen`, so valid behavior can fail nondeterministically.

Require acknowledged publication of the actual receipt before stopping the observer, or produce an equivalent exact receipt on the completed baseline path with once-only publication. Preserve the independent thread path for the outer-loop busy-spin mutant, where the event-loop coroutine cannot publish. A sleep chosen to make the thread likely to run is not a causal fix. Observe actual monitor termination as well as requesting it.

## Improvements accepted in source

T1's event-loop blocking entry wait is fixed: `_wait_for_thread_event` yields while requiring actual private/shared worker entry under a bounded deadline. The returned child forwards the actual completion callback and checks exact finalizer reservation/generation identity. It retains the held shared sibling until after three actual cancellations have been delivered.

T3's cancellation identity gap is fixed for the returned-service path: forwarding `asyncio.sleep` catches actual CancelledError objects on the exact teardown Task; final exception leaves are compared by identity against all three originals, not merely marker args. Callback identity, actual telemetry witness owner, false executor success, registry COMPLETE refusal and false watchdog completion remain asserted.

The new parent provides a useful independent causal boundary for the outer-loop-only busy-spin mutant. It requires a physical receipt measured from the exact private Future, registering generation join and registry getter before the specific missing-membership assertion can count. Missing physical evidence or generic process timeout is rejected. A specific child missing-membership marker is emitted before cleanup. Expected-red mode returns successfully only after recognizing the named causal failure; root must record that protocol rather than mislabel this mode as a native pytest failure. A2 still prevents accepting its complete cleanup guarantee.

The partial child actually constructs/registers the service and private executor before throwing, schedules a separate actual finalizer owner, observes its claim before app's partial branch and asserts one private join. It covers the previously unexercised claimed partial-constructor predicate in principle; A1 blocks baseline acceptance. Its cancellation-retaining wait_for is not independently finite, but the parent process boundary is intended to bound it after actual physical evidence. A2 must be fixed before relying on that boundary.

SQLite membership's real method return is still only NO_MEMBERSHIP. Quarantine remains allowed to refuse fresh shared cleanup admission. None of these source controls demonstrates new post-failure EXECUTE release SQL or PostgreSQL membership persistence.

## Verification and hold

Independent byte/AST evidence verifies all manifest guards and production identity. Isolated AST-selected stdlib source guards accepted the candidate and rejected their four advertised source mutations. Those guards still accept A1–A3, illustrating their scope: string presence and call counts do not establish exception chronology, guaranteed process join or receipt publication. No guard main or child/test code was executed.

Prepare a separately frozen control successor addressing A1–A3; preserve successor 2 unchanged. Re-review then run actual baselines and each correctly scoped intended mutant with exact process custody. The constructor fixture, canonical authority/Future/callback migration, fresh shared-source composition and combined gates remain separate. Original 102 obligations, four collections and two UNKNOWNs remain held; no merge/deployment or external Daybreak claim is made.
