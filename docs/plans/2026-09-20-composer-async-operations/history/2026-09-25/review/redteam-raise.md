# Red-team review: raise-path coverage (T00 Appendix A plus tasks)

Plan: docs/plans/2026-09-20-composer-async-operations{.md,/T00..T18.md,/contract.md}
Tree: the main checkout, release/0.8.1. Read-only. Scope: every exit of `send_message` (messages.py:116-1178) and
`recompose` (compose.py:90-829), plus the callees I walked. Question: does each exit land before the 202, or in the
terminal envelope with its current public meaning? Can any job stay nonterminal? Can anything fire after the terminal
CAS and vanish?

## What I walked (the attack record)

- Read both route bodies in full: messages.py:116-1178 and compose.py:86-829.
- Read the lifecycle closure:
  - `_track_compose_inflight`, the heartbeat and the renew policy (_helpers.py:2442-2645).
  - `_cancel_on_client_disconnect` (_helpers.py:2209-2335).
  - `_publish_progress` / `_composer_progress_sink` (_helpers.py:382-411).
  - `SessionOperationLease`: acquire, adopt, create_task, _renew_forever, _record_renewal_error, _close, __aexit__
    (lifecycle.py:261-761, 1068-1091).
  - `_join_shielded_task_after_cancellation` (lifecycle.py:261-268).
  - `maybe_auto_title_session` (_auto_title.py:300-389).
  - progress `claim_request` in both registries (composer/progress.py:106-129; composer_progress_authority.py:533-542).
  - The repository `acquire` conflict and inactive arms (repository.py:4760-4840).
  - The app exception handlers (app.py:1363-2266). There is no ValueError handler.
- Read the moved turn body in T10 (T10.md:1693-2484) and the worker and reaper in T11 (T11.md:742-1701).
- Read T06's composite `complete_` / `fail_` (T06.md:1540-1719) and its risks (T06.md:2185-2216).
- Read T04's `request_cancel`, `settle_unstarted`, `list_expired_*`, `settle_lost` and `count_nonterminal`
  (T04.md:1765-2055), and T13's admission (T13.md:916-975).
- Read the T16 race test (T16.md:1096-1144).
- Traced every Appendix A row against the code that implements it.
- Checked and cleared:
  - W2's http_error settle is admitted by `_UNSTARTED_FAILURE_CODES` (T04.md:1379).
  - W1 REQUEUE is spec-mandated (spec §3 l.164-166).
  - `settle_auto_commit_intent` supplies `commit_timeout_seconds` internally (T10.md:501).
  - A heartbeat or marker cancel racing `complete_` is settled correctly: completed wins (T11.md:1512-1514, T06.md:2185-2191).
  - A finish_request failure that replaces the turn's error has the same parity as today (the dependency's finally does the same).
- Measured request_json expansion with the venv's pydantic (see F4).

## Findings

### F1 (major): an adopted deviation that no task implements leaves `running` rows stuck forever
- contract.md:21 (the Adopted-deviations table overrides every later section) says T04 adds
  `settle_lost_inactive_session(...)` and "the reaper's OWNER_INACTIVE arm calls it (T11)".
- The plan index Review Focus #5 (composer-async-operations.md:55) expects the reaper to settle through it.
- Reality:
  - `grep -n "inactive|OWNER_INACTIVE|archived_at" T04.md` returns nothing, so the method does not exist.
  - T11.md:1659-1670: the OWNER_INACTIVE arm logs `reap_skipped_inactive_session` and returns `SESSION_INACTIVE`.
  - T11.md:2044: "The fix is unresolved and recorded as a residual."
  - T00 Review note 11 flags the same hole.
- Consequences:
  - A `running` row whose owner died and whose session was then archived is never settled.
  - The row counts against D3 capacity forever (T04.md:2048-2055).
  - The row is re-listed every reap interval.
  - Starvation: `list_expired_running` has no cursor. It is `ORDER BY started_at ... LIMIT 32` (T04.md:1953-1954,
    T11.md:874/1147). Once 32 such zombies exist and are older than a newly lost row, every reap sees only the zombies.
    Genuinely lost rows on live sessions are then never reaped. Those sessions stay behind D8's 409
    `composer_operation_active` indefinitely. This consequence is probable: I reasoned it from the plan code and did not
    run it.
- Fix: implement the method in T04 (unfenced, guarded by `sessions.archived_at IS NOT NULL` inside the locked tx). Call it
  from T11's OWNER_INACTIVE arm. Add a reaper test for an archived session. Alternatively, correct contract.md:21 and
  index:55 so they say "residual", and give `list_expired_running` a way to skip rows it has already classified.

### F2 (major): a cancel after compose() returns discards the turn's LLM-call audit cohort (spec §3 and §4)
- The spec requires "the LLM-call audit cohort persisted before terminal publication" (spec l.192-194). It also says the
  worker "settles request_cancelled after its audit join" (l.236-237).
- Today, `_cancel_on_client_disconnect` wraps only `composer.compose()` (messages.py:357-379, compose.py:202-215). A
  disconnect that races completion is absorbed so "its results persist normally" (_helpers.py:2300-2322). A user Stop
  therefore can never drop `result.llm_calls`.
- After cutover:
  - T11 sets `job.phase = "running"` before `_run_turn` and flips to "settling" only after `run_composer_turn`
    returns (T11.md:1405, 1437-1439). `deliver()` fires whenever phase == "running" (T11.md:926). The watcher can
    therefore cancel the turn anywhere in the post-compose tail: auto-commit settlement, state validation and save, the
    auto-title join, and the composite.
  - The cancel arm drains only `_llm_calls_from_exception(exc)` (T10.md:2449). A CancelledError raised in the tail
    carries none, because the calls live on `result.llm_calls`, which is consumed only by the composite.
  - If the cancel instead wins the terminal CAS, `complete_composer_async_operation` rolls back the assistant row and the
    whole cohort ("Every failure, including a lost CAS, raises inside the transaction", T06.md:1560-1562, 1622-1642).
    `_run_turn` maps `ComposerOperationCancelledDuringTurn` straight to request_cancelled with no persist (T11.md:1443-1445).
- The plan pins the loss instead of catching it:
  - T06 `test_cancel_committed_first_raises_cancelled_during_turn_and_writes_nothing` (T06.md:734-766) asserts the LLM
    sidecar is gone.
  - T16 `test_cancel_and_completion_settle_exactly_one_terminal` (T16.md:1130-1135) accepts the "CAS rejected
    completion and rolled back" branch after a real provider call.
  - T11's audit test cancels only inside compose (T11.md:475-517).
- Result: a provider call that was made (and charged) has no audit row. The same happens for lease_lost, shutdown and
  heartbeat cancels in the tail.
- Fix (one option):
  1. On CancelledDuringTurn, or on a CancelledError raised after `_compose_result` is set, persist
     `result.llm_calls` (and the tool rows, if they are to be kept) in their own fenced tx before the request_cancelled
     fail write, as `_persist_llm_calls` does.
  2. Or narrow marker delivery to the compose() window, as today's watcher does.
  3. Add a tail-cancel test and a CAS-loss test that assert the sidecar survives.

### F3 (major): exits in the worker frame outside `_run_turn` have no owner and are relabelled; the typed failure is lost
- Appendix W6/W9 promise that "any defect escaping the start/adopt path" becomes operation_failed 500. The code does
  something else.
- The terminal failure write itself:
  - `_settle_running` → `fail_composer_async_operation` runs inside a shielded join (T11.md:1366-1371). It has no retry
    and no catch other than FenceLost (T11.md:1524).
  - A transient OperationalError or AsyncWorkerAdmissionTimeoutError on that one write kills the job task, which only
    logs `job_failed` (T11.md:1208-1216). `_close_lease` releases the SOL, `list_expired_running` finds the row, and the
    reaper writes `worker_lost` 503 (T11.md:1647-1698).
  - The already-projected 422 convergence (recovery_text / partial_state), 403 admission, 502 provider or 409 stale body
    is discarded. Today those responses need no DB write to reach the user. The async design adds exactly one write, and
    it is not retried.
- Start composite: a non-listed exception from `_start_composite` (T11.md:1304-1323 catches only four classes) kills the
  job while it holds a claim. The claim expires, the row is re-claimed, and this repeats until `deadline_at`. Then
  `list_expired_queued` settles 504 deadline_expired (T11.md:1624-1645). Today the same fault is an immediate 503/500.
- Adopt: a non-FenceLost exception from `SessionOperationLease.adopt` (T11.md:1346-1353) propagates. Adopt has already
  released the SOL (lifecycle.py:488-497), so the reaper later writes worker_lost 503. The same happens for
  `self._authority.get` or the AuditIntegrityError at T11.md:1359-1361.
- Fix:
  1. Bounded retry of the fail write under the still-live SOL for the transient family.
  2. Wrap `_start_composite` and adopt so a defect settles through `settle_unstarted`/`fail_` with the projected
     envelope, as W9 states.
  3. Update Appendix W6/W9 to match, and add one test per path.

### F4 (minor): a body the strict DTO accepts can 500 before the 202 (an unlisted exit)
- The DTO allows `content` up to 65536 chars (contract.md:143). Admission stores `request_json=body.model_dump_json()`
  (T13.md:944). `admit` raises `ValueError` when `len(request_json) > 131072` (T04.md:1371-1372, 1554-1555).
- T13 catches only the three ComposerOperation* errors (T13.md:947-975). There is no ValueError app handler
  (app.py:1363-2266). The result is a bare 500.
- Measured: 65536 x `\n`, `"` or `\\` gives 131156 chars; 65536 x `\x01` gives 393300.
- Today the same body is accepted. The input is pathological (it must be mostly escape characters near the cap), but the
  exit is not in Appendix A.1.
- Fix, one of:
  - Bound the serialized length in the DTO and return 422 pre-202.
  - Raise the CHECK and the mirrors in T02 `__post_init__` and T04 to at least 6*65536 plus framing.
  - Serialize content so it cannot expand.
- Record the exit in A.1.

### F5 (minor): exceptions after the terminal CAS are discarded without a log
- `_settle_running` returns SETTLED silently when `current.status != "running"` (T11.md:1512-1514).
- An exception raised after `complete_` commits is projected and then thrown away. Examples: the post-commit
  `_project` metrics hook (T06.md:1645-1657), the "complete" publish (T10.md:2392-2401) with a non-advisory class, or
  `registry.finish_request` in the lifecycle's finally (_helpers.py:2639).
- When the projection is http_error (for example, a 503 from finish_request) no diagnostic id is logged, so the fault
  vanishes.
- Fix: log `composer_operation.post_terminal_exception` (with class and failure_code) whenever a failure is discarded
  because the row is already terminal.

### F6 (minor): a cancel during the success-path auto-title join orphans the title task past the terminal
- The success path swaps first: `title_task, auto_title_task = auto_title_task, None` (T10.md:2378-2382). A marker or
  heartbeat cancel that lands inside `asyncio.wait` therefore leaves the title task neither cancelled nor re-joined by
  the finally (T10.md:2482-2483).
- `lease.close()` then joins it unbounded (`asyncio.gather`, lifecycle.py:703-717) after `_settle_running` has written
  the terminal. `update_session_title` (_auto_title.py:385-389) lands after the job settled request_cancelled or
  worker_lost, and `stop()` waits on a provider call with no 2 s bound.
- T20 states "only a join timeout or cancel is logged". `_join_auto_title` (T10.md:1732-1738) logs nothing.
- Fix: cancel and bounded-join the title task in the finally whenever `title_task` is set, and log the timeout or cancel.

### F7 (minor): Appendix A contradicts the task code (progress claim and worker ownership re-check)
- P11/P12 say sink-claim and publish faults, including RuntimeError/ValueError "lease mismatch", are ADVISORY. In the
  code:
  - `_advisory_progress_sink` catches only the lease, Permission, SQLAlchemy and pool-timeout family (T10.md:1706).
  - `test_progress_publish_defect_is_not_swallowed` pins RuntimeError propagating (T10.md:1057-1069).
  - The claim (`_composer_progress_sink_for_lease` → `claim_request`, which raises RuntimeError/ValueError, PermissionError
    or OperationalError: progress.py:127, PA:538-540) sits outside the wrapper (T10.md:1828-1838). A claim failure
    after the send user row commits is therefore terminal.
- Public meaning roughly matches today (500/503), but the inventory is wrong.
- The worker's ownership re-check 404 (T11.md:1461-1472, duplicated at T10.md:1761-1766) is a new POST-202 exit that no
  appendix row lists.
- Fix: reclassify P11/P12 claim-time exits as terminal and add a row for the re-check.

## Verdict
Appendix A is not complete. Two exits are unowned by any task that implements them: the archived-session reaper arm
(F1) and exceptions in the worker frame outside `_run_turn` (F3). One spec requirement (the LLM-call audit before a
cancel terminal) is violated in the post-compose window, and the plan's tests pin the violation (F2). The rest are
minor mis-inventories. The typed ladder (L1-L13), the K classes, the X5 heartbeat 503s and completed-wins races map
correctly as written.

```json
{"findings": [
 {"title": "Adopted deviation settle_lost_inactive_session is implemented by no task; archived-session running rows never settle and can starve the reaper", "severity": "high", "confidence": "confirmed", "files": ["docs/plans/2026-09-20-composer-async-operations/contract.md", "docs/plans/2026-09-20-composer-async-operations/T04.md", "docs/plans/2026-09-20-composer-async-operations/T11.md", "docs/plans/2026-09-20-composer-async-operations.md"], "repro": "grep -nE 'inactive|OWNER_INACTIVE|archived_at' docs/plans/2026-09-20-composer-async-operations/T04.md (no hits); read T11.md:1659-1670 and :2044 against contract.md:21 and index:55", "detail": "Contract override table says T04 adds settle_lost_inactive_session and T11's OWNER_INACTIVE arm calls it; neither does. Row stays running, charges D3 capacity forever; list_expired_running ORDER BY started_at LIMIT 32 with no cursor means >=32 zombies starve reaping of all newer lost rows (probable)."},
 {"title": "Cancel after compose() returns (or winning the terminal CAS) discards the turn's LLM-call audit cohort", "severity": "high", "confidence": "confirmed", "files": ["docs/plans/2026-09-20-composer-async-operations/T10.md", "docs/plans/2026-09-20-composer-async-operations/T11.md", "docs/plans/2026-09-20-composer-async-operations/T06.md", "docs/plans/2026-09-20-composer-async-operations/T16.md"], "repro": "Plan text: T11.md:1405/1437-1439 (markers deliverable through the whole tail), T10.md:2449 (arm drains only _llm_calls_from_exception), T06.md:1560-1642 (composite rolls back cohort on CAS loss), T11.md:1443-1445 (CancelledDuringTurn -> request_cancelled, no persist), T06.md:734-766 and T16.md:1130-1135 pin the loss", "detail": "Spec l.192-194/236-237 require the LLM-call audit before the cancel terminal. Today the disconnect watcher covers only compose() and absorbs racing cancels, so a Stop never drops result.llm_calls; after cutover it does."},
 {"title": "Worker-frame exceptions outside _run_turn (terminal fail write, start composite, adopt) are unowned and relabelled worker_lost/deadline_expired", "severity": "high", "confidence": "probable", "files": ["docs/plans/2026-09-20-composer-async-operations/T11.md", "docs/plans/2026-09-20-composer-async-operations/T00.md"], "repro": "Trace T11.md:1366-1374 (_settle_running unretried), 1208-1216 (job_failed only logged), 1647-1698 (reaper worker_lost); 1304-1323 and 1346-1353 (narrow catches) vs Appendix W6/W9", "detail": "A transient DB fault on the single fail write turns a projected 422/403/502/409 into 503 worker_lost and loses the typed body; start defects loop to 504; adopt defects become 503. Appendix promises operation_failed."},
 {"title": "request_json length bound makes DTO-valid bodies 500 before 202", "severity": "low", "confidence": "confirmed", "files": ["docs/plans/2026-09-20-composer-async-operations/T13.md", "docs/plans/2026-09-20-composer-async-operations/T04.md"], "repro": "pydantic model_dump_json of content='\\n'*65536 -> 131156 chars (>131072); '\\x01'*65536 -> 393300; T04.md:1554 raises ValueError; T13.md:947-975 does not catch it; no ValueError app handler", "detail": "Pathological input, but an unlisted pre-202 exit that today is accepted."},
 {"title": "Post-terminal exceptions discarded silently by _settle_running", "severity": "low", "confidence": "probable", "files": ["docs/plans/2026-09-20-composer-async-operations/T11.md"], "repro": "T11.md:1512-1514 returns SETTLED with no log when the row is already terminal", "detail": "finish_request / post-commit projection faults after complete_ vanish without a diagnostic when projected as http_error."},
 {"title": "Auto-title task orphaned past the terminal when a cancel lands in the success-path join", "severity": "low", "confidence": "probable", "files": ["docs/plans/2026-09-20-composer-async-operations/T10.md"], "repro": "T10.md:2378-2382 swaps auto_title_task to None before awaiting; finally at 2482 then skips; lifecycle.py:703-717 joins unbounded at close after settle", "detail": "Late update_session_title after request_cancelled/worker_lost; stop() unbounded; T20's promised log absent."},
 {"title": "Appendix A P11/P12 and missing ownership-recheck row contradict T10/T11 code", "severity": "low", "confidence": "confirmed", "files": ["docs/plans/2026-09-20-composer-async-operations/T00.md", "docs/plans/2026-09-20-composer-async-operations/T10.md", "docs/plans/2026-09-20-composer-async-operations/T11.md"], "repro": "T10.md:1706 catch tuple; T10.md:1828-1838 claim outside wrapper; T10.md:1057-1069 pins RuntimeError propagation; T11.md:1461-1472 new 404 exit", "detail": "Inventory mis-states classes; public meaning roughly preserved."}
]}
```
