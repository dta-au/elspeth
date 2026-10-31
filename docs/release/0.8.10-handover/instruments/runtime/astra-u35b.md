**Changes requested.** The main blocker is the lease-release ticket: a known handoff refusal permanently prevents the required cancellation settlement or retry.

Reviewed only `d7ad18596..42d2d7e56`. HEAD remained `42d2d7e563db7e577da2845e6c2cb9a557a31d8b`; final status was clean and detached. No files changed and no tests or scanners ran.

1. **P1 — Handoff seals required work before ownership actually ends.**
   [coordination/lifecycle.py:1584](.claude/worktrees/chk-rt-42d2d7e56/src/elspeth/web/coordination/lifecycle.py:1584) calls `prepare_lease_release()` before the transaction. That permanently sets the release barrier ([required_work.py:1544](.claude/worktrees/chk-rt-42d2d7e56/src/elspeth/web/required_work.py:1544)).

   If Stop wins, the transaction raises `ComposerOperationCancelledDuringTurn` without writing. The lease records `UNKNOWN`, but the prescribed `request_cancelled` settlement cannot reserve its SQL ticket: all further reservations are forbidden. CAS refusal likewise cannot proceed through the specified retry/handover path. Later `close()` only rejoins the failed task, leaving the fence unreleased.

   **Fix:** keep known-refusal handling and ambiguous-commit readback within the retained finish lifecycle, using drain-admissible accounting that permits those branches. Seal release accounting when ownership actually ends. Do not reopen it after an ambiguous commit without authoritative readback. The existing ticket provides drain admission and completion checks, but its irreversible barrier is unsuitable at this point.

2. **P2 — Terminal settlement does not validate the supplied claim token or attempt.**
   [sessions/continuation.py:821](.claude/worktrees/chk-rt-42d2d7e56/src/elspeth/web/sessions/continuation.py:821) proves the context’s live fence, then selects rows without comparing `claim.claim_token` or `claim.attempt`. The terminal writes use those loaded rows. Replacing either claim field while retaining the current context therefore still permits settlement, contrary to §2.7’s exact-owner requirement.

   **Fix:** compare the complete supplied claim/fence tuple before writing and retain it in the terminal CAS predicates. Extend the existing stale-terminal test with token and attempt mismatches and unchanged-row assertions.

   The readback has a related incomplete check: [continuation.py:1138](.claude/worktrees/chk-rt-42d2d7e56/src/elspeth/web/sessions/continuation.py:1138) compares only `session_operation_id`, so a mismatched epoch/token/attempt can receive `None` or a receipt. Validate the complete historical binding there too.

3. **P2 — Consuming a handoff never marks the lease closed.**
   [coordination/lifecycle.py:1591](.claude/worktrees/chk-rt-42d2d7e56/src/elspeth/web/coordination/lifecycle.py:1591) sets `CONSUMED` but never sets `_closed`, including exception paths. Subsequent `close()` only joins the retained task, so `lease.closed` remains false permanently. This disagrees with ordinary close and archive consumption; Composer observation checks that property.

   **Fix:** set `_closed = True` in the finalized handoff lifecycle’s `finally`. Add successful-consume-plus-close and failure-path tests. The retained task already prevents a second release.

4. **P2 — Cut coverage bypasses the production selector.**
   [test_continuation_cut_coverage.py:114](.claude/worktrees/chk-rt-42d2d7e56/tests/unit/web/composer/test_continuation_cut_coverage.py:114) calls `handoff_cut_allowance` directly. Removing its integration from `next_continuation_action_policy` leaves these assertions passing while actual Advisor, End and later-Compose selection fails. The classifier cases similarly call only the helper.

   **Fix:** exercise `next_continuation_action_policy` with valid selected data for every required cut. Keep helper tests as supplementary checks. The reviewed tests also never invoke `consume_continuation_handoff`, leaving findings 1 and 3 uncovered.

5. **P3 — Renewal test accepts no renewal.**
   [test_composer_continuation_handover.py:167](.claude/worktrees/chk-rt-42d2d7e56/tests/unit/web/coordination/test_composer_continuation_handover.py:167) accepts `renewed == first`; removing the renewal UPDATE would still satisfy it.

   **Fix:** establish a shorter but live expiry, renew, and assert a strict extension consistent with the configured lease.

6. **P3 — R8 test covers generations, not different segment sequences.**
   [test_composer_continuation_handover.py:249](.claude/worktrees/chk-rt-42d2d7e56/tests/unit/web/coordination/test_composer_continuation_handover.py:249) uses handover, so both saves remain in sequence 1. An incorrect aggregation restricted to the current sequence would pass.

   **Fix:** add a real yield between saves and verify both sequences contribute. Preserve the existing case for multiple-attempt coverage.

Checked and found correct by source inspection:

- **Yield/handover transactions:** yield uses the full owner tuple, checkpoint/head CAS, `(sequence+1, generation+1)`, a handoff checkpoint, and atomic fence release. Handover preserves sequence, advances generation and emits recovery’s exact event bytes. Yield leaves the parent `running/continuation_queued`. False mutation proofs become fence loss.
- **Stop and claims:** A1’s marker/revision update is atomic; A2 makes unclaimed queued/interrupted segments eligible while preserving live claims. Stop refuses live-owner yield and handover. Renew/release target segment claims and preserve NULL parent tokens.
- **Lease ordering:** owned children join before renewal stops; renewal loss prevents handoff. The single retained finish task prevents duplicate release, including after waiter cancellation.
- **R8/R9:** production aggregation includes every segment-attempt fence and chooses the latest saved version; `model_request` is supported. Slice rollover preserves request-wide counters.
- **Readback:** successor progress and recovery-driven generation advances are tolerated, with interrupted-attempt history checked.
- **Locking:** the relevant paths share session serialization through SQLite process/thread locks or PostgreSQL advisory transaction locks. I found no additional dialect-specific race. The PostgreSQL re-exports exercise serial scenarios, not contention proofs.
- **Gates:** removed rows have replacement identities; exact bindings, bidirectional inventory comparison and negative controls remain. The quota caller re-pin accompanies replay changes while preserving fresh-insert quota ordering. No guarded paths or exports were removed. Scanner derivation and numeric fingerprint correctness remain **unverified** under the read-only-command restriction.
- **Both R5 findings are correct owned-union discrimination**, at [composer_operation_authority.py:2141](.claude/worktrees/chk-rt-42d2d7e56/src/elspeth/web/coordination/composer_operation_authority.py:2141) and [continuation.py:824](.claude/worktrees/chk-rt-42d2d7e56/src/elspeth/web/sessions/continuation.py:824). They follow ADR-032’s concrete internal-type rule and existing V1 usage; neither warrants changing the code or adding a suppression.

**R3 remains a deferred dependency:** peer recovery still lacks Stop selection. That predates this diff, and the design assigns Stop-before-recovery to U-39’s reaper ([runtime design:1103](.claude/worktrees/chk-rt-42d2d7e56/docs/architecture/design-notes/0.8.10-continuation-runtime-design.md:1103)). U-35b alone does not establish R3.
