# R50. The saga settlement in `finally` raises `SessionDerivedCustodyError` when the failed status never landed, masking the original exception

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Execution and run controls |
| Review line | backend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | be-18#3 |

## Finding

- **Location:** `src/elspeth/web/execution/service.py:4306-4316`, `:4340-4355` and `coordination/repository.py:1725-1740`.
- **Wrong:** `terminal_event_persisted` is set when the failed status write was swallowed, and on the signal path. The settlement UPDATE then matches 0 rows and raises out of `finally`. `_on_pipeline_done` logs a custody-integrity class, and a `SystemExit` is no longer filtered.
- **Fix:** Set the flag only when the failed status is durable, correct the comment at `:4341-4348`, and add a test for the degraded-status path.
- **Sources:** be-18#3.
- **Verifier notes:** Recovery is not affected.


## Source findings and verification

### be-18#3: Saga settlement in finally raises SessionDerivedCustodyError when the failed status never landed, masking the original exception

- **Reported at:** `src/elspeth/web/execution/service.py:4316`; reviewer severity low; category error-handling; diff-anchored True.
- **Summary:** The BaseException handler sets terminal_event_persisted=True whenever the failed event is persisted, including when _persist_failed_run_status swallowed an SQLAlchemyError or OSError (status still running) and on the signal path, where the status update is skipped. With outputs_finalized True, `finally` calls mark_recovery_outputs_finalized. Its UPDATE filters on terminal status, matches 0 rows and raises SessionDerivedCustodyError out of `finally`, replacing the re-raise of the original exception. The comment at :4347-4348 presents the mutation's status recheck as a gate, but it raises.
- **Failure scenario:** A pipeline crashes. The failed-status write hits a transient SQLite 'database is locked' error and the next event write succeeds. The Future's exception becomes SessionDerivedCustodyError ('session-scoped derived record is unavailable'), and _on_pipeline_done logs a custody-integrity class in exactly the case its docstring calls 'the ONLY place the failure surfaces'. The original class appears only later in exc_class_chain via __context__. A SystemExit raised inside the worker is likewise converted, so the callback's signal filter no longer applies. Recovery is unaffected.
- **Evidence:** service.py:4306-4316, :4340-4355, :4419-4455 (the degraded return), :4284-4288 (skip on signal), :4711-4714, :4757; repository.py:1725-1740 raises when rowcount != 1; repository.py:5254-5283 (mutate does not wrap). No test in test_service.py:1016-1128 reaches the settlement with status still running.
- **Suggested fix:** Set the settle flag in the BaseException handler only when the failed status is durable (not a signal, status_update_exc_class is None, no IRTE), or track a separate terminal_status_persisted flag. Correct the :4347 comment. Add a test for the degraded-status path asserting the original exception propagates and no settlement call is made.
- **Verifier (trace):** upheld, confidence high, severity low. I traced the path at 74c0ce0db and could not refute it. Commit 6e377bdc4, inside the review window, added the settlement in `finally` and the `terminal_event_persisted = True` in the BaseException handler. `_persist_failed_run_status` catches SQLAlchemyError/OSError from `update_run_status` and returns only the class name, so the run row stays 'running'. The signal path skips the status update entirely. In both cases `run_already_terminal` stays False, so the failed event is still appended. `append_run_event` has no terminal-status precondition, so that append can succeed, which sets `terminal_event_persisted=True`. `_finalize_output_blobs` returns True when there is no blob service or every blob settled. `finally` then calls `mark_recovery_outputs_finalized`. Its UPDATE filters on terminal status, so it matches 0 rows and raises `SessionDerivedCustodyError`. `mutate` only closes the transaction facade and does not catch the error. The custody error leaves `finally` and replaces the pending `raise`, leaving the original exception only on `__context__`. `_on_pipeline_done` then logs `SessionDerivedCustodyError` as the top-level `exc_type`. For a SystemExit, the `isinstance(exc, (KeyboardInterrupt, SystemExit))` filter no longer suppresses the log. The comment at :4344-4345 describes the recheck as a gate, but the recheck raises, and the comment at :4341-4343 wrongly implies terminal status is already durable. Recovery is unharmed: the saga stays non-terminal and peers can still find the run. So low severity is right. The impact is a misleading operator diagnostic plus a stale comment.
