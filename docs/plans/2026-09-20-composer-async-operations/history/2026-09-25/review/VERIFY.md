# Fix verification — composer async operations plan (2026-09-25)

Scope: `docs/plans/2026-09-20-composer-async-operations.md` (index), `T00..T18.md`, `contract.md`.
Inputs: `SYNTHESIS.md` (B1-B4, M1-M8, m1-m9), `codex.md` (C1-C7), contract "Review-pass decisions".
Method: for each item, grep the owning task(s) for the named symbol or test and read the edit and the test body.
Test-first order was checked against step numbers (the test step comes before the implementation step). Line numbers
are after this pass's edits. All file paths are relative to `docs/plans/2026-09-20-composer-async-operations/`
unless stated otherwise.

**Totals over the 28 synthesis + codex ids (B1-B4, M1-M8, m1-m9, C1-C7; C4 and C6 are verified with B1 and M8 but
counted as their own ids): 25 RESOLVED, 3 PARTIAL (B2, B4, M7), 0 MISSING.** The contract's Review-pass rows map onto
these ids. F-B2/C2 and F-B4 are PARTIAL through B2 and B4; every other row is RESOLVED.

## Blockers

| Id | Verdict | Where the fix is | Test-first evidence |
|---|---|---|---|
| B1 / C4 | RESOLVED | T04 Produces :168; impl `settle_lost_inactive_session` :2713 (Step 5). T11 `_reap_running` OWNER_INACTIVE arm → `_reap_inactive_session`, MISSING → ALREADY_TERMINAL :2835-2845 (Step 5). T00 W18 :904, Review note 11 :708-721. T16 Review note 8 :1811-1824. The RECONCILE/"Unresolved W18" text is gone from T11 | T04 `_assert_inactive_session_settle` :707, `_assert_inactive_session_settle_publishes_a_committed_cancel` :751. Tests at :1380 and :1386 (SQLite) and :1839 and :1845 (PG), all in Steps 2-3. T11 `test_reaper_settles_a_running_row_of_an_archived_session` :1165 and `test_reaper_survives_a_session_deleted_between_discovery_and_acquire` :1199 (Step 2). Mutation control T11 :2912 |
| B2 / F-B2/C2 | **PARTIAL** | T10 `ComposerTurnObservation` :2057; `observation.compose_result = result` :2543; `persist_cancelled_turn_audit` :2267 (audit_only, joined, idempotent through `audit_cohort_durable`); composite joined :3035-3048. T11 `_finish_running` :2500 joins the persist before every failure terminal; `_write_failure` :2596. T06 flipped test and PG twin. T16 race test cohort assert + new PG test :1332 | T10 :1072, :1109, :1152, :1302; T11 :844, :891; T06 :1074 (flipped); T16 :1272, :1332, with Step 5b control |
| B3 / F-B3 | RESOLVED | T02 field `deadline_remaining_ms: int = Field(ge=0)`, required (:111, class at ~:1457). T12 `composer_operation_deadline_remaining_ms` :1311, routes :1394 and :1440 (DB clock via `database_now`). T14 deviation :21-30 (tighten-only `Math.min`, `performance.now()`); no `Date.parse(deadline_at)` timing left (only in the Step 15 mutation control :5139) | T02 :873, :892; T12 :660, :693, :723 (Step 1, before Step 7); T14 `it.each` skew test :3162-3175 (±10 min, in step), with Step 15 controls :5134-5143 |
| B4 / F-B4 | **PARTIAL** | T11 bounded retry of the terminal fail write (`terminal_write_retrying` :2573); `_start` catch-all → `settle_unstarted`; adopt catch-all → fenced fail write. T00 W6 :791, W9 :794, S5 :907 | T11 :927 (transient retry), :959 (start defect → 500, not 504), :987 (adopt defect → 500). The status re-read between attempts is in `_settle_running`'s loop: each iteration re-reads with `authority.get` and returns on `status != "running"` (:2537-2554); backoff at :2578 |

**B2 gap (defect introduced by the fix).** T10.md:67-68 and T11.md:3262 say the worker calls
`persist_cancelled_turn_audit` on *every* non-success exit. T10 sets `audit_cohort_durable` only after the success
composite (:3048) and inside the persist (:2322, :2347). The post-compose state-persistence runtime-preflight arm
(T10.md:2898-2933) rebuilds the error with `llm_calls=result.llm_calls`, and `_handle_runtime_preflight_failure`
persists that cohort. I confirmed this on the live tree: `_helpers.py` `_handle_runtime_preflight_failure` (def :3416)
calls `_persist_turn_audit_cohort(..., exc.llm_calls, ...)` at about :3646-3650. The handler then raises
HTTPException 500, and the frame persists the same `llm_calls` a second time, so the audit gets duplicate
`llm_call_audit` rows. Owner: T10. The fix is to set `observation.audit_cohort_durable = True` after that handler
returns, with a failing-first test that asserts one audit row per call on that arm. T11 flagged this; T10 did not take it.

**B4 gap (measured limit).** The real `SessionOperationLease.adopt` releases the minted context before it re-raises
(`src/elspeth/web/coordination/lifecycle.py:208` `_raise_adopt_failure_after_release`, called at :448, :490, :532 and
:576; I read the source). After that release, the fenced fail write and `settle_own_lapsed` are refused, and the
reaper settles 503 `worker_lost`. There is no 504 loop, but the promised `operation_failed` 500 does not happen.
T11's adopt test (:987) monkeypatches an adopt that does *not* release, so it covers only the unreleased case.
T11 Review note :3263 records the limit. T00 W6 (:791) and W9 (:794) still promise `operation_failed` for every adopt
defect. Owners: T00 (add the caveat), plus a decision on whether to add a T04 settle for this case (no shared
signature for one exists) or to accept the 503 as a residual.

## Majors

| Id | Verdict | Where | Test |
|---|---|---|---|
| M1 | RESOLVED | T11 `_renew_claim` transient retry (`claim_renewal_retrying` :2176); `_spawn_or_supersede` :2017 | T11 :1022, :1060 |
| M2 / F-M2 | RESOLVED | T04 `_claim_candidates` fence filter; Produces :200-201; Gates "Index shape" | T04 `_assert_fence_blocked_sessions_do_not_starve_a_startable_job` :672; :1356 SQLite, :1833 PG |
| M3 / F-M3 | RESOLVED | T04 `settle_own_lapsed` :2795 (owner-id `ValueError` guard); T11 `_settle_own_lapsed` :2628-2648 from the fenced-out path and the reaper's own-instance conflict :2820-2831; T16 PG test | T04 `_assert_own_lapsed_settle` :773, :1392, :1398, :1851, :1857; T11 :1092, :1128; T16 :1361 + Step 5b. The residual (next job waits for membership lapse) is recorded in T04 Review notes |
| M4 | RESOLVED | T14 `ComposerOperationNotFound` :1073; client fns :1762, :1791, :1809; store `deactivateMissingSession` (13d2) | T14 store tests (Step 11): `it("stops polling, clears custody and takes today's session-not-found path…")` :3023, `it("never resubmits into a missing session when the reload probe answers Session not found")` :3049, contrast `it("keeps the session and renders the missing-operation copy…")` :3067. Client tests are in Step 2 (the `it.each` blocks at :292/:346, `isComposerOperationNotFound` :371). Step 15 control :5144 |
| M5 | RESOLVED | T11: 17 `capture_logs` uses; every event the synthesis named is asserted, plus `_assert_logs_are_redacted` :401 | `reaped_running` + `audit_may_be_incomplete=True` :807-808; the event census is in the grep output below |
| M6 | RESOLVED | T14 App.tsx guided ceiling reads `composer_sync_timeout_seconds` (:85-87, :194, :3667-3691); T15 Step 9c sentence rewritten (Review-pass :1263-1278) | T14 Step 11: `it("adopts the transport-safe sync budget, not the compose budget, as the guided abort ceiling (M6)")` :3677 (fixture `composer_sync_timeout_seconds: 270` :3691), `it("stays unready when status carries the compose budget but no sync budget…")` :3725; Step 15 control. T15 is docs-only |
| M7 | **PARTIAL** | T00 P11 :811, P12 :812 reclassified TERMINAL; P15 :800 names the survivor (`_require_actor_owns_session`) | **Missing:** T00 P15 assigns T11 an ownership-change test (settles `(404, {"detail":"Session not found"})`, no provider call). T11 has no such test; `test_archive_that_wins_before_start` :1379 is W2 (before start), not P15. T10 still has the duplicate predicate at :2384, which P15 says T10 drops. T11 Review note :3261 was aligned by this pass (see Edits) |
| M8 / C6 | RESOLVED | T11 `_claim_and_spawn` re-checks `_stopping` after a shielded `claim_next` and releases claims (`claims_released_on_stop` :2014); `stop()` drains late jobs. T17 CHANGELOG sentence :391-394, docs-test fragments :292, Commit B paragraph :1070 | T11 :1220; T17 docs-test fragments (red first) |

## Minors

| Id | Verdict | Where |
|---|---|---|
| m1 / F-m1 | RESOLVED | T02 `COMPOSER_OPERATION_REQUEST_JSON_MAX_LENGTH = 393_334` (:82, :1009), tests :504, :518, vocabulary pin :404-405. T03 CHECK `<= 393334` :790, pinned to the constant :406. T04 `_MAX_REQUEST_JSON_CHARS = COMPOSER_OPERATION_REQUEST_JSON_MAX_LENGTH` :1996, reject test :865 and at-bound test :882. T00 A20 :780. Residual (T02 Review note ~:1702-1703): nothing pins the **live** T10 `SendMessageRequest` width to the constant. T10 has zero hits for the constant, so this is a T10 owner item |
| m2 | RESOLVED | T11 `post_terminal_exception`; test :1398 |
| m3 | RESOLVED | T10 `_join_auto_title` :2212 cancellation-safe; tests :1274, :1302; mutation control (viii) |
| m4 | RESOLVED | T04 `_assert_one_admit_takes_the_active_slot` :626; tests :1362 (SQLite), :1801 (PG) |
| m5 / F-m5 | RESOLVED | T01 `le=16` :65/:391, test :318. T04 :1998 and T17 env row already read 16. Contract §Settings still prints `le=256` (contract owner) |
| m6 | RESOLVED | T00 Step 2 anchors :91-94 (212, 291-310/299, 312-320/314, 319). I re-measured them with the plan's own `grep -nF` on `scripts/worktree-cleanup.sh`: 212, 299, 314, 319 |
| m7 | RESOLVED | T02 :68 `hashing:32,66,89` in order, matching `src/elspeth/contracts/hashing.py` (32, 66, 89) |
| m8 | RESOLVED | T18 :1067 `deploy/compose/nginx.conf:25-37`; `location / {` is at :25 and the file has 37 lines |
| m9 | RESOLVED (by the index owner, outside my scope) | index :95 "No interim merge between T10 and T13." |

## Codex items not already covered above

| Id | Verdict | Where |
|---|---|---|
| C1 | RESOLVED (core). The side effect is under B2 | Same edits and tests as B2; the CAS-loss cohort is proved by T11 :891 and T16 :1332 |
| C2 | RESOLVED | T06 `require_no_committed_composer_cancel_on_connection` :2395, `audit_only` threaded (:2480, :2540, :2573, :2678, :2712), request_cancelled terminal passes (`audit_only=(failure.failure_code == "request_cancelled")` :307). Tests :1128, :1174, :1209, :1256, PG :3242; Step 10b mutation controls |
| C3 / F-C3 | RESOLVED | T04 `renew_claim -> ComposerOperationRecord` :2386, tests :1295, :1326. T11 `_acquire_compose_lock` :2097, `_settle_stopped_wait` :2120, test :1259 |
| C5 / F-C5 | RESOLVED | T10 `ComposerBudgetAnchor` :2001, `ComposerTurnDeadlineExpired` :2028, check :2369-2371; tests :1171, :1186, :1204, :1232. T11 anchor :2357-2359, same float to `budget_seconds` :2394, catch by class :2428; tests :1306, :1326 |
| C7 / F-C7 | RESOLVED (soft cap accepted by the contract) | T04 `admit` docstring :2171; tests :1366 (SQLite exact), :1808 (PG bound) |

## Shared-signature cross-check (T02/T04/T06/T10/T11/T12/T14/T16)

| Name | Producer | Consumers | Result |
|---|---|---|---|
| `deadline_remaining_ms: int` (ge=0, required) | T02 :111 | T12 :130-133, :1311-1354; T14 :128-130 decoder (9 keys); T16 :49, `_stable` :984; T18 :1835 | consistent; produced (T02) before it is consumed |
| `renew_claim(claim) -> ComposerOperationRecord` | T04 :159, :2386 | T11 :37, :2133, :2162 | consistent |
| `settle_lost_inactive_session(*, session_id, operation_id, failure, cancelled_failure)` | T04 :168, :2713 | T11 `_reap_inactive_session` kwargs identical | consistent |
| `settle_own_lapsed(*, session_id, operation_id, owner_instance_id, failure, cancelled_failure)` | T04 :174, :2795 | T11 :2636-2642 (passes `self._owner_instance_id`); T16 :62 | consistent |
| `audit_only: bool = False` | T06 (service, Protocol, `_session_composer_mutation_transaction`, five writers) | T10 Step 11(a2) :1808-1815 (skipped if T06 already did it); T10 persist :2333; T11 :107 preflight grep | consistent. T11's "assumption" (T11 :46) that the request_cancelled fail write commits under a committed cancel is satisfied by T06 :305-308 / :2259 |
| `ComposerTurnObservation` (`compose_base_state_id`, `compose_result`, `audit_cohort_durable`) | T10 :2057 | T11 :50, :2313, :2410; T16 behaviour only | consistent (names and types) |
| `persist_cancelled_turn_audit(services, running, observation) -> None` | T10 :68, :2267 | T11 :53, :2519, :2611; T16 :1630 (mutation) | consistent |
| `ComposerBudgetAnchor(remaining_at_running_seconds: float, monotonic_at_running: float)` | T10 :2001 | T11 :51, :2357-2359 | consistent |
| `COMPOSER_OPERATION_REQUEST_JSON_MAX_LENGTH` = 393 334 | T02 :1009 | T03 :48, :790 (literal) + pin :406; T04 :108, :1996; T00 A20 | consistent. Contract §Table (131072) and the contract F-m1 row ("393 230", content only) are the contract owner's |
| `run_composer_turn(..., request_lease, observation, budget_anchor)` | T10 :2355-2363 | T11 :2403-2411 | consistent |

Commit order (contract task table 2→3→4→5→6→10→11→12→13→14→16): every name above is produced before it is
consumed. T06's flipped test (:1074) simulates the owner's audit-only write directly and does not import T10's
`persist_cancelled_turn_audit`, so there is no forward reference.

## Fixer-reported items: verdicts

- **T11 B4 adopt arm (partial).** Confirmed on the live `lifecycle.py`. B4 is PARTIAL; see above.
- **T11 M8 deploy note belongs to T17.** It is present (T17 :391-394, :292, :1070).
- **T11 → T10 double persist.** Confirmed on the live `_helpers.py`. It is a real defect and makes B2 PARTIAL. Owner: T10.
- **T11 assumption on T06.** T06 has now landed it (T06 :305-308, :2259-2271). No action.
- **T04 → T10 manifest count.** Fixed by this pass (see Edits).
- **T04 F-m1 value set in T02/T03.** Confirmed consistent.
- **T12 → T04 scanner hazard (`conn.exec_driver_sql(self._clock_sql)`).** **False alarm; T04 needs no action.**
  I measured it with the gate's own `scan_production_writers`, anchored at a full `src/` copy in scratch:
  - T04's shape (`self._clock_sql = _DATABASE_CLOCK_SQL[...]` imported from `membership_authority`, read as
    `conn.exec_driver_sql(self._clock_sql)`) gives only the `write_connection` row.
  - Positive control: an opaque parameter, `conn.exec_driver_sql(statement)`, gives an extra
    `<unresolved-session-write> unknown_opaque` row.
  - Live precedent: `websocket_ticket_authority.py` has the identical shape and scans to 5 rows with 0 unresolved.
    `identity_authority.py` and `membership_authority.py` together scan to 68 rows with 0 unresolved.
  - Confound: a scratch anchor without the source tree makes the import unresolvable, and that produces the
    unresolved row. Keep T12's own rule (the router reads the clock through `database_now(conn)`).
- **T12 → T14 stale `T12.md:1061-1095` / "8 keys".** Already rebound: T14 :128 says 9 keys, and no stale T12
  citation remains in T14.
- **T12 terminal-row `deadline_remaining_ms` (computed, not 0).** Owner decision; left open.
- **T14 contract propagation (tighten-only wording; `| ComposerOperationNotFound`).** Contract owner.
- **T14 → T15 M6 sentence.** Done (T15 Review-pass).
- **T14 no-banner session deactivation.** Owner (copy) decision.

## Edits made by this pass

1. `T10.md:3208`, `:3214` (Step 12). The manifest count now reads `10` → `11`, with "eleventh row", the insertion
   point "after the `settle_own_lapsed` entry", the control "`11 == 10`", and the pointer "T04.md Step 7; rows
   pasted in Step 9". It previously read `8` → `9`, "ninth writer", "after `settle_lost`", "T04.md Step 9".
2. `T04.md:217`, `:3068-3070`, `:3372-3373`. The stale `T10.md:2540-2546 still says ninth writer` references now
   point at T10 Step 12's "eleventh row" paragraph. These three were a content-keyed exact-string replacement in a
   Python one-off with a count==1 assertion per string, not the Edit tool.
3. `T02.md:~1705-1708` (F-m1 Review note). T04 no longer reads 131072; only contract §Table does (contract owner).
4. `T03.md:1477`. Same correction.
5. `T01.md:1148`. The two stale "256" mentions in T04 and T17 are recorded as already corrected; only contract
   §Settings remains.
6. `T11.md:3261`. The ownership-recheck note now follows T00 P15 (the survivor is `_require_actor_owns_session`;
   T10 drops its predicate) and states that the P15 test has not been written.

No behavioural code or test text was changed.

## Out of scope, left to their owners

- **Index (owner):** m9 is already present (index :95). Review Focus #4 (:59) names "T13 (server race)", but the
  server race test is T04's m4 (`test_concurrent_different_id_admits_race_for_the_active_slot`, T04 :1362/:1801).
- **Contract (owner), body text that the Review-pass table overrides but that still reads the old way:**
  - §Settings `le=256` (:91);
  - §Wire DTOs has no `deadline_remaining_ms` (:164-167);
  - §Table `length(request_json) <= 131072` (:188);
  - §Authority still shows `renew_claim -> None`, has no `settle_lost_inactive_session` or `settle_own_lapsed`, and
    `admit` still carries `rate_limit`;
  - the F-B3 row should say tighten-only (`min(existing, armed)`);
  - §Frontend `| null` should read `| ComposerOperationNotFound`, and the client-deadline sentence (:431) still
    says "server `deadline_at` + grace";
  - the F-m1 row says "393 230", but the constant is 393 334 (content plus UUID framing).
- **T13 (owner, low severity):** the fake-server status bodies (T13 :1764, :1827, :1952, :1994) have 8 keys and no
  `deadline_remaining_ms`. Today's consumers do not require it: the ACA `_OperationStatus` model at :1861 declares 3
  fields, and the live-route reads at :374/:390 validate a real response. So nothing fails, but the mocks no longer
  match the 9-key wire. T13 has no Review-pass section.
- **T10 residual introduced by F-B2/C2 (T10 :3432):** a typed-ladder handler (`_handle_convergence_error`,
  `_handle_plugin_crash`, `_handle_runtime_preflight_failure`, `_handle_planner_failure`) that meets a committed
  cancel at its first non-audit write raises `ComposerOperationCancelledDuringTurn` before its own cohort write. The
  exception-carried LLM evidence of that turn is then not persisted. T10 records this as not fixed; John should see it.
- **Owner decisions:** T12's terminal-row value; T14's missing-session copy; the B4 adopt released-context settle
  (a T04 signature, or an accepted 503 residual recorded in the contract).
