# Fix verification, second pass: composer async operations plan (2026-09-25)

Scope: `T00..T18.md` and `contract.md` in `docs/plans/2026-09-20-composer-async-operations/`, plus the index
`docs/plans/2026-09-20-composer-async-operations.md` for the scans. Inputs: `VERIFY.md` (B2, B4 and M7 were PARTIAL;
T12 terminal-row value and T13 mock wire were open). All paths below are relative to the plan directory. Line numbers
are after this pass's edits, which changed no line counts.

**Result: B2, B4, M7, T12 and T13 are all RESOLVED. Placeholder scan: 0 hits. User-home scan: 2 hits, both the
generic `/home/<user>` in T18 prose, not a user path. One name-consistency correction made (T11, two lines).**

## Verdicts

| Id | Verdict | Where the fix is | Failing-first test (step order) |
|---|---|---|---|
| B2 (VERIFY gap: double persist on runtime-preflight path 2) | RESOLVED | T10 path-2 arm sets `observation.audit_cohort_durable = True` at T10:3146, right after `_handle_runtime_preflight_failure` returns (comment :3140-3145), before the 500 raise (T10:3147). Live check: `_handle_runtime_preflight_failure`'s `_persist_turn_audit_cohort(..., exc.llm_calls, ...)` (`src/elspeth/web/sessions/routes/_helpers.py` ~:3643-3652) is unconditional, not under `if exc.partial_state is not None`, so the test's `partial_state=None` still reaches the cohort write. Every other handler arm sets it too: convergence :2747, plugin crash :2877, preflight path 1 :2923, the `_persist_llm_calls` arms :2777, :2813, :2849, :2995, :3020. Produces text T10:66 and the persist precedence rule T10:69 (compose result, else pending calls, never a union) | `test_post_compose_runtime_preflight_arm_persists_one_audit_row_per_call` T10:1185 (Step 8, :787; impl in Step 11, :1859). It asserts the flag, one row before and exactly one row after the frame's persist. Mutation control (ix) T10:3380: deleting the flag gives `len(rows) == 2`, the measured defect |
| B2 follow-on (VERIFY "T10 residual": a fenced handler lost its exception-carried calls) | RESOLVED (LLM calls). Residual kept: tool rows | New field `pending_exception_llm_calls`, set as the first statement of every typed arm: T10:2722, :2751, :2788, :2824, :2860, :2891, :2930, :2975, :3011, :3114. Read by `persist_cancelled_turn_audit` T10:2469-2471, :2536. Residual (exception-carried tool rows are not written by the frame) recorded at T10:3659 | `test_cancel_fenced_convergence_handler_leaves_its_exception_llm_calls_to_the_frame` T10:1242 (Step 8). Mutation control (x) T10:3381, with an instrument check that removes `audit_only=True` |
| B4 (released-context adopt defect) | RESOLVED (as an accepted residual) | Contract Review-pass row `B4-residual` contract.md:56. T00 W6 :805 and W9 :808 now scope the 500 promise to start-composite defects and unreleased adopt defects, and name the 503 residual. T00 W18 :918 lists the entry path. T00 Review note 12 :726-739, Review-pass :955-960. T11 Produces :80, Review note :3379, Review-pass :3402. Line citations `LC:208`, `:448`, `:490` checked against live `src/elspeth/web/coordination/lifecycle.py`: `_raise_adopt_failure_after_release` is defined at 208 and called from `adopt` at 448 and 490. The VERIFY.md calls at 532 and 576 are in `adopt_fork_child` (def :506), so leaving them out is correct | `test_adopt_defect_whose_context_adopt_released_is_reaped_worker_lost` T11:1036 (Step 2, :117; impl in Step 5, :1593). Its fake adopt calls the real `_raise_adopt_failure_after_release` (import T11:169), and the call signature matches live `lifecycle.py:208-214`. It asserts the row is still `running`, then `reap_once() == 1` gives `worker_lost_error(...)` 503, then `reap_once() == 0` and no provider call. The unreleased case stays at T11:1004. Mutation control (ix) T11:3033 |
| M7 (P15 ownership-change test and T10 predicate) | RESOLVED | T10 no longer re-checks ownership. `run_composer_turn` keeps only `get_session` and its missing-row 404 (T10:2545-2556). `grep -n "archived_at\|auth_provider_type" T10.md` hits only Review-note prose (:3682, :3731) and no code. The survivor is T11 `_require_actor_owns_session` :2571. `_run_turn` calls it at :2509, after adopt (it receives the adopted `lease`) and before `_composer_request_lifecycle` opens at :2510. Review notes: T10:3682, :3716, :3731; T11:81, :3377, :3401; T00 P15 :814, Review-pass :961-963 | `test_ownership_change_before_the_first_write_settles_session_not_found` T11:1475, parametrised `reassigned` / `auth_provider` (Step 2; impl in Step 5). Helper `_change_session_owner_by_sql` T11:381, which uses `ensure_test_identity` (live `tests/fixtures/identities.py:10`; the FK is real, live `models.py:494`). It asserts `("http_error", 404, {"detail": "Session not found"})`, no user row, no audit row, no provider call and no `start_fence_lost`. Mutation control (viii) T11:3032 |
| T12 terminal-row `deadline_remaining_ms` | RESOLVED: owner decision is 0 on terminal rows | T12 `_status_body` T12:1393-1398, `deadline_remaining_ms=0 if terminal else live_remaining_ms` :1425. Contract §Wire DTOs contract.md:168 "0 on terminal rows" | `test_terminal_rows_report_zero_deadline_remaining_ms_before_their_deadline` T12:725 (Step 1, :190; impl in Step 7, :1272). It has a positive control (the deadline is still in the DB future) and a queued control row that reads exactly 60 000 under the same patched clock. The red is described at T12:1555. Mutation control T12:1564-1565 |
| T13 mock wire (8 keys) | RESOLVED | All four fake status bodies now carry `"deadline_remaining_ms": 0`: T13:1770, :1834, :1960, :2003. Every one is terminal: the parametrised `_operation` (:1747) is only called with `completed` (default) or `failed` (:1802). Review note 11 T13:2348-2357, Review-pass :2361-2366 | Test data only; the Review-pass records that no failing-first step applies (no consumer reads the key). Accepted |

## Name-consistency checks

- **`pending_exception_llm_calls`.** 29 hits, all in T10: :7, :64, :69, :1073, :1227, :1268, :2194, :2216, :2431, :2469,
  :2471, :2536, the ten arm sites above, :3381, :3654, :3659, :3663, :3715, :3719, :3727. The spelling is identical
  everywhere. I also searched for variant spellings (`pending_exception`, `exception_llm_call`, `pending_llm_calls`,
  `pending_exception_calls`) across the plan, the index and the review lane. The only extra hits are the test name
  `..._leaves_its_exception_llm_calls_to_the_frame`, which is identical at T10:1242, :3640, :3658 and :3726. No other
  task cites the field. T10:3663 says T11 reads none of the fields, so T11 needs no code edit.
- **`test_ownership_change_before_the_first_write_settles_session_not_found`.** It is defined once, at T11:1475. It is
  cited with the identical name at T00:814, T00:962, T10:3682, :3716, :3731, and T11:81, :3032, :3377, :3401. There are
  no variant spellings (`grep -n "ownership_change\|ownership change"` returns only these).
- **Contract decision id for B4.** T11:3379 and :3402 cited `"B4 adopt released-context"`, but the contract row is
  `B4-residual` (contract.md:56). Corrected; see Edits.

## Scans

Instrument controls, run first on scratch files. The positive file had one line per pattern; the combined regex
matched 7 of 7 and the home regex 2 of 2. The negative file matched only `TBDX` (a substring match, which is accepted
for a scan). Real runs were over `T00.md`..`T18.md` and `contract.md` (20 files) and the index:

- **Placeholders** (`TBD|TODO|similar to Task|as above|add appropriate|implement later|fill in`, case-sensitive and
  `-i`): **0 hits**. `grep` exit=1 on the task files and exit=1 on the index.
- **User-home paths** (`/home/|/Users/`): **2 hits, both benign.**
  - `T18.md:129`: "The record carries no `/home/<user>` path: `write_record.py` replaces them".
  - `T18.md:1588`: "run on synthetic artefacts carrying `/home/<user>` strings and printed `home_paths=0`".

  Both are the literal placeholder `/home/<user>`. `scripts/branch-safety-check.sh:159` matches
  `/(home|Users)/[A-Za-z0-9_.-]+`, and `<` is outside that class, so neither line trips the gate. Neither is a real
  path. The index has 0 hits (exit=1). No real user-home path appears.

## Stale text left for owners (not name fixes, so not edited)

0. **contract.md:48 (F-B4 row)** still reads "Any unexpected exception from the start composite or from `adopt`
   settles `operation_failed` 500", with no qualifier. The `B4-residual` row at contract.md:56 narrows it. The
   Review-pass table is read as a whole and the later row governs, so B4 stays RESOLVED. But someone reading F-B4 on
   its own is misled. Owner: contract. Add "(except an adopt defect after adopt released its context; see B4-residual)".
   Item 3 below is the same gap seen from T00.
1. **T11:50 and T11:53 under-describe T10.** T11:50 says `audit_cohort_durable` "is set once the cohort is committed".
   T11:53 says the persist is "a no-op before `compose()` returned". Since B2-closure, the flag is also set after every
   ladder handler, and the persist writes `pending_exception_llm_calls` when `compose()` raised. T10:3663 names this.
   Owner: T11 (description only; T11 constructs `ComposerTurnObservation()` and reads no field).
2. **T11:3378 Review note is obsolete.** It says the flag "is set only after the success composite … The fix belongs
   there". T10 has now landed that fix (T10:3140-3145). T10:3663 asks the T11 owner to retire the note.
3. **T00:738-739 (Review note 12)** still says the contract F-B4 row "needs this caveat from the contract owner". The
   caveat now exists as contract row `B4-residual` (contract.md:56).
4. **T11:3379, last sentence**, still says "The caveat for the released case belongs in T00 (the T00 owner's edit)".
   T00 W6/W9/W18 and Review note 12 now carry it.
5. **Informational:** T02:114-117 describes `deadline_remaining_ms` as `max(0, deadline_at − database now)` and says
   the DTO "does not tie the value to `status`: a terminal row may carry 0 or a positive remainder". That is true of the
   DTO. T12's route rule (0 on terminal rows) is stricter and matches the contract (contract.md:168), so nothing
   conflicts. T16 `_stable` (T16:991-992) accepts 0.

## Edits made by this pass

1. `T11.md:3379`: `contract decision "B4 adopt released-context"` became `contract Review-pass decision \`B4-residual\``.
2. `T11.md:3402`: `(contract "B4 adopt released-context", accepted)` became `(contract \`B4-residual\`, accepted)`.

Both were made with the Edit tool; a re-grep for `B4 adopt released-context` over the plan and the index returns
exit=1. No behavioural code, test text or line count changed.

This report was then revised before hand-off. The B2 flag line was corrected from ":3140-3145" to :3146 (raise :3147).
The live `_helpers.py` cohort-write check and the M7 call-order check were added, along with stale item 0
(contract.md:48). None of these touched a plan file.
