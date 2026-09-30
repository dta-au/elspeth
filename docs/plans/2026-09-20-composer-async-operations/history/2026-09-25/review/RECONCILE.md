# Cross-task reconciliation (T00-T18)

Editor pass over `contract.md` and `tasks/T00.md`..`T18.md` (tree `release/0.8.1` @ `ea5fa50d5`). The contract file
itself was not edited; where a task deviates from it, the adopted form is listed in section 2 and every task now uses it.

## 1. Fixes applied

### Would have turned a task red at execution time

1. **T11 reaper passed the wrong envelope to `settle_lost`.** `_reap_running` built
   `request_cancelled_error(...)` when a cancel marker was set and passed it as `failure=`; Task 4's `settle_lost`
   requires `failure` to be `worker_lost` (`_require_failure(failure, _WORKER_LOST)`) and substitutes the cancel
   envelope itself through the new `cancelled_failure` keyword. Fixed: always `failure=worker_lost_error(...)`,
   `cancelled_failure=request_cancelled_error`; the `reaped_running` slog reads `failure_code` from the returned row.
2. **T11 never settled expired/cancelled queued rows.** T11 Deviation 5 said the authority has no queued lister and
   relied on the claim path, but Task 4's `claim_next` never returns a deadline-passed or cancel-marked row, so those
   rows would have stayed `queued` forever (and counted against D3 capacity). Fixed: `reap_once` now runs
   `list_expired_queued` → `settle_unstarted(None, ...)` with `deadline_expired_error` or `request_cancelled_error`
   by `cancel_requested_at`, before the running sweep; Deviation 5 rewritten as RECONCILED; Consumes lists
   `list_expired_queued`; Task 0 APPENDIX A row W5 updated.
3. **T11 `_settle_running` ignored Task 6's cancel-outranks rule.** Task 6's `fail_composer_async_operation` refuses a
   non-cancel failure while a cancel marker is set (raises `ComposerOperationCancelledDuringTurn`) and says the owner
   must retry with `request_cancelled`. T11 did not catch it, and its test
   `test_external_cancel_racing_the_cancel_marker_settles_worker_lost_and_keeps_unwinding` expected `worker_lost`
   with a committed marker (contradicting spec §4). Fixed: `_settle_running` retries with `request_cancelled_error`;
   test renamed `..._settles_the_committed_cancel_and_keeps_unwinding` and asserts `request_cancelled`.
4. **T11 duplicated Task 10's `_ComposerOperationCancel`.** T11 said "add the marker block after the imports" and
   "add `request_lease` if absent"; T10 already creates the marker (two kinds) and the keyword. Fixed: T11 now
   EXTENDS T10's block in place (the `shutdown` kind, docstring, one singleton, one entry in T10's read-only
   `MappingProxyType` identity map; a first draft of this fix swapped it for a mutable `Final[dict]`, which the
   immutability lints would flag, and was corrected), leaves `request_lease` alone, and anchors the arm
   rewrite on T10's actual `operation_cancel = _composer_operation_cancel_of(exc)` line (T10's arm has no
   `heartbeat_cancel = ...` line) keeping T10's `labels.cancelled_progress_task_name`. Without this, T10's rule would
   have labelled `shutdown` as a user Stop.
5. **Strict `SendMessageRequest` had no owner (commit-order violation).** Contract: Task 10. T10 (D-T10-3) pushed it
   to T13; T02 said T13; T13 said T10 and made no edit. Meanwhile T11 (`_turn_input` reads `request.operation_id`,
   tests build `SendMessageRequest(operation_id=...)` and hash it; T02's codec refuses a non-strict DTO) and T12
   (`_stand_in_submit`, `_admit_send`, the IDOR row) needed it before T13. Fixed: D-T10-3 withdrawn; new **T10 Step 1b**
   (own commit) makes `SendMessageRequest` strict (T02's `_StrictSendShape` shape plus the visible-content validator)
   and keeps T10's measured blocker honest by moving the still-synchronous `send_message` route onto a transitional
   `LegacySendMessageRequest` (today's coercing body verbatim), re-exported from `_helpers.py`; `test_schemas.py`
   `TestSendMessageRequest` retargeted to the legacy class; T02's promised live-DTO identity test added there. T10
   constructors (`_running_job`, the kind-mismatch test, the PG test) now pass `operation_id`; T10's conditional
   `RecomposeRequest` creation removed (T02 owns it); Step 14 pathspec drops `schemas.py`. **T13** gains Step 8b
   (delete `LegacySendMessageRequest`, retarget `TestSendMessageRequest` to the strict class), its Step 1 precondition
   checks for the legacy class, its Step 4 red expectation is corrected (the `compose_message` variant now fails fast
   with 422 on the legacy body, not by timeout), its "Not a deviation" paragraph and commit pathspec/message updated.
6. **T10 D6 edit list predated Task 1.** T01 added the required keyword `commit_timeout_seconds` to
   `settle_pipeline_proposal_under_compose_lock` (line 273 becomes `timeout_seconds=commit_timeout_seconds`) and a
   `commit_timeout_seconds=request.app.state.settings.composer_timeout_seconds` argument in
   `settle_auto_commit_intent`. T10 told the executor to prefix-swap `:273` (nothing to swap) and missed the
   `request.app...` read, which its own "no `request.` survives" grep would then have failed on. Fixed: `:273` left
   unchanged; the `settle_auto_commit_intent` argument becomes `services.settings.composer_timeout_seconds`;
   `commit_timeout_seconds: float` added to T10's Produces signature; note that anchors after `:185` shift by ~+7
   (match by quoted text); Step 5 run now includes T01's three settlement pins; the Review note on the auto-commit
   timeout corrected.
7. **T08 turned Task 1's pin red.** T01's `test_freeform_service_surfaces_keep_the_compose_budget[freeform-planner]`
   asserts one `_timeout_seconds` node in `_plan_and_stage_empty_pipeline`; T08 Step 7g replaces it with the
   `budget_seconds` parameter (0 nodes). T01's Review notes assign the retarget to T08, which omitted the file. Fixed:
   T08 Step 9i drops that parameter case and adds
   `test_freeform_planner_wall_clock_is_the_resolved_turn_budget` (no sync cap, no `_timeout_seconds`,
   `PlannerModelConfig(timeout_seconds=budget_seconds)`), with a mutation control; the file joins T08's Files list,
   the Step 10 run, ruff lists, `git add` and the commit pathspec (12 → 13 paths).
8. **T12 helper passed an `asyncio.Event` to the worker.** T11 Deviation 1 (measured) makes `instance_draining` an
   exact `threading.Event` and the worker raises `TypeError` otherwise; `install_composer_async_worker` passed
   `asyncio.Event()`, which would have broken every T12/T13/T16 route test. Fixed: `threading.Event()`,
   `import threading` replaces the now-unused `import asyncio` in that module; Consumes line notes the type.
9. **T10 adds a ninth writer to Task 4's exact manifest.** `record_composer_operation_user_message_on_connection`
   lives in Task 4's module; T04's `test_composer_async_operation_authority_manifest_is_exact` pins
   `len(live) == len(reviewed_writers) == 8`. Fixed: T10 Step 12 adds the function to that test's `writers` dict and
   changes 8 → 9, with a control.
10. **Commits of untracked files.** `git commit -- <path>` refuses a path git does not know (measured in a scratch
    repo: `error: pathspec 'new.txt' did not match any file(s) known to git`, exit 1; exit 0 after `git add --`).
    T07 (new `test_composer_request_lifecycle.py`) and both T10 commits (new `composer_app_services.py`,
    `test_composer_turn.py`, `composer_turn.py`) had no `git add`. Fixed: exact-path `git add --` before each.
11. **T13 named the wrong Task 0 file.** T00 creates `test_composer_async_gate2_baseline.py`; T13 said it replaces
    `test_composer_async_operations.py`. Fixed: T13 `git rm`s the baseline in Step 2, creates
    `test_composer_async_operations.py`, checks the right path in Step 1, and commits the deletion by pathspec.

### Adopted-deviation ripples (names reconciled to the producing task)

12. **`admit` has no `rate_limit` (T04 D-4a).** Removed `rate_limit=` and the `_no_rate_limit` helpers from T05
    (`_admit`, Consumes), T06 (`_running_operation`), T10 (`_running_job`, PG test), T11 (`_admit`), T12 (three
    sites). T13's own conflicting deviation (keep `admit(rate_limit=<no-op>)`, charge in the route) replaced by a
    RECONCILED note; `accept_composer_operation` now calls
    `admit_composer_operation(authority, rate_limit=partial(rate_limiter.check, user.user_id), ...)`
    (`_rate_limit_charged_before_admission` and the route's own `authority.get` deleted; `functools.partial`
    imported; module docstring step 2 updated; Consumes updated). T16 Consumes notes the class patch is still hit.
    T00 APPENDIX A row A12 owner text updated.
13. **`request_cancel` / `settle_lost` take `cancelled_failure` (T04 D-4b).** Added at every call: T05 (2 tests, via
    `_cancelled_failure` imported from T04's test module, since Task 9 is not built yet), T06 (2 tests, via a new
    `_cancelled_failure_factory` shaped as the Protocol, also imported by T06's PG module), T11 (5 test sites; reaper),
    T12 (the cancel route, with `request_cancelled_error` imported into the router; Consumes and the deviation text).
14. **`ComposerOperationFenceLost(*, session_id, operation_id, attempt=None)` (T02).** Positional-string and
    `(claim)` constructions fixed in T05 (raise site, Consumes, Step 0 check now exercises the keyword form and stops
    instead of improvising), T06 (`_composer_terminal_cas_loss`, Consumes, Review note) and T10
    (`record_composer_operation_user_message_on_connection`). T10's `ComposerOperationCancelledDuringTurn("...")`
    fixed to Task 6's `(claim)` constructor.

### Plan hygiene (checks 3 and 5)

15. **Preamble P0 missing.** T01, T03-T05, T07-T17 run `$ELSPETH_WORKTREE`/`$BASE` blocks without defining them (only
    T00, T02, T06, T18 restate P0; T10 also used an undefined `$PY`). An unset variable makes `cd ""` a no-op and a
    bare import resolve to the main checkout. Fixed: each of those 15 tasks now opens with a P0 block and the rule
    "prefix every command block with it" (T10's also sets `PY=`); T10's `export ELSPETH_WORKTREE=<...>` placeholder
    replaced. All pytest/mypy/lints invocations already use the two-root `PYTHONPATH` form and log-then-`exit=$?`
    (scanned: the only lines without `PYTHONPATH=` on the same line are continuation lines of multi-line commands
    whose previous line sets it, plus T08's `MYPYPATH` mypy call). Every commit is by pathspec.
16. **Placeholders.** `as above` in T14 (App.test.tsx mock keys) and T06 (fail CAS predicate) replaced with the
    literal text. Remaining `…` inside code fences are T13's migration-shape templates (applied per legacy site, each
    listed in its Appendix A) and T18's usage strings, not missing code.
17. **Stale notes.** T02 notes now say Task 10 (not 13) makes `SendMessageRequest` strict and that the
    `ComposerOperationFenceLost` call shapes are reconciled; T11 review notes on queued expiry and the zombie row
    updated; T11 gains the `composer_operation.reap_skipped_inactive_session` event (see residual R1).
18. **T15 left Task 1's property test stale.** T01 assigns T15 the rewrite of
    `test_every_accepted_config_keeps_its_sync_budget` (true only while the coupling check stands). It stays green (its
    strategy still draws only funded configs) but its docstring would state a false premise. Fixed: T15 Step 2 rewrites
    that docstring and turns `test_sync_budget_caps_a_compose_budget_above_the_transport_safe_value`'s `model_copy`
    workaround into plain `_settings(composer_timeout_seconds=600.0)` construction; Files list updated.
19. **T00 APPENDIX A rows.** Rows A12 (charge path) and W5 (queued expiry through
    `list_expired_queued` → `settle_unstarted(None, ...)`) now name the reconciled mechanism.

20. **T11 and T13 disagreed on an unmarked `CancelledError` (a composer that raises it itself).** T13 (Consumes,
    Shape R-cancel, S6, Review note 1) expected `failed` 499 `request_cancelled` with `cancelled`/`client_cancelled`
    progress; T11's `_terminal_failure` settles every cancel that is not the worker's `cancel_requested` marker as
    `worker_lost` 503 and its arm publishes `failed`/`service_setup_failed` (finding #28: in a detached worker there is
    no client socket, and `stop()` must not read as a user Stop). Adopted T11 (measured rationale, and it keeps the
    Stop semantics on the cancel endpoint). Fixed T13: Consumes bullet, Shape R-cancel (status 503, D14 worker_lost
    body, snapshot `failed`/`service_setup_failed`, terminal counter `failed`, LLM-call sidecar assertions kept), the
    S6 example body, Review note 1, and the cutover commit's "Changed expectations". Affects the 5 `R-cancel+F` sites
    and S6; Shape K (Stop through the cancel endpoint) keeps 499 `request_cancelled` / `client_cancelled`. This is a
    visible behaviour decision for the operator (section 2 row).

## 2. Contract deviations adopted

| Name / area | Contract | Adopted (owner) | Why |
|---|---|---|---|
| `admit` | takes `rate_limit: Callable[[], None]` | no `rate_limit`; new coroutine `admit_composer_operation(authority, *, rate_limit: Callable[[], Awaitable[None]], ...)` does get → charge-if-absent → `admit` (T04 D-4a) | both limiters are coroutines and the shared one commits on its own connection (measured `rate_limit.py:100,152-155`, `rate_limit_authority.py:85`); T13's competing no-op-hook deviation withdrawn in its favour |
| `request_cancel`, `settle_lost` | no factory arg | required `cancelled_failure: ComposerOperationCancelledFailure` (T04 D-4b) | the D14 cancel body with the row's `request_id` must be built inside the locked tx; Task 9 lands later |
| authority read API | no queued lister | `list_expired_queued(*, limit)` (T04 D-4c); T11's contrary Deviation 5 withdrawn | `claim_next` skips deadline-passed/cancel-marked rows, so only this lister can find them |
| `claim_next` | one batch tx with SKIP LOCKED | discovery read + one `locked_session_transaction` per candidate (T04 D-4d) | lock order advisory → row, as every other writer |
| Tier-1 decoration of the four exceptions | decorated | plain `RuntimeError` subclasses (T02) | `@tier_1_error` raises `PermissionError` outside `elspeth.contracts/engine/core` (`tier_registry.py:73,149,161`, measured) |
| `ComposerOperationError` home | `schemas.py` or re-export | `composer_operations.py`, `BaseModel` with `_StrictResponse`'s config, re-bound in `schemas` (T02) | import cycle the other way |
| record invariants | table list | + queued ⇒ `user_message_id` NULL; completed ⇒ `started_at` set and `cancel_requested_at` NULL; six time-ordering checks; canonical result JSON (T02, mirrored by T03) | forced by other contract clauses |
| `ComposerOperationFenceLost` ctor | unspecified | `(*, session_id, operation_id, attempt=None)` (T02); applied in T05/T06/T10 | facts only, reaper holds no claim |
| `ComposerOperationCancelledDuringTurn` ctor | unspecified | `(claim)` (T06); applied in T10 | |
| `ComposerOperationAssistantWrite` | content, tool_calls, composition_state_id, writer_principal, raw | `(message_id: UUID, content, raw_content, composition_state_id)` (T06, T10 D-T10-2) | cohort tool rows need the parent id before insert |
| `run_composer_turn` | `(services, turn, *, lease, running)` | `+ request_lease: ComposerRequestLease` (T10 D-T10-1 = T11 Dev 3, identical) | progress sink needs the worker-owned CRL |
| `_ComposerOperationCancel` | worker module, 2 kinds | `composer_turn.py`, 3 kinds incl. `shutdown` (T10 creates, T11 extends; T11 Dev 2) | import direction; `stop()` must not read as a user Stop |
| unmarked `CancelledError` out of a detached turn | not specified | server fault: 503 `worker_lost`, progress `failed`/`service_setup_failed` (T11); T13's 499 `request_cancelled` expectation withdrawn | no client socket exists; only the cancel endpoint's marker is a user Stop |
| `instance_draining` | `asyncio.Event` | `threading.Event` (T11 Dev 1); T12 helper fixed to match | readiness refuses anything else (`readiness.py:663-664`, measured) |
| cancelled-turn terminal write | turn's arm | worker job frame after sidecar join (T11 Dev 4) | the heartbeat 503 is produced after the arm re-raises |
| lifespan placement | after `orphan_task` | construct + startup `reap_once` after `recover()`, `start()` after the orphan callback (T11 Dev 6) | a failing startup reap would leak the orphan task |
| strict `SendMessageRequest` | Task 10 | Task 10 (contract kept; T10's D-T10-3 withdrawn), plus transitional `LegacySendMessageRequest` for the still-sync route, deleted by T13 | see fix 5 |
| settlement signature (D6) | `services`, `user_id` | as contract, plus T01's required `commit_timeout_seconds: float` kept | T01 names the commit budget per caller |
| poll/cancel `PermissionError` arms | 401/404 translation | none (T12 deviation) | the contracted authority methods take no actor and never raise those classes; ownership re-checked per call |
| client deadline | `useComposer.ts` | `sessionStore` poll loop (T14 deviation) | `deadline_at` exists only on the poll body and must survive reload/A→B→A |
| epoch bump | "Task 15 (last)" in §Table | Task 17 (68), per the contract's own task table | contract self-inconsistency (T00 Review note 2) |
| spec §2 D8 line | "Task 16 edits the spec" | Task 17 edits it (Commit A) | T16 is test-only |

## 3. Residual issues not fixed

R1. **W18 capacity leak for a `running` row in an archived session (T00 Review note 11).** After its owner dies, the
    reaper's COMPOSE `acquire` raises `SessionOperationFenceLost(OWNER_INACTIVE)`, so the row stays `running` and
    `count_nonterminal` keeps charging it against `composer_async_max_queued_operations`. D7's `ON DELETE CASCADE`
    covers only a hard delete. Not fixed: it needs a new authority write path (design, not reconciliation). Proposal:
    Task 4 adds `settle_lost_inactive_session(*, session_id, operation_id, failure, cancelled_failure)`, an unfenced
    settle guarded inside the locked tx by `sessions.archived_at IS NOT NULL` (the archive already proves the fence is
    dead), called by T11's `OWNER_INACTIVE` arm; alternatively exclude archived sessions from `count_nonterminal`.
    Mitigation applied: T11 now logs `composer_operation.reap_skipped_inactive_session` on every sweep.
R2. **Pre-202 `AsyncWorkerAdmissionTimeoutError` stays a bare 500 (T00 Review note 6, APPENDIX A A17).** Task 13's
    admission does not map it; the worker path maps it to 503 (Task 9). A product decision, left as the plan states.
R3. **Contract text is stale** for every row of section 2; it was not edited (the brief allows task-file edits). The
    plan assembler should either amend `contract.md` or ship section 2 beside it.
R4. **Not executed.** No task step was run: every fix above is a text edit checked by reading the producing task.
    Semantics that only execution can prove remain the executors' (for example whether T06's composite actually
    raises `ComposerOperationCancelledDuringTurn` in the T11 re-settle path, whether T13's migrated `test_routes.py`
    sites all pass, and T10 Step 1b's claim that exactly 9 `TestSendMessageRequest` constructors exist, measured
    here by reading `tests/unit/web/sessions/test_schemas.py:79-127` at `ea5fa50d5`).
R5. **Gate 5 "cross-instance poll" (frontend).** The SPA cannot observe instances; T14 covers it as poll-by-id
    reattach (`resumes polling the custody operation on reload…`, `reattaches on A→B→A…`, `attaches to the session's
    active operation on 409 composer_operation_active (another tab)`), and the server side is T16
    (`test_job_accepted_on_one_instance_is_claimed_by_a_peer_scan_loop_and_polled_from_both`,
    `test_instance_b_polls_and_cancels_a_turn_running_on_instance_a`). Accepted as coverage, noted here.
R6. **T11 test harness types.** T11's `_harness` passes the test app's `plugin_snapshot_factory` (typed on
    `UserIdentity`) as `plugin_snapshot_for_user_id` (typed on `str`). It works because the test factory
    (`lambda _user: snapshot`, `test_routes.py:945`) ignores its argument; tests are outside the mypy gate. Left.
R7. **T04 note: "Task 5 must refuse a claimed job whose deadline has passed."** T05 does not check the deadline in
    the composite. Covered downstream instead: T11 settles `deadline_expired` on the claim path
    (`deadline_at <= updated_at`) and right after start (`deadline_at - started_at <= 0`), before any provider call.

### Coverage checks (check 4), for the record

- APPENDIX A: every row's owner exists and handles it; the owners changed by this pass are A12 (T04's coroutine), W5
  (T11 `reap_once` via `list_expired_queued`), W18 (T11 reaper, now with the corrected `settle_lost` call; archived
  case = R1), T18/X3 (T11 `_settle_running` re-settle), X8/Review note 5 (T11
  `test_stop_settles_a_running_turn_as_worker_lost_and_joins_it`), T20/Review note 4 (T10 joins auto-title before the
  terminal).
- Spec §4 crash windows → T16: lost 202 `test_lost_acknowledgement_retry_replays_the_one_row_and_one_provider_turn`;
  202-before-claim `test_job_accepted_on_one_instance_is_claimed_by_a_peer_scan_loop_and_polled_from_both`; dies
  before running `test_worker_killed_after_claim_before_running_leaves_a_reclaimable_job_with_no_side_effects`; dies
  after running `test_worker_killed_while_running_is_settled_worker_lost_by_a_peer_without_provider_replay`,
  `test_committed_cancel_decides_the_reaped_outcome_of_a_dead_owner`, `test_partitioned_owner_is_reaped_and_cannot_write_after_it_heals`;
  races `test_cancel_and_completion_settle_exactly_one_terminal[cancel_then_complete|complete_then_cancel|concurrent]`.
- Spec gates: 1 → T03 schema tests + T03/T04/T05/T06 PG modules; 2 → T13
  `test_post_is_accepted_before_the_held_provider_and_poll_returns_the_exact_former_result[compose_message|compose_recompose]`
  and `test_only_the_three_guided_routes_keep_the_request_scoped_compose_dependency` (+ T18 short-idle proxy
  acceptance); 3 → T16; 4 → T11 (`test_local_cancel_reaches_the_worker_turn_task_and_settles_request_cancelled`
  reads `cancelling()` in the job task, `test_cancelled_turn_persists_its_llm_call_audit_before_the_terminal_write`,
  the heartbeat twins, plus T07's 56-test lifecycle twin); 5 → T14 `sessionStore.composerOperations.test.ts`
  (lost acknowledgement, reattach, Stop, client deadline, stale transcript 409, exact response application, poll
  outage); 6 → T18 (`full-suite-gate.sh --execute --detach` with all stages, serial testcontainer).
- Commit order: a forward-reference scan (every name produced by task N searched in tasks < N) finds only prose
  mentions (e.g. T04 docstrings naming Task 9's `request_cancelled_error`, T02 notes naming Task 5/6 classes); no
  earlier task's code or test depends on a later task. Instrument control: the scan flags the `admit_composer_operation`
  mention this pass added to T00's APPENDIX A (T00 < T04), so it does detect forward names.

## 4. Anchor spot-check

All at `ea5fa50d5` (the contract's `d479eb2b4` + 13 commits; T00 measured no change under the cited web paths).
Every anchor below was read with `sed -n`; all match the cited text (35 distinct file:line anchors).

| Anchor (cited by) | Found |
|---|---|
| `_helpers.py:2788-2807` `_verify_session_ownership` (T12, T13) | match, 404 `Session not found` on archive/user/provider |
| `_helpers.py:2449` `_track_compose_inflight` docstring (T13) | match: "wired into ``send_message`` and ``/recompose``" |
| `_helpers.py:398-399` `request.state.composer_request_lease` (T10) | match |
| `_helpers.py:3064` `_handle_convergence_error` (T00, T08) | match |
| `_helpers.py:311` `_SessionComposeLockRegistry`, `:2197` `_failure_log_request_id`, `:2395` `_composer_heartbeat_cancel_of` (T11, T13) | match (T13 cites `_failure_log_request_id` at `:2195-2204`, def at 2197: within range) |
| `app.py:1876` `instance_draining = web_instance_membership.draining` (T11) | match |
| `app.py:900-925` recover → `orphan_task` → callback → `yield` (T11 Dev 6) | match |
| `app.py:2247` `handle_http_exception` (T00 APPENDIX A) | match |
| `membership_lifecycle.py:66` `threading.Event()` (T11) | match |
| `readiness.py:663-664` exact `threading.Event` check (T11) | match |
| `async_workers.py:14-24` pool bounds, `:194` `run_sync_in_worker` (T04, T13) | match |
| `tier_registry.py:73`, `:149`, `:161` (T02) | match |
| `lifecycle.py:426` `SessionOperationLease.adopt` (T05, T10) | match (decorator at 425) |
| `lifecycle.py:734` `_close` (T00) | match |
| `repository.py:4786-4790` acquire body, archived/missing (T05, T00 `R:4790`) | match |
| `pipeline_settlement.py:173-178` signature, `:273` `timeout_seconds=request.app.state.settings.composer_timeout_seconds`, `:447-452`, `:473-478` (T01, T10) | match |
| `proposals.py:318-324` settlement call (T01, T10) | match |
| `test_routes.py:3863-3866` IDOR module tuple (contract, T12) | match |
| `test_routes.py:945` `plugin_snapshot_factory = lambda _user: snapshot` (T10 note) | match |
| `composer_progress_authority.py:178-265` `cleanup_expired` (T04 D-4d) | match (def at 169, body from 178) |
| `locking.py:279-283` `locked_session_transaction` (T04) | match |
| `execution/service.py:2663-2700` `_signal_shutdown_on_operation_loss` (contract, T11) | match (def at 2662) |
| `rate_limit.py:100`, `:152-155` both `check` coroutines (T04, T13) | match |
| `rate_limit_authority.py:85` own `engine.begin()` (T04) | match |
| `protocol.py:620`, `:636` guided exception precedents (T02) | match |
| `messages.py:118` `body: SendMessageRequest`, `:186`, `:191` 404s, `:1176` `auto_title_task.result()` (T10, T00) | match |
| `compose.py:145`, `:147` recompose 400/409 (T00) | match |
| `service.py:6324` `_insert_chat_message(..., message_id=None)` (T06, T10) | match |
| `composer/protocol.py:1506` `ComposerSettings` (contract, T01) | match |
| `sessions/protocol.py:3875` `SessionOperationAuthority` (contract, T05) | match (decorator at 3874) |
| `composer/service.py:2719`, `:4163`, `:5382`, `:4099`, `:4459`, `:4873` timeout reads (T01, T08) | match (2719 is the read inside `_run_one_turn_for_test`, def at 2670) |
| `schemas.py:54-91` `_StrictResponse`/`_RequestModel`/`_GuidedOperationRequest`, `:145-160` `SendMessageRequest`, `:420-436` `RevertStateRequest._parse_state_id` (T02, T10) | match |
| `test_schemas.py:79-127` `TestSendMessageRequest`, `:175` extra-keys row (T10 Step 1b, T13 Step 8b) | match |
| `test_no_chain_authoring_path.py:18-29` `_RETIRED_CONTRACTS` (T12 gate; checked for the new name `LegacySendMessageRequest`, which reaches OpenAPI from T10 to T13) | no retired token is a substring of the name |
| `messages.py:41` `SendMessageRequest,` in the `from .._helpers import (...)` block; `_helpers.py:234`, `:3857` re-export (T10 Step 1b) | match: the route imports the model through `_helpers` |
