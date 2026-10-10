# Focused historical worker controls

These controls supply bounded new evidence for H061/H062, H085 and the `run_until_idle` part of H089. They do not close the entire historical obligation rows or establish frozen-tree clearance. All 102 original obligations remain retained, and the missing original reports remain unknown.

The new test module is [test_composer_worker_historical_controls.py](../../../tests/unit/web/sessions/test_composer_worker_historical_controls.py). It runs actual worker/repository paths with file-backed SQLite and a nominal fake process watchdog. It makes no provider calls or real signal requests.

| Control | Observed boundary | Limit |
| --- | --- | --- |
| Archived running row | Actual DB-clock lease expiry, authoritative archived branch, inactive-owner settlement once, immutable terminal replay, zero composer dispatch | Archive status is set directly in the fixture; this does not exercise the archive filesystem workflow. |
| Missing after reaper snapshot | Actual session deletion at lease acquisition produces the missing-session race; the next expired job still settles | SQLite only; no PostgreSQL contention clearance. |
| Nonmissing authority failure | Exact `TOKEN_MISMATCH` object propagates; the running row and zero provider count remain | Controls the missing-only refusal policy; it does not cover every fence discriminator. |
| Shutdown latch | Shared event is set synchronously before watchdog acknowledgement; repeated calls retain one escalation task | Fake available/unavailable watchdog; no actual stopped helper process. |
| Caller cancellation | A held actual worker job settles before the driving task remains cancelled; another idle drive does not replay the job | This proves the historical task-cancellation contract, not preservation of the cancellation message text. |

The completed serial run wrote private raw log `historical-worker-controls-2.log` and exit sidecar `historical-worker-controls-2.exit`. Raw result:

```text
0
============================== 6 passed in 3.80s ===============================
```

The first run is preserved as `historical-worker-controls-1.log` and `.exit`:

```text
1
========================= 1 failed, 5 passed in 4.43s ==========================
```

That failure was an extra test assertion requiring the caller's cancellation message. The retained historical requirement is `drivers[0].cancelled() is True`; the test now checks that exact task state after observing `CancelledError`. No production cancellation behavior changed for this repair.

Repository-context Ruff required import sorting after workspace formatting had classified the test package differently. The original bytes remain preserved. A controlled AST comparison verified the imported symbols and all other statements remained unchanged; its positive control reordered imports and its negative control changed an assertion. The two root-owned proof files then passed Ruff:

```text
0
All checks passed!
```

The production branch remains unfinished and unfrozen. Full default Python selection, serial PostgreSQL selection, whole-tree contracts/pins and final independent reviews remain outstanding. The real pipeline-authoring test has been applied but remains unexecuted while explicit provider custody is under review. John's local-testing hold remains in force.
