# Red-team review: concurrency core of the composer async operations plan

Lens: disprove the concurrency core (one terminal only, the R3 SOL binding, same-id retry, cross-instance claim).
Plan read at the working-tree copies under `docs/plans/2026-09-20-composer-async-operations/` (T04, T05, T06, T10, T11,
T12, T16, contract.md). Live tree: `release/0.8.1`. Every line cited below was read in this session.

## Attacks tried and closed (no finding)

1. **Two terminals commit (cancel vs completion vs reaper vs deadline).** Every terminal writer runs under the same
   per-session lock. On PG that is `pg_advisory_xact_lock(classid, hashtext(session_id))`
   (`src/elspeth/web/sessions/locking.py:256-261,279-283`), and T06's `_session_write_lock`
   (`service.py:4801-4815`) resolves to the same lock. On SQLite it is the per-session flock sidecar. Each writer is
   also a status CAS: request_cancel `status='queued'` + prior token/attempt (T04:1792-1799); settle_unstarted
   (T04:1883-1890); settle_lost `status='running'` + SOL triple + reaper epoch strictly newer (T04:2008, 2016-2028);
   complete `status='running'` + claim + SOL triple + `cancel_requested_at IS NULL` (T06:1388-1401, 1626); fail drops
   the marker predicate only for `request_cancelled` (T06:1680, 1695-1696). Under READ COMMITTED the discriminating
   SELECT runs after the advisory lock is granted, so it sees the previous holder's commit. A second terminal cannot
   commit; the terminal-immutable trigger is a backstop.
2. **A stale owner writes after losing.** settle_lost needs a live reaper COMPOSE fence that supersedes the row's
   epoch (T04:1986-2009). The owner's writers go through `_require_session_operation_context_on_connection`, which
   needs `released_at IS NULL AND lease_expires_at > now` (`service.py:4958-4959`), so they are fenced out. T16's
   partition test (T16:1070-1093) pins this.
3. **R3 half-states.** The composite start is one transaction. The cancel check comes before the fence advance
   (T05:978-986, pinned by the moved-check mutation control at T05:1051-1054), and the running CAS runs in the same
   transaction. On any failure `SessionOperationLease.adopt` releases the minted context
   (`lifecycle.py:426-500`, `_raise_adopt_failure_after_release`). A released fence can be reaped without the
   membership check (`repository.py:4800-4809` only consults membership when `released_at IS NULL`).
4. **Same-id retry creates a second user row.** Replay goes through the PK read under the lock (T04:1563-1567). A row
   leaves `queued` exactly once (start CAS `status='queued'`, T05:988-1008). The user-row insert and
   `user_message_id` binding are ONE fenced transaction guarded by `user_message_id IS NULL` (T10:1203-1246, 1802).
   T16 windows 1, 3 and 4 assert one conversation row. Closed.
5. **Cross-instance claim while the session has a running job.** D8 active check in admit (T04:1568-1579) plus the
   `claim_next` running re-check under the lock (T04:1703-1707) plus the partial unique index. Closed.
6. **Cancel route ordering.** `request_cancel` commits before `signal_local_cancel` (T12:1154-1171). Closed.

Not filed (within spec, or already documented): the `deadline_at <= updated_at` guard (T11:1292), PG capacity
softness (T04:1541-1543), and the synchronous 409 becoming a requeue (spec §3; raise-inventory-send.md:130,310).

## Findings

### F1 (major, confirmed): `settle_lost_inactive_session` is adopted in the contract, but no task implements it, and T11 leaks the row permanently
- `contract.md:21` (Adopted deviations: "the task files already use the adopted form") says T04 adds
  `settle_lost_inactive_session(...)` and T11's `OWNER_INACTIVE` arm calls it. Plan index Review Focus #5 names
  T04 + T11 as its owners.
- `grep -rn settle_lost_inactive_session docs/plans/2026-09-20-composer-async-operations/` finds only
  `contract.md:21` and the index `:55`. There are zero hits in any task file. T04's Produces list (T04:133-147) and
  its test list have no archived/inactive case.
- T11:1659-1670: the `OWNER_INACTIVE` arm logs `reap_skipped_inactive_session` and returns `SESSION_INACTIVE` on
  every sweep. T11:2044 says the fix is "unresolved and recorded as a residual in RECONCILE.md". That file exists only
  in gitignored lane state (`.claude/lanes/async-ops-plan-2026-09-25/tasks/RECONCILE.md:163-170`), not in the plan
  directory.
- Mechanism: the owner dies with a `running` row, then a delete/archive wins the fence and soft-archives
  (`service.py:7210+`, `decide_and_soft_archive`). If the delete phase then fails or is interrupted, the session stays
  archived. The reaper's acquire then hits `_advance_exclusive_fence_on_connection` (T05:817-820 =
  `repository.py:4786-4790`): archived and not live gives OWNER_INACTIVE, and the reaper skips the row forever.
  `count_nonterminal` (T04:2048-2055) and admit's D3 count (T04:1580-1584) count
  `status IN ('queued','running')` with no archived filter, so each such event takes one cluster slot (default 64)
  for good.
- Fix: implement the contract row in T04 (an unfenced settle guarded by `sessions.archived_at IS NOT NULL` and
  `status='running'` under the session lock, choosing `request_cancelled` if a marker is set), call it from
  T11:1661, and add the RF#5 test in T04 and T11. Or update contract.md and the index to match T11.

### F2 (major, probable): on PostgreSQL, a SOL that lapses while its owner instance is alive leaves a `running` row that no writer can settle, or wedges the session
- One renewal exception of any class marks the lease lost (`lifecycle.py:664-692`, `_record_renewal_error`). After
  that, `close()` does NOT release (`lifecycle.py:740-746`: release only `if self._renewal_error is None`). `release`
  itself also needs `lease_expires_at > now` (`repository.py:5641-5645` via `_exact_active_predicates`
  `:4955-4968`).
- Path 1, fence already expired: the watcher delivers LEASE_LOST (T11:1563-1565), and `fail_composer_async_operation`
  is fenced out (`service.py:4958-4959`), giving ABANDONED (T11:1524-1531). The peer or own reaper's COMPOSE acquire
  reaches `PostgresSessionOperationRepository._expired_owner_allows_takeover` (`repository.py:5780-5790`). That
  returns False while the owner's `web_instances` lease is live, so SessionOperationConflictError, then OWNER_ALIVE
  on every sweep. The row stays `running` until the instance restarts. Admission for that session then returns
  409 `composer_operation_active` naming a job that never ends (T04:1568-1579), and the SPA attaches and polls
  forever (T14: "pollers stop only on terminal").
- Path 2, fence still inside its window: the worker_lost terminal commits, but the fence is left unreleased. Once it
  expires, every later start composite for that session hits the same takeover refusal: release, requeue, and 504
  deadline_expired after 85 s, until the instance restarts.
- The same happens when adopt's own release fails (T11:1346-1353 only catches SessionOperationFenceLost; any other
  exception escapes the job task with the running row bound to a fence nobody renews).
- Test gap: T11's reaper tests use `_start_with_dead_owner` + `_expire_session_fence` under the SAME owner id
  (T11:261-280, 663-702) on SQLite. `SQLiteLocalSessionOperationAuthority._expired_owner_allows_takeover` returns True
  unconditionally (`sqlite_authority.py:50-59`), so they pass. T16 only kills or partitions owners, which lets
  membership lapse. No test covers a live owner with a lapsed SOL on PG.
- This inherits the SOL fail-closed rule; it does not relitigate it. The new part is a durable `running` row plus D8,
  which turn the inherited wedge into an indefinite attach/poll loop, with no owner-side writer for "my own lapsed job".
- Fix: an owner-side guarded settle keyed on `status='running' AND claim_owner_instance_id = self AND fence
  released_at IS NULL AND lease_expires_at <= now` (and a self-owned fence takeover/release for the same instance),
  plus a PG test with a live owner whose SOL is aged.

### F3 (major, probable): claim discovery has head-of-line starvation; jobs blocked by a live foreign SOL are re-claimed every scan and starve startable jobs
- `_claim_candidates` (T04:1636-1674) orders by `created_at` with `LIMIT free`. It excludes only sessions with a
  running composer row, not sessions whose session_operation_fences row is live under another kind.
- An EXECUTE lease is held for a whole pipeline run (`execution/service.py:1692-1720`, a loss watcher for the run's
  duration), and guided turns hold COMPOSE. A freeform send during either is admitted (D2 has no fence check), then
  claimed. The start then raises SessionOperationConflictError, and `release_claim` (T04:1746-1763) returns the row
  unclaimed at the same `created_at` position (T11:1306-1308).
- With `composer_async_worker_concurrency` N (default 4), N such rows are the N oldest. Every scan (1 s) claims them,
  conflicts, releases, and re-discovers the same N. On a single instance (the SQLite deployment) no newer, startable
  job is ever discovered until the blocked rows pass their deadline (85 s). Healthy users' jobs then expire as 504
  `deadline_expired`, caused by other users' runs. On multiple instances, every instance fixates on the same oldest
  rows.
- No fairness or starvation test exists (only the single-row
  `test_session_operation_conflict_releases_the_claim_and_leaves_the_row_queued`, T11:390).
- Fix: filter discovery on the fence row being free or expired (join `session_operation_fences`), or order by
  `updated_at` (which `release_claim` bumps) so conflicted rows rotate to the back. Add a test with N+1 rows where N
  are fence-blocked.

### F4 (major, probable): one transient claim-renewal error can crash the scan loop and restart the instance
- `_renew_claim` (T11:1249-1253) is `while True: sleep; renew` with no retry. The first exception (OperationalError,
  pool admission timeout, FenceLost) ends renewal silently; `_stop_claim_renewal` only logs it later (T11:1255-1264).
- The job task can still be waiting on the in-process compose lock (T11:1228-1230). A guided turn on the same
  session, or the previous job's close, can hold it. The claim expires (30 s), and the SAME instance's `claim_next`
  re-claims the row (`_claim_is_live` false, T04:1490-1491, 1695-1702). `_spawn` then raises RuntimeError
  "claim_next returned an operation this worker already runs" (T11:1197-1200).
- That propagates through `_claim_and_spawn`/`_scan_once`. `_loop_forever` catches only `_LOOP_TRANSIENT_ERRORS`
  (T11:1170), so the scan task dies. `_escalate_loop_failure` then sets draining and `request_shutdown()`
  (T11:1180-1183). One transient DB error becomes an instance restart that loses every in-flight turn as worker_lost.
  The new claim and the rest of that batch are also orphaned.
- Fix: retry transient renew errors within the lease (as the membership heartbeat does). In `_claim_and_spawn`, treat
  a claim for an owned key as "my stale job lost its claim": cancel or mark the old job and keep the loop alive.

### F5 (minor, probable): the reaper re-raises SessionOperationFenceLost(MISSING), so a session delete racing a sweep kills the reap loop
- T11:1659-1671 swallows only OWNER_INACTIVE and re-raises every other reason. `list_expired_running` is a lock-free
  hint. A session hard-deleted in between (`repository.py:5650-5663` archive_delete; the FK CASCADE removes the job)
  makes acquire raise MISSING (`repository.py:4786-4790` / T05:809-810).
- Non-transient, so the reap loop dies and the instance drains and restarts (T11:1170-1183). The startup `reap_once`
  placement (contract row 34) makes the same race fail lifespan startup.
- Fix: treat MISSING like ALREADY_TERMINAL.

### F6 (minor, probable): `stop()` can miss jobs spawned by an in-flight scan
- `stop()` snapshots `self._jobs` once (T11:1093-1099). `_scan_once` checks `_stopping`/draining only before its
  await (T11:1185-1188). `_claim_and_spawn` spawns after `await claim_next` without re-checking (T11:1190-1195).
- Jobs spawned after the snapshot are never sent `request_shutdown` and never joined. They can start a provider turn
  after drain began, and are later reaped as worker_lost. Fix: re-check `_stopping` after the await (release the
  claims), or loop the snapshot until `_jobs` is empty.

```json
{"findings": [
  {"title": "settle_lost_inactive_session adopted in contract.md but absent from T04/T11; archived-session running row leaks D3 capacity forever",
   "severity": "high", "confidence": "confirmed",
   "files": ["docs/plans/2026-09-20-composer-async-operations/contract.md", "docs/plans/2026-09-20-composer-async-operations/T04.md", "docs/plans/2026-09-20-composer-async-operations/T11.md", "docs/plans/2026-09-20-composer-async-operations.md"],
   "repro": "grep -rn settle_lost_inactive_session docs/plans/2026-09-20-composer-async-operations/ -> only contract.md:21; T11.md:1659-1670 skips OWNER_INACTIVE; T11.md:2044 declares it unresolved",
   "detail": "Contract's overriding Adopted-deviations row and Review Focus #5 promise an unfenced archived-session settle in T04 called from T11; neither task has it, T11 skips the row every sweep, and count_nonterminal/admit count it against max_nonterminal permanently."},
  {"title": "PG: a SOL lapsed while its owner lives leaves a running row unsettleable (or the session wedged); only SQLite tests cover the reaper",
   "severity": "high", "confidence": "probable",
   "files": ["docs/plans/2026-09-20-composer-async-operations/T11.md", "src/elspeth/web/coordination/repository.py", "src/elspeth/web/coordination/lifecycle.py", "src/elspeth/web/coordination/sqlite_authority.py"],
   "repro": "Reasoned: renew error -> lifecycle.py:689-692 lost; close skips release lifecycle.py:740-746; terminal fenced service.py:4958-4959; reaper acquire -> repository.py:5780-5790 False while owner web_instances lease live -> OWNER_ALIVE forever. T11 reaper tests run on SQLite where takeover is unconditional (sqlite_authority.py:50-59).",
   "detail": "Durable running row + D8 409 turns the inherited fail-closed SOL rule into an indefinite attach/poll loop; no owner-side settle/release for its own lapsed job; no PG live-owner test."},
  {"title": "claim discovery head-of-line starvation: fence-blocked queued rows re-claimed every scan starve startable jobs to deadline_expired",
   "severity": "high", "confidence": "probable",
   "files": ["docs/plans/2026-09-20-composer-async-operations/T04.md", "docs/plans/2026-09-20-composer-async-operations/T11.md"],
   "repro": "Reasoned: _claim_candidates ORDER BY created_at LIMIT free (T04:1636-1674) with no fence filter; start Conflict -> release_claim (T11:1306-1308) keeps position; EXECUTE held for run duration (execution/service.py:1692-1720). N=concurrency blocked rows monopolise discovery.",
   "detail": "Healthy jobs expire as 504 deadline_expired because of other users' pipeline runs or guided turns; no fairness test."},
  {"title": "single transient claim-renewal error -> same-instance reclaim -> _spawn RuntimeError kills scan loop -> instance drain/restart",
   "severity": "high", "confidence": "probable",
   "files": ["docs/plans/2026-09-20-composer-async-operations/T11.md", "docs/plans/2026-09-20-composer-async-operations/T04.md"],
   "repro": "Reasoned: _renew_claim no retry (T11:1249-1253); job waits on in-process lock (T11:1228-1230); claim expires; claim_next reclaims (T04:1695-1702); _spawn raises (T11:1199-1200); _loop_forever catches only transient (T11:1170); _escalate_loop_failure shuts down (T11:1180-1183).",
   "detail": "One DB hiccup converts to an instance restart losing every in-flight turn as worker_lost."},
  {"title": "reaper re-raises SessionOperationFenceLost(MISSING) on a session deleted between list and acquire, killing the reap loop / startup",
   "severity": "low", "confidence": "probable",
   "files": ["docs/plans/2026-09-20-composer-async-operations/T11.md"],
   "repro": "Reasoned: T11:1659-1671 re-raises non-OWNER_INACTIVE; archive_delete cascade (repository.py:5650-5663); acquire MISSING (repository.py:4786-4790).",
   "detail": "Benign delete race becomes instance drain; startup reap_once would fail lifespan."},
  {"title": "stop() snapshots jobs once; scan in flight spawns unowned jobs after shutdown began",
   "severity": "low", "confidence": "probable",
   "files": ["docs/plans/2026-09-20-composer-async-operations/T11.md"],
   "repro": "Reasoned: T11:1093-1099 snapshot; _claim_and_spawn spawns after await without _stopping re-check (T11:1190-1195).",
   "detail": "Jobs spawned after the snapshot are never shut down or joined and may start provider turns during drain."}
]}
```
