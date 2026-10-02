# R13. A user-cancelled run whose output finalization fails is never recovered (pre-existing)

| | |
|---|---|
| Severity | medium |
| Status | confirmed |
| Area | — |
| Review line | backend |
| Pre-existing (touches lines outside the window) | yes |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | be-18#2 |

## Finding

- **Status and severity:** confirmed, **medium**, pre-existing.
- **Location:**
  - `src/elspeth/web/coordination/run_recovery_authority.py:145-161`.
  - `run_cancellation_authority.py:64-65`.
  - `coordination/repository.py:1794-1795`.
  - `execution/service.py:4340-4345`, where the new settlement and its comment are.
- **What is wrong:** A cancel after the start permit writes `saga_state='cancel_pending'`. The worker moves the status to `cancelled`, but `transition_run_status` rewrites `saga_state` only when the new status is `running`. If `_finalize_output_blobs` fails (returns False on suppressed OSError, SQLAlchemy or Blob errors), the worker skips settlement and relies on peer recovery. `list_recoverable_run_records` never admits `cancel_pending`. The window's new comment says unfinished outputs "remain discoverable by peer recovery", which is false for this state.
- **Failure scenario:** The user cancels a running web run and one output blob fails to finalize during the unwind. The row ends `status=cancelled, saga_state=cancel_pending`. Its output blobs stay pending forever: nothing settles them, recovery never lists them, and no sweeper exists. `finalize_run_output_blobs` has only three callers.
- **Suggested fix:** Admit `saga_state='cancel_pending'` in `list_recoverable_run_records`, in both the SQL predicate and the locked recheck. `project_terminal` already maps INTERRUPTED to cancelled. At minimum, correct the `:4341-4345` comment. Add a test: cancel after permit, make finalization fail, and assert the run is recoverable.
- **Sources:** be-18#2.
- **Verifier notes:** A cancel that lands while the status is still `pending` is safe, because the pending-to-running transition overwrites `saga_state`. `cancel_pending` has exactly one writer and no reader anywhere.


## Source findings and verification

### be-18#2: User-cancelled run with failed output finalization is never recovered (cancel_pending invisible to recovery)

- **Reported at:** `src/elspeth/web/coordination/run_recovery_authority.py:147`; reviewer severity medium; category recovery-leak; diff-anchored False.
- **Summary:** A user cancel after the start permit sets saga_state=cancel_pending. The worker then moves status to cancelled, and transition_run_status rewrites saga_state only when the new status is running. If _finalize_output_blobs fails, the worker skips settlement and relies on peer recovery. But list_recoverable_run_records admits only status in (pending, running) or saga_state in (running, admission_refusal_pending), so this row is never a candidate. The window's new comment at service.py:4344-4345 claims unfinished outputs 'remain discoverable by peer recovery'.
- **Failure scenario:** The user cancels a running web run. During the GracefulShutdownError unwind the blob store raises an OSError, or one output blob fails to finalize. The run ends status=cancelled, saga_state=cancel_pending, and its output blobs stay pending permanently: no worker settlement, no recovery candidacy, and no other sweeper. The only callers of finalize_run_output_blobs are service.py:2439, service.py:4632 and recovery.py:175.
- **Evidence:** run_cancellation_authority.py:64-65; repository.py:1794-1795; run_recovery_authority.py:145-161; service.py:4004-4046 (graceful path) and :4340-4345 (the new settlement and its comment). The unrecoverable state predates the window; the window's new settlement design and comment depend on the opposite being true.
- **Suggested fix:** Admit saga_state='cancel_pending' in list_recoverable_run_records, in both the SQL predicate and the locked recheck; project_terminal already maps INTERRUPTED to cancelled and appends the event once. At minimum, correct the :4341-4345 comment. Add a test: cancel after permit, make finalization fail, assert the run is listed as recoverable.
- **Verifier (trace):** upheld, confidence high, severity medium. I could not refute this. I traced every step at 74c0ce0db and the path is reachable as described.

1. The user cancel goes through ExecutionService.cancel (service.py:2858). That calls request_run_cancellation, which uses RepositoryRunCancellationAuthority.request (sessions/service.py:10558-10564). For a run past its permit, request writes saga_state="cancel_pending" and keeps status=running (run_cancellation_authority.py:64-65). It then sets the in-process shutdown event.

2. In the GracefulShutdownError handler (service.py:~4004), _finalize_output_blobs does not raise when finalization fails. OSError, SQLAlchemyError and the Blob* errors are in _FINALIZE_SUPPRESSED (service.py:4610-4616), and on those it returns False (4674-4691). A partial failure also returns False. So control reaches update_run_status(status="cancelled").

3. update_run_status calls transition_run_status (sessions/service.py:10687+). That function sets saga_state only when status=="running" (coordination/repository.py:1794-1795). The row therefore ends as status=cancelled with saga_state=cancel_pending.

4. The finally block settles only when both outputs_finalized and terminal_event_persisted are true (service.py:~4340). Here outputs_finalized is False, so mark_recovery_outputs_finalized is skipped.

5. Neither the SQL predicate nor the locked recheck in list_recoverable_run_records admits this row (run_recovery_authority.py:147 and :160). They require status in {pending, running} or saga_state in {running, admission_refusal_pending}. RunRecoveryService.recover (recovery.py:150) is the only consumer. Its _reconcile_terminal (recovery.py:175) is the only other place that finalizes outputs for a terminal run.

6. The only writer of cancel_pending is run_cancellation_authority.py:64, and no code anywhere reads it or moves a row out of it. The only other references are the enum (contracts.py:87) and the CHECK constraint (models.py:2527). No test covers recovery of a cancel_pending row; the only test hit is test_contracts.py, which just lists the enum. recent-code-hints.md has no recorded ruling on it.

The window added the settlement block and its comment, "unfinished outputs remain discoverable by peer recovery" (the + hunks in the 7c986dc97..74c0ce0db diff, service.py:4340-4345). For a cancel made mid-run, that claim is false.

One qualification narrows the finding without refuting it. If the cancel lands after the permit but while status is still pending, the later pending->running transition overwrites saga_state with "running" (repository.py:1795). That run stays recoverable. So the defect covers only a cancel made while status=running, which is the common case. The same stuck state also follows a normal completion that races a cancel_pending marker and then fails finalization.

Medium severity stands: output blobs stay pending permanently and no sweeper picks them up. The run's status and audit record are still correct.
