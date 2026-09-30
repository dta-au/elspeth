# codex.md (recovered from workflow journal)

Verdict: CHANGES_REQUESTED — NO-GO. Codex (second-model review, focused on T04/T05/T06/T11) verified 7 plan defects against release/0.8.1 @ 53b7d4344bab555342253bef5aa51d0db1487b5a. NOTE: Codex's sandbox filesystem policy prevented it from writing the requested report file at .claude/lanes/async-ops-plan-2026-09-25/review/codex.md — the full findings below are Codex's raw output, not confirmed as persisted to that path. No separate defect was found in T05's composite-start SQL. This was a source-and-plan review with one isolated in-memory scheduling probe, not a full implementation test-suite run.

## C1 [blocker] T06/T11/T10 — cancellation-vs-completion race

**Issue:** Cancellation winning the completion CAS loses the completed provider's audit evidence. T06.md:1607-1642 inserts the audit cohort then rolls it back entirely when cancellation beats the terminal CAS; T11.md:1443-1445,1515-1523 then publishes cancellation via the failure writer without restoring that cohort; T10.md:2371-2390,2445-2463 only handles exception-attached evidence on CancelledError, not the normal-return case.

**Evidence:** src/elspeth/web/sessions/routes/messages.py:994 persists result.llm_calls; src/elspeth/web/sessions/service.py:14802 records token usage in that same audit transaction — both are lost on rollback per the plan as written.

**Fix:** After cancellation rejects completion, preserve and join the returned result's audit evidence before publishing cancellation, including tool references to the rolled-back assistant turn. Add a test for cancellation committed after provider return but before the completion CAS, asserting durable audit/usage accounting.

## C2 [blocker] T04/T10/T11 — committed cancel does not fence composition-state writes

**Issue:** A committed cancel only sets a marker (T04.md:1821-1828); before T11.md:1562-1575 observes it, T10.md:2318-2323 can commit a new composition-state write using the still-valid SessionOperationLease. Rejecting the later completion transaction cannot undo that already-committed state, violating the spec's late-write fencing requirement (design doc lines 234-237).

**Evidence:** src/elspeth/web/sessions/service.py:9992 routes this mutation through the SOL authority; src/elspeth/web/coordination/repository.py:4955 checks lease identity/expiry but has no cancellation predicate.

**Fix:** Check the bound job's cancellation state inside non-audit mutation transactions (while keeping the cancellation-audit path intact). Test: pause the watcher, commit cancellation remotely, then confirm a subsequent state mutation fails.

## C3 [blocker] T11/T04 — claimed jobs blocked on the compose lock ignore Stop/expiry

**Issue:** T11.md:1222-1230 renews the claim while waiting on the local compose lock, but renewal (T04.md:1733-1741) ignores cancellation and deadline, and the reaper excludes live claims (T04.md:1918-1920). Local cancel is only consumed after the running watcher starts, so an unrelated guided turn holding the same session compose-lock registry can delay cancellation past the spec's renewal-interval bound.

**Evidence:** src/elspeth/web/sessions/routes/composer/guided.py:3032 uses the same session compose-lock registry the async worker waits on.

**Fix:** Observe cancellation and expiry while waiting for lock acquisition, not only after acquiring it. Test: hold the shared lock, claim a queued operation, then verify local cancellation, remote cancellation, and expiry all resolve without needing the lock released.

## C4 [blocker] T04/T11/contract.md — archived-session settlement path missing

**Issue:** contract.md:21 (an adopted deviation) requires settle_lost_inactive_session, but T04's authority implementation omits it entirely; T11.md:1659-1670 still just logs and skips OWNER_INACTIVE, so an operation whose owning session is archived runs indefinitely.

**Evidence:** src/elspeth/web/coordination/repository.py:4797 rejects COMPOSE acquisition for an archived session after fence expiry, so the reaper cannot reach any normal settlement path; an AST inventory of T04's methods confirmed settle_lost_inactive_session is absent.

**Fix:** Implement and invoke the already-adopted settle_lost_inactive_session method (with its mutation-authority declarations) per the contract's adopted-deviations table, and add the archived-session/dead-owner test the plan index calls for.

## C5 [blocker] T08/T10/T11 — worker does not enforce the absolute operation deadline

**Issue:** T11.md:1395 computes budget as deadline_at - started_at once, then performs additional awaited setup before passing that unchanged duration to the turn; neither the job frame nor the watcher re-checks the absolute deadline afterward. T08.md:1147 explicitly delegates enforcement of that outer bound to T10/T11, but neither implements it.

**Evidence:** src/elspeth/web/composer/service.py:4161 establishes the compose deadline at compose entry, so any delay between the running transition and that entry silently extends the operation past its admitted deadline.

**Fix:** Enforce a server-relative absolute deadline that spans setup plus turn execution (not just the turn), preserving audit cleanup on expiry. Test: delay setup after the running transition and assert provider work receives only the actually-remaining time.

## C6 [major] T11 — shutdown drain race with in-flight admission scan

**Issue:** Shutdown (T11.md:1093-1106) snapshots and drains jobs before stopping scan tasks, but _claim_and_spawn (T11.md:1194-1195) can resume from its awaited DB call and spawn another job without rechecking _stopping, so a job claimed concurrently with shutdown is missed by the drain.

**Evidence:** src/elspeth/web/app.py:949 subsequently shuts down membership and the shared worker pool while such a job can still be running. Codex reproduced this by executing the planned scheduling methods in-memory: stop_returned=True, new_shutdown_requested=False, new_task_pending=True (i.e. a job was spawned after the stop signal and before the drain snapshot); a control ordering where claim finished before shutdown drained the job correctly.

**Fix:** Quiesce claim/spawn activity before taking the final drain snapshot (e.g. check _stopping again after the await, before spawning), and release any claim returned during shutdown rather than orphaning it.

## C7 [major] T04 — PostgreSQL admission capacity race

**Issue:** Admission (T04.md:1561,1580-1585) does count-then-insert under a session-specific lock, so two different sessions can both observe the last available capacity slot and both insert, exceeding the promised cluster capacity limit. The plan's own soft-limit concession (T04.md:1541-1543) contradicts contract.md deviation D3, and no adopted deviation authorizes that relaxation.

**Evidence:** src/elspeth/web/sessions/locking.py:256 keys the exclusion lock by session ID; src/elspeth/web/sessions/locking.py:279 supplies only an ordinary transaction, with no shared cross-session capacity serialization.

**Fix:** Make capacity reservation atomic across sessions (e.g. a single serialized admission counter/row-level check spanning all sessions) without introducing the previously-prohibited advisory lock. Add a PostgreSQL test with two sessions racing for the final admission slot.
