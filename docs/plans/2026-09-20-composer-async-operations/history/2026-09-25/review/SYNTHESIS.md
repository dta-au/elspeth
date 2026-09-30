# Plan Review Synthesis: CHANGES_REQUESTED

**Plan:** `docs/plans/2026-09-20-composer-async-operations.md` + `T00..T18.md` + `contract.md` + `findings/`
**Spec:** `docs/specs/2026-09-16-composer-async-operations-design.md` (freeform-only amendment)
**Tree:** the main checkout, `release/0.8.1` @ `53b7d4344` (the plan's measurement points `d479eb2b4`/`ea5fa50d5` are ancestors; the cited web paths are unchanged)
**Synthesized:** 2026-09-25
**Reviewer inputs read in full:** reality-a (T00–T04), reality-b (T05–T09), reality-d (T14–T18), architecture, quality, systems, redteam-raise, redteam-cas.
**Missing input:** `reality-c.md` (T10–T13) **does not exist** in the review directory, even though the brief lists it and says "missing: none". T10–T13 had no reality/anchor pass. See Information Gaps.

Owner rulings not relitigated: freeform-only scope; 202 + poll now; the composite terminal transaction; the running fence bound to the COMPOSE SOL through one composite tx + `SessionOperationLease.adopt`.

Every finding below was re-checked against the plan text and/or the live tree in this session. The cited lines are the ones I read, not the reviewer's.

---

## Verdict

**CHANGES_REQUESTED.** There are 4 blockers. Three contradict the plan's own binding text: the contract's "Adopted deviations" override (contract.md:21), Review Focus #1 and #5 (index:51, :55), and spec §3 l.192-194. The fourth (B4) quietly changes the public meaning of the terminal error, which the plan's Global Constraints require to be preserved. There are also 8 majors and 9 minors. The concurrency core held up: single terminal, R3 half-states, same-id replay, cross-instance claim, and cancel-route ordering were all attacked and closed by redteam-cas.

---

## Blocking issues (must fix before execution)

### B1 — `settle_lost_inactive_session` is adopted in the contract and implemented by no task; archived-session `running` rows leak capacity forever and can starve the reaper
- **Sources:** quality F1 (blocker), architecture #1 (major), redteam-raise F1 (major), redteam-cas F1 (major), T00 Review note 11. Severity conflict resolved **upward to blocker**: contract.md:21 says the Adopted-deviations table "wins over any later section… the task files already use the adopted form", and index:49 says "The fix pass adds each line's test to the owning task".
- **Evidence (verified):**
  - `grep -rn settle_lost_inactive_session docs/plans/2026-09-20-composer-async-operations/ docs/plans/2026-09-20-composer-async-operations.md` finds only contract.md:21 and index:55. T04.md has zero `archiv|OWNER_INACTIVE` hits.
  - T11.md:1659-1670: the `OWNER_INACTIVE` arm logs `reap_skipped_inactive_session` and returns `SESSION_INACTIVE`. T11.md:2044 says "The fix is unresolved and recorded as a residual in RECONCILE.md", which is a gitignored lane file (`.claude/lanes/.../tasks/RECONCILE.md`, `.gitignore:67`), not part of the plan.
  - Mechanism confirmed on the tree. `repository.py:4797-4800`: archived + fence not live → `SessionOperationFenceLost(OWNER_INACTIVE)`. `count_nonterminal` and admit's D3 count have no archived filter (T04:2048-2055, :1580-1584).
  - Starvation amplifier (redteam-raise F1): `list_expired_running` is `ORDER BY started_at … LIMIT limit` with no cursor (T04.md:1953-1954), and the reaper uses `_REAP_BATCH_SIZE = 32` (T11.md:874). Once 32 archived zombies are older than a genuinely lost row, the reaper sees only the zombies, and live sessions stay behind D8's 409.
  - Adjacent defect in the same arm (redteam-cas F5): T11.md:1671 re-raises every other `FenceLossReason`. A session hard-deleted between `list_expired_running` and `acquire` raises `MISSING` (`repository.py:4789-4790`). That class is not in `_LOOP_TRANSIENT_ERRORS` (T11.md:872), so the reap loop dies and `_escalate_loop_failure` drains the instance (T11.md:1180-1183). The startup `reap_once` then fails lifespan.
- **Fix (owner T04, with T11 in the same item):**
  - **T04:** add `settle_lost_inactive_session(*, session_id, operation_id, failure, cancelled_failure)` with exactly the contract.md:21 signature.
    - It is an unfenced settle inside `locked_session_transaction`, guarded by `sessions.archived_at IS NOT NULL` and `status='running'`.
    - It substitutes `cancelled_failure` when `cancel_requested_at` is set, mirroring `settle_lost`'s D-4b substitution.
    - It raises `ComposerOperationFenceLost` when the guard fails.
    - Add it to T04's Produces list and add SQLite + PG tests: archived → settles `worker_lost`; archived + cancel marker → `request_cancelled`; not archived → refuses; already terminal → refuses.
  - **T11:** in `_reap_running` (T11.md:1659-1671):
    - Call it from the `OWNER_INACTIVE` arm, using `worker_lost_error(request_id=...)` as in the `settle_lost` call. A `ComposerOperationFenceLost` from it maps to `ALREADY_TERMINAL`.
    - Map `FenceLossReason.MISSING` to `ALREADY_TERMINAL` instead of re-raising. Keep re-raising other reasons.
    - Keep one `slog` event per settle.
    - Add reaper tests for an archived session with a dead owner (settles, `count_nonterminal` drops) and for the delete-between-list-and-acquire race (the loop survives).
    - Delete the "unresolved … RECONCILE.md" review note at T11.md:2044 and the "Unresolved W18 hole" comment.
  - **Also:** T00 Appendix row W18 (T00.md:875) and Review note 11 (T00.md:695): change "unresolved" to "settled by `settle_lost_inactive_session` (T04/T11)".
  - **Also:** T16.md:1459's "Not covered here… archive during a job (D7, Task 13 and Task 4 tests)" must point at the new T04/T11 tests.
  - **Only if the owner rules the leak an accepted residual instead:** rewrite contract.md:21 and index:55 to say "residual", move RECONCILE.md R1 into `findings/`, and give `list_expired_running` a skip for rows already classified `SESSION_INACTIVE`. That route is an owner decision, not a fixer's.

### B2 — A cancel landing after `compose()` returns, or winning the terminal CAS, discards the turn's LLM-call audit cohort (spec §3 l.192-194, l.236-237)
- **Source:** redteam-raise F2 (major, confirmed), corroborated by T11's own review note ("If Task 6 rolls the whole composite back when the cancel committed first, that turn's audit cohort would be lost unless Task 6 persists it separately. Verify this in Task 6 before merge", T11.md, notes block). Raised to **blocker**: it violates an explicit spec requirement, and the plan's tests pin the violation. Audit primacy is a product characteristic (ADR-046).
- **Evidence (verified):**
  - `_ComposerJob.deliver()` fires whenever `phase == "running"` (T11.md:926).
  - The phase becomes `"running"` before `_run_turn` (T11.md:1405) and flips to `"settling"` only in the `finally` after `run_composer_turn` returns (T11.md:1437-1439). So a Stop, lease-loss, shutdown or heartbeat marker can land anywhere in the post-compose tail: auto-commit settlement, state save, auto-title join, and the composite.
  - `except ComposerOperationCancelledDuringTurn` maps straight to `request_cancelled` with no persist (T11.md:1443-1445).
  - Spec l.192-194: "…with the LLM-call audit cohort persisted before terminal publication". Spec l.236-237: "…settles `request_cancelled` after its audit join".
  - Today `_cancel_on_client_disconnect` wraps only `compose()` and absorbs a racing disconnect so "its results persist normally" (redteam-raise, `_helpers.py:2300-2322`).
  - Tests that pin the loss: T06 `test_cancel_committed_first_raises_cancelled_during_turn_and_writes_nothing` (T06.md:734-766) and T16 `test_cancel_and_completion_settle_exactly_one_terminal` (T16.md:1130-1135).
- **Fix (owner T11; T10, T06 and T16 edited in the same item):**
  - Choose one of two designs:
    - **(a) Narrow marker delivery.** Deliver the marker only inside the `compose()` window, today's semantics: T10 sets a job-visible flag after `compose()` returns, and `deliver()` refuses outside it. A cancel committed during the tail is then decided only by the T06 CAS.
    - **(b) Persist the cohort before the cancel terminal.** On `ComposerOperationCancelledDuringTurn`, or on a `CancelledError` raised after the compose result exists, persist `result.llm_calls` (and tool rows, if kept) in their own fenced transaction before the `request_cancelled` fail write. This mirrors `_persist_llm_calls`, and T10 must expose the result to the job frame.
  - In either design, T06:734-766 must stop asserting that the LLM sidecar is gone after a committed cancel. It should assert that the sidecar survives while the assistant row does not.
  - T16:1130-1135 must assert the cohort is present on the CAS-loss branch.
  - Add a T11 test that cancels in the tail (after `compose()` returns, before the composite) and asserts the llm_calls rows exist.
  - Remove the "Verify this in Task 6 before merge" note once the fix is done.

### B3 — The client deadline compares the server's absolute `deadline_at` to `Date.now()`, which is the exact anti-pattern Review Focus #1 forbids
- **Source:** quality F2 (blocker). Confirmed.
- **Evidence (verified):**
  - index:51 requires that "the client deadline is measured from a server-relative remaining duration, never by comparing the server's absolute `deadline_at` to `Date.now()`".
  - T14.md:17-18 (CONTRACT DEVIATION) and T14.md:3825-3830 implement `Date.now() >= Date.parse(answer.deadline_at) + COMPOSE_CLIENT_GRACE_MS`.
  - The poll body (contract.md:151, `ComposerOperationStatusResponse`) carries only `deadline_at`, with no server clock and no remaining duration.
  - `grep -n "skew|remaining_ms|remaining_seconds|server_now|performance.now"` finds zero hits across every task file and the spec.
  - A client clock 10 minutes fast cancels a healthy turn on its first poll.
- **Fix (owner T12; T02, contract and T14 edited in the same item):**
  - **Contract:**
    - Add `deadline_remaining_ms: int` (≥ 0) to `ComposerOperationStatusResponse` in contract §Wire DTOs (contract.md:151).
    - Rewrite the "client deadline" Adopted-deviations row (contract.md:38) and §Frontend (contract.md:416) to "remaining-duration measured on the client's monotonic clock".
  - **T02:** add the field to the DTO class and its tests.
  - **T12:** have the poll and cancel routes compute it from database time (`deadline_at − database now`, clamped at 0) and add a route test.
  - **T14:**
    - When the attachment first sees a non-terminal body, and on every later one, set `localDeadline = performance.now() + deadline_remaining_ms + COMPOSE_CLIENT_GRACE_MS`.
    - Compare against `performance.now()` and stop using `Date.parse(deadline_at)`.
    - Update the T14.md:13-20 deviation text.
    - Add a Vitest test with `Date.now()` mocked ±10 minutes from the server clock. It must assert that there is no early cancel, and that the cancel fires once the true remaining time plus grace has elapsed.
    - Keep the existing `>=`/`+60_000` mutation control.

### B4 — Worker-frame exceptions outside `_run_turn` have no owner; a transient fault on the single terminal write relabels a preserved public error as 503 `worker_lost`
- **Source:** redteam-raise F3 (major, probable). Raised to **blocker**: the Global Constraints say "Post-202 errors keep today's public HTTP meaning inside the terminal envelope", and today those responses need no DB write at all.
- **Evidence (verified):**
  - `_settle_running` → `fail_composer_async_operation` catches only the FenceLost pair and has no retry (T11.md:1509-1533).
  - An `OperationalError` or `AsyncWorkerAdmissionTimeoutError` there kills the job task. `_forget_job` then only logs `job_failed` (T11.md:1208-1216), and the reaper later writes `worker_lost` 503 (T11.md:1647-1698). The projected 422 convergence, 403, 502 or 409 body is lost.
  - `_start` catches four classes around `_start_composite` (T11.md:1304-1323). Any other exception kills a claimed job whose renewal has already stopped (T11.md:1302). The claim expires, the job is re-claimed, and this loops until `deadline_at`, after which the reaper settles 504 `deadline_expired`. Today the same fault is a 503/500.
  - `SessionOperationLease.adopt` catches only `SessionOperationFenceLost` (T11.md:1346-1353). T00 Appendix W6/W9 promise "operation_failed 500" for these defects.
- **Fix (owner T11; T00 Appendix edited in the same item):**
  - (1) Wrap the terminal fail write in a bounded retry for the transient family (`OperationalError`, `AsyncWorkerAdmissionTimeoutError`) while the adopted SOL is still live. Re-check `status == 'running'` between attempts.
  - (2) In `_start`, add a final `except Exception` arm that settles through `settle_unstarted(claim, project_composer_operation_error(...))`, so a start defect becomes `operation_failed` 500 with a `diagnostic_id`, not a 504 loop.
  - (3) Give adopt the same arm.
  - (4) Add one test per path: transient on the fail write → the typed body survives; start defect → 500 `operation_failed`, not 504; adopt defect → 500.
  - (5) Update T00 Appendix W6/W9 to match.

---

## Major (should fix)

### M1 — [T11] One transient claim-renewal error can end in an instance restart
- **Source:** redteam-cas F4. Every link verified:
  - `_renew_claim` is `while True: sleep; renew` with no retry (T11.md:1249-1253).
  - The job can be waiting on the in-process compose lock (T11.md:1228-1230); a guided turn can hold that lock longer than the 30 s claim lease.
  - Once the claim expires, `claim_next` re-claims the row for the same instance (T04 `_claim_is_live` false).
  - `_spawn` then raises `RuntimeError("claim_next returned an operation this worker already runs")` (T11.md:1197-1200).
  - `RuntimeError` is not in `_LOOP_TRANSIENT_ERRORS` (T11.md:872), so the scan task dies and `_escalate_loop_failure` drains the instance (T11.md:1180-1183). Every in-flight turn is lost as `worker_lost`.
- **Fix:**
  - Retry transient renew errors inside `_renew_claim` while `claim_expires_at` has not passed, as the membership heartbeat does. Log each retry.
  - In `_claim_and_spawn`, treat a claim whose key is already in `self._jobs` as "my stale job lost its claim": cancel the old job (it is in phase `claimed`, so a plain cancel releases the claim), release or skip the new claim, and keep the loop alive. Do not raise.
  - Add a test: renewal raises once → no loop death; forced same-key reclaim → loop survives.

### M2 — [T04] Claim discovery has head-of-line starvation: fence-blocked rows are re-claimed every scan and push startable jobs to 504
- **Source:** redteam-cas F3. Verified:
  - `_claim_candidates` (T04.md:1636-1674) orders by `created_at` with `LIMIT free`. It excludes only sessions with a running or older-queued composer row, not sessions whose `session_operation_fences` row is live under another kind (EXECUTE for a whole pipeline run, or a guided COMPOSE).
  - A start conflict calls `release_claim` (T04.md:1746-1763), which bumps `updated_at` but keeps the row's `created_at` position.
  - N = `composer_async_worker_concurrency` blocked rows therefore occupy every scan until their 85 s deadline.
- **Fix:**
  - Add `~exists(fence where session_id matches AND released_at IS NULL AND lease_expires_at > now)` to `_claim_candidates`. The authority already selects from `_FENCES` in `list_expired_running`, and the fence table is keyed by `session_id`, so no T03 index change is needed.
  - The advisory filter is re-decided by the start composite.
  - Do not switch to ordering by `updated_at`: T03's `ix_composer_async_operations_claimable` is on `(status, claim_expires_at, created_at)` (T03.md:60).
  - Add a test with N+1 queued rows where N sessions hold a live foreign fence: the startable row is claimed on the first scan.

### M3 — [T11, with a T16 test] On PostgreSQL, a SOL that lapses while its owner instance is alive leaves a `running` row no writer can settle; D8 turns that into an indefinite 409/poll
- **Source:** redteam-cas F2 (probable, reasoned; inherited-and-amplified). Verified on the tree:
  - Any renewal exception records the lease as lost (`lifecycle.py:690-691`).
  - `close()` skips release when `_renewal_error` is set (`lifecycle.py:740-746`).
  - PG `_expired_owner_allows_takeover` refuses while the owner's `web_instances` lease is live (`repository.py:5780-5790`), and `acquire` has no same-instance exemption (`repository.py:4801-4808`). So the owner's own reaper also gets `SessionOperationConflictError` → `OWNER_ALIVE` on every sweep.
  - SQLite always permits takeover (`sqlite_authority.py:50-59`), so T11's reaper tests (SQLite, same owner id) pass.
- The fail-closed SOL rule itself is inherited and is not relitigated here. What is new is the durable `running` row plus D8's 409 `composer_operation_active`, which the SPA attaches to and polls until the instance restarts.
- **Fix:**
  - T04 adds an owner-side guarded settle for "my own lapsed job". Its guard: `status='running' AND claim_owner_instance_id = self AND` the bound fence triple has `released_at IS NULL AND lease_expires_at <= now`, all under the session lock.
  - T11 calls it from the watcher's LEASE_LOST path when `fail_composer_async_operation` is fenced out, instead of returning ABANDONED (T11.md:1524-1531).
  - T16 adds a PG test with a live owner whose SOL is aged past expiry: the row reaches terminal and the next send is admitted.
  - If the owner prefers not to add a self-owned settle path, record the wedge as an accepted residual in contract.md with the restart-to-clear behaviour. **Flag for human decision:** it touches SOL recovery policy.

### M4 — [T14] The two 404 shapes the poll route deliberately distinguishes are collapsed; the archived-in-another-tab case renders "operation missing" rather than today's "session not found" (Review Focus #2)
- **Source:** quality F3. Verified:
  - contract.md:363-364 defines two 404 bodies: the `Session not found` shape and `{"detail":"Operation not found"}`.
  - `fetchComposerOperation` and `cancelComposerOperation` return `null` on any 404 without reading the body (T14.md:1601-1603, :1621-1623).
  - T14.md has zero hits for "session not found".
- **Fix:**
  - Have the two client functions return a discriminated `{kind:"session_missing"} | {kind:"operation_missing"}` (or throw the typed ApiError for the session case) based on the 404 body.
  - Make the store's poll loop route `session_missing` to today's session-not-found path: stop polling, clear `composerOperationCustody`, no resubmission.
  - Add a store-level test that feeds the Session-not-found body and asserts that path, plus the contrasting operation-missing test.

### M5 — [T11, T04, T16] No test proves any worker/reaper structured log fires, including the spec-required audit-incomplete diagnostic
- **Source:** quality F4. Verified: `grep -c "capture_logs|caplog"` returns 0 for T04, T06, T11, T12, T13 and T16. Spec W18 requires "audit-incomplete diagnostics" (T00.md:875), which T11 emits as `composer_operation.reaped_running` with `audit_may_be_incomplete=True` (T11.md:1690-1695). T09.md:692-704 shows the in-plan pattern (`structlog.testing.capture_logs()`).
- **Fix:**
  - T11 adds `capture_logs()` assertions for at least `reaped_running` (including `audit_may_be_incomplete=True`), `job_failed`, `terminal_write_fenced`, `start_fence_lost`, and the settle event B1 introduces.
  - Each assertion checks the event name and the key fields, and checks that no secret or URL appears, reusing T09's pattern.

### M6 — [T14, T15 doc] The guided client abort ceiling goes stale after decoupling, and T15 publishes a false "no frontend change is needed"
- **Source:** systems F1 (major), plus a defect found during synthesis in T15.
- **Evidence (verified):**
  - `App.tsx:401-403` feeds `status.composer_timeout_seconds` to `applyServerComposerTimeout`, which guided's `runComposeWithTimeout` uses.
  - T01.md:1101 says "Task 14 or 15 should make the guided client read `composer_sync_timeout_seconds`". Neither task does: T14 touches `applyServerComposerTimeout` only in tests (T14.md:3307, :3327).
  - T15.md:814-817 states "the SPA's freeform deadline tracks each operation's server `deadline_at`, so no frontend change is needed when either budget moves". That is false for guided.
  - On ACA (fixed 240 s ingress) the sync cap is 210 s while the guided client timer arms at 840 s.
- **Fix:**
  - T14: feed `composer_sync_timeout_seconds` (published by T01, T01.md:73) to the guided/`runComposeWithTimeout` ceiling, keeping the readiness latch. Add a test that the guided ceiling follows the sync key when the two differ.
  - T15: rewrite T15.md:814-817 to say the guided ceiling reads `composer_sync_timeout_seconds`.
  - Guided must "behave identically" (Scope ruling).

### M7 — [T00] Appendix A contradicts the task code and omits a post-202 exit
- **Source:** redteam-raise F7. Verified:
  - T00.md:783-784 (P11/P12) call sink-claim and publish faults "POST-202 ADVISORY".
  - But `_advisory_progress_sink` catches only `ComposerRequestLeaseLost, PermissionError, SQLAlchemyError, AsyncWorkerAdmissionTimeoutError` (T10.md:1706), T10.md:1057-1069 pins `RuntimeError` propagating, and the claim call sits outside the wrapper (T10.md:1828-1838).
  - The worker's ownership re-check 404 (T11.md:1461-1472) is not in the appendix.
- **Fix:**
  - Reclassify the P11 claim-time `RuntimeError`/`ValueError` and the P12 claim faults as TERMINAL, with their actual projected codes.
  - Add an appendix row for the ownership re-check 404, owned by T11.
  - T11's own note says to keep one of the duplicated re-checks (T11 notes, "may duplicate a check in Task 10's preamble"); name which one survives.
  - B4 separately corrects W6/W9.

### M8 — [T11] `stop()` misses jobs spawned by an in-flight scan, and deploy drain cancels every in-flight turn as `worker_lost` without saying so
- **Sources:** redteam-cas F6 (minor) and systems F2 (major, downgraded; see Conflicts). Merged because both concern `stop()`.
- **Evidence (verified):**
  - `stop()` snapshots `self._jobs` once (T11.md:1093-1094).
  - `_scan_once` checks `_stopping` only before its await (T11.md:1185-1187), and `_claim_and_spawn` spawns after `await claim_next` without re-checking (T11.md:1190-1195).
  - Spec l.200-201 does mandate "Worker shutdown requests cancellation and drains owned tasks", so the immediate cancel conforms to the spec. The gap is that no deploy doc says so.
- **Fix:**
  - T11: after `await claim_next`, if `_stopping` or draining is set, release each new claim without spawning. Alternatively, have `stop()` loop until `_jobs` is empty after the loops are cancelled.
  - T11: add a test in which `stop()` runs while `claim_next` is awaiting.
  - T17: add one CHANGELOG/runbook line saying that every deploy or drain settles in-flight freeform turns as 503 `composer_operation_worker_lost` ("reload and resubmit").

---

## Minor

| Id | Task | Issue | Fix | Evidence |
|---|---|---|---|---|
| m1 | T02 (owner; T03, T04, contract) | A DTO-valid body can 500 before the 202: `request_json` cap 131072 < worst-case serialized length; no `ValueError` catch in T13, no app handler | Measure the maximum `model_dump_json()` length of each strict request DTO at its field caps, using worst-case escaping characters. Set `COMPOSER_OPERATION_REQUEST_JSON_MAX_LENGTH` above it (≥ 393230 + framing) in T02, the T03 CHECK (T03.md:71, :781, :1468), the T04 mirror (T04.md:1371) and contract.md:173. Alternatively, bound the serialized length in the DTO validator (422 pre-202). Add the exit to Appendix A.1 | Measured with the venv's pydantic: `'\n'*65536` → 131086, `'\x01'*65536` → 393230; T13.md:940-975 catches only the three ComposerOperation* errors |
| m2 | T11 | `_settle_running` silently discards an exception raised after the terminal already committed | When `current.status != "running"` and `failure` is not None, emit `composer_operation.post_terminal_exception` with `failure_code` and the exception class before returning SETTLED | T11.md:1512-1514 |
| m3 | T10 | A cancel during the success-path auto-title join orphans the title task past the terminal | Keep `auto_title_task` referenced until the join completes, or wrap the join in `try/finally` that cancels and bounded-joins. Log a timeout or cancel in `_join_auto_title`, which Appendix T20 promises and T10.md:1732-1738 does not do | T10.md:2378-2382 swaps to `None` before awaiting; the finally at T10.md:2482-2483 then skips it |
| m4 | T04 | Review Focus #4 (two tabs, different ids) is proven only sequentially | Add `test_concurrent_different_id_admits_race_for_the_active_slot` (SQLite + PG) using the existing `ThreadPoolExecutor` harness: exactly one admit lands, the other raises `ComposerOperationActiveError` naming the winner | T13's test waits on `composer.first_call_started` (quality F5); T04's concurrent tests are same-id only |
| m5 | T01 (owner; contract) | `composer_async_worker_concurrency` accepts up to 256, but every authority call shares the process-wide 16-worker/16-queue pool | Cap `le=` at a value the pool can serve (≤ 16), or state the `async_workers.MAX_WORKERS/MAX_QUEUED` coupling in the field description | contract.md:76; `async_workers.py:14-19`; T11.md "every call here uses the shared 16+16 pool" |
| m6 | T00 | Step 2's `worktree-cleanup.sh` anchors are ~79 lines stale | REMOVABLE decided at `:212`, removal loop `:291-299`, link loop `:312-320`, `ln -s` line `:319` | T00.md:90 cites `:133`, `:212-220`, `:233-240`; `grep -n` on the script: 212, 299, 319 |
| m7 | T02 | Name-to-line pairing of the hashing consumes is out of order | `is_lower_sha256_hex:32, canonical_json:66, stable_hash:89` | T02.md:68; `hashing.py:32,66,89` |
| m8 | T18 | nginx citation range misses the `location /` block | `deploy/compose/nginx.conf:25-37` | T18.md:1067; `location / {` at line 25 |
| m9 | index | `LegacySendMessageRequest` ships permanently if the branch is merged or paused between T10 and T13 | Add one line to Execution notes → Stop points: "no interim merge between T10 and T13" | T10.md:174-239 introduces it; T13.md:1134-1151 deletes it |

---

## Cross-task fixes (one owner each)

| Id | Owner | Also edits | Summary |
|---|---|---|---|
| B1 | T04 | T11, T00 (W18 + Review note 11), T16 (:1459 pointer); contract/index untouched unless the owner picks the residual route | add `settle_lost_inactive_session`, wire the OWNER_INACTIVE arm, stop re-raising MISSING, remove the RECONCILE.md reference |
| B2 | T11 | T10 (expose the compose result or a compose-window flag), T06 (:734-766 test flips), T16 (:1130-1135) | preserve the LLM-call cohort on any cancel after compose |
| B3 | T12 | contract (:38, :151, :416), T02 (DTO field), T14 (monotonic deadline + skew test) | server-relative `deadline_remaining_ms` |
| B4 | T11 | T00 (Appendix W6/W9) | retry the terminal write; catch-all start/adopt arms settle `operation_failed` |
| M3 | T11 | T04 (owner-side settle), T16 (PG live-owner test), contract (if accepted as residual) | lapsed-SOL live-owner wedge; **needs an owner decision** |
| M6 | T14 | T15 (:814-817 text) | guided ceiling reads the sync key |
| M8 | T11 | T17 (deploy note) | stop()/scan race + document drain behaviour |
| m1 | T02 | T03, T04, contract, T00 A.1 | request_json bound ≥ worst-case serialization |
| m5 | T01 | contract (:76) | cap or document the worker concurrency coupling |

---

## Conflicts resolved

| Issue | Views | Resolution |
|---|---|---|
| Severity of the archived-session leak (B1) | quality: blocker; architecture, redteam-raise, redteam-cas: major | **Blocker.** The contract's own override table and Review Focus #5 bind it; err upward. Quality's "maybe an accepted residual" is refuted as a resting state: the only acceptance record is a gitignored lane file, and contract.md:21 says the tasks "already use the adopted form" |
| Systems F2: immediate cancel on drain, no grace window | systems: major design gap | **Downgraded to a doc line (in M8).** Spec l.200-201 prescribes cancel-and-drain. A grace window would be a spec change for the owner |
| R05 lock-order entanglement | index Execution notes imply risk; systems: checked, not a defect | **Accepted as not a defect.** `acquire` touches only `sessions` and the fence table (`repository.py:4786-4830`, re-read); the rate limiter commits on its own connection (contract.md:18) |
| redteam-raise F3 severity | redteam: major/probable | **Blocker (B4).** It changes a public error's meaning, which the Global Constraints forbid |

## Dropped (with reason)

- **Architecture #2 (`run_composer_turn` is ~800 lines).** Dropped as style. The plan deliberately chose a byte-preserving move of two route bodies (contract §run_composer_turn: "moved bodies… typed ladder unchanged"). Refactoring during the move widens the diff that T00 Appendix A audits. A later extraction of the LiteLLM arms is reasonable follow-up work, not a plan defect.
- **Architecture #4, #5, #6.** The reviewer marked them "OK / informational", and they need no change.
- **Quality F6 (T16 praise).** Calibration only.
- **Systems F3 as a major.** Kept only as m5. The worker already retries pool saturation, and the cross-feature contention is unmeasured.
- **Reality-b.** Zero defects in T05–T09. It was checked, not dropped.

---

## Confidence Assessment

**Overall Confidence:** Moderate-High. The blockers and M1, M2, M4, M5, M6 and M7 were each re-verified line by line in this session. M3 and M2's runtime consequences are reasoned and were not executed on PostgreSQL. T10–T13 had no reality pass.

| Finding | Confidence | Basis |
|---|---|---|
| B1 | High | grep hits only in contract/index; T11.md:1647-1700 read; `repository.py:4797-4800` read |
| B2 | High | T11.md:915-945 and :1395-1450 read; spec l.188-196, l.232-240 read; T11's own review note |
| B3 | High | T14.md:10-22 and :3815-3835 read; contract.md:151; zero-hit grep |
| B4 | High (mechanism) / Moderate (frequency) | T11.md:1296-1375 and :1500-1535 read |
| M1 | High (mechanism) / Moderate (likelihood: needs one renew error during a long lock wait) | T11.md:1085-1270, :872 read |
| M2 | Moderate | T04.md:1636-1710, :1746-1763 read; starvation is reasoned |
| M3 | Moderate | `lifecycle.py:664-750`, `repository.py:4786-4843`, `:5780-5790`, `sqlite_authority.py:45-62` read; the end-to-end wedge is reasoned, not run |
| M4 | High | T14.md:1588-1630; contract.md:360-366 |
| M5 | High | grep counts |
| M6 | High | App.tsx:398-406; T01.md:1101; T15.md:806-825 |
| m1 | High | measured in the venv |
| m6–m8 | High | direct line reads |

Reviewer confidence aggregation: reality-a High, reality-b High, reality-d High, architecture Moderate-High, quality High (F3 Moderate), systems High on mechanism / Moderate on severity, redteam-raise High (F1, F2, F4, F7 confirmed; F3, F5, F6 probable), redteam-cas High on the closed attacks / Moderate on F2–F6 (probable). Reality-c: **Insufficient Data** (missing).

## Risk Assessment

**Implementation Risk:** High. Executing as written ships a cluster-capacity leak (B1), an audit-integrity regression that the tests pin (B2), healthy turns cancelled on skewed clocks (B3), and relabelled public errors (B4).
**Reversibility:** Difficult for B2 (lost audit rows cannot be recovered after the fact) and Moderate for B1 (leaked rows need an operator hard-delete). The rest are Easy, as code or plan edits.

| Risk | Severity | Likelihood | Mitigation |
|---|---|---|---|
| Archived-session zombies exhaust D3 capacity and starve the reaper | Critical | Likely over time | B1 |
| A provider call is made and charged with no audit row after a tail cancel | Critical | Likely (every late Stop, deploy or lease loss) | B2 |
| A skewed browser cancels healthy turns | High | Possible | B3 |
| A transient DB error relabels a 422/403/502 as 503, or loops to a 504 | High | Possible | B4 |
| One renew hiccup restarts the instance | High | Possible | M1 |
| Other users' runs push healthy jobs to 504 | High | Possible under load | M2 |
| A session is wedged behind a never-ending 409 until restart (PG) | High | Possible | M3 |

## Information Gaps

1. [ ] **reality-c (T10–T13) is missing.** No anchor or symbol pass covers T10–T13. Incidentally verified here, all matching: T10.md:1700-1740, :2374-2386, :2478-2486; T11.md:872, :915-945, :1085-1270, :1296-1375, :1395-1450, :1500-1535, :1640-1700; T13.md:940-976. The rest of T10–T13 (the moved-body diffs in T10, the poll/cancel routes in T12, the caller migration in T13) is unverified for anchor drift. Run a reality pass on T10–T13 before execution.
2. [ ] **Nothing was executed on PostgreSQL.** M2 and M3 (and B1's starvation amplifier) are reasoned from the plan code plus the live repository code.
3. [ ] From systems: the full identity/ledger lock graph inside `run_composer_turn` (R05 relevance) was not traced.
4. [ ] From systems: the ECS/ACA stop-timeout values actually in use (they size M8's operator note), and whether `docs/runbooks/staging-session-db-recreation.md` documents the hard cutover for ECS/ACA.
5. [ ] From reality-a: the tails of T02, T03 and T04 were read partially. The trust-tier fingerprint strings were not recomputed.
6. [ ] From reality-b: the unread second halves of T06–T09 and the PG-only test files were not re-grounded.
7. [ ] From reality-d: the Bicep/ACA line citations in T15 Step 11+ were not independently re-verified.
8. [ ] From quality: whether the pre-existing `resyncAfterSettledComposeTurn` incidentally renders "session not found" (this would soften M4's UX impact, but not the missing test).
9. [ ] From architecture: no end-to-end admit → claim → start → complete test across all three authorities was confirmed. The reviewer expects it in T16/T18.
10. [ ] Synthesis-specific: M3's fix touches SOL recovery policy, so the owner must choose between an owner-side settle and an accepted residual.

## Caveats & Required Follow-ups

### Before relying on this synthesis
- [ ] Re-run `/review-plan` (or at least a reality pass on T10–T13 plus a re-check of B1–B4) after the fixes land.
- [ ] Get owner decisions on B1's fallback route (only if the fix is declined) and on M3.
- [ ] Confirm B3's field name (`deadline_remaining_ms`) with the owner before T02, T12 and T14 all bind it.

### Assumptions made
- contract.md's Adopted-deviations table and the index's Review Focus list are binding acceptance criteria (by their own text).
- HEAD `53b7d4344` stands in for the plan's base (reality-a and reality-b showed the cited paths are unchanged).

### Limitations
- This synthesis re-verified findings against the plan text and the tree but did not execute any plan step or test.
- Legal, compliance and accessibility are outside the four lenses.
