# Exceptions lane status (25 Oct)

> **RESUME TASK 0 (hung tests) is the release's FIRST priority, owned by u45-firstcompose plus one coordinator-assigned owner. It is NOT this lane's job.** See `.claude/lanes/release-0.8.10/RESUME-FIRST.md`. This lane resumes its own unit, under the guardrails: one pytest at a time, `-n 3` max, `timeout 2400`, both hang files `--ignore`d, no directory runs without a slot. Do not touch the fencing gate, the reserve-contract tests or the pytest config. Maintainer's order, 2026-10-25.

Branch codex/0.8.10-exceptions, tip 85f709968, rebased onto release/0.8.10-assembly d7ad18596 (preserve ref preserve/exceptions-pre-rebase-20261025 = edac53b88).
- 602830a86 U-18/F1 executor_unavailable: red 27 failed / green 331 passed (backend); frontend decoder red 1 / green 15. Re-run after rebase: 331 passed, exit 0.
- d411959d0 U-19/F2-F3 observation faults: red 16 failed / green 30 passed; api+stores vitest 1035 passed after rebase, exit 0.
- 85f709968 WIP: Sol U-20 part A (F5 carrier), UNREVIEWED, no red/green verification yet.
app.py overlap: my only app.py hunk is the 4-line generation handler body (~line 2587); target's only app.py change since e66fbd1f1 is the U-38 continuation_policy kwarg. No overlap, rebase was clean. U-20 part A touches tool_batch/service/continuation codecs, not app.py.
Fencing file: whole-file run does not finish (escape walk blocked on U-46); not run. Keyless set-diff, landing-rule (set-subset) evidence and U-20 B-F7 still owed.


## COLD START (paused 2026-10-25), coordinator-written

*Written by the coordinator from git, the lane's notes in `.claude/worktrees/exceptions-20261025/.claude/lanes/`, and COLD-START-REVIEW.md. The lane was stopped with TaskStop at about 09:31, during 3 parallel wide runs. It wrote no cold-start notes of its own. The tip and commit list above this section are STALE: `85f709968` was reset away and re-committed as `748d548db`.*

**Branch:** `codex/0.8.10-exceptions` @ `ddc1ec9e8`, 9 commits on d7ad18596 (9/0). Pre-rebase ref: `preserve/exceptions-pre-rebase-20261025` (`edac53b88`). Worktrees: `exceptions-20261025` (the branch), `exc-base-20261025` and `exc-f1-20261025` (detached, scratch). All clean.

| Commit | Unit | Author | Evidence (under `.claude/worktrees/exceptions-20261025/.claude/lanes/`) |
|---|---|---|---|
| `602830a86` | U-18/F1: executor unavailability is reported as `executor_unavailable` | Sol + lane | `f1-u18/` (red/green 27F->331P, 14 mutation controls). `sol-f1.md` points at `docs/release/0.8.10-f1-executor-unavailable-note.md`, which never existed in git; use `f1-u18/`. |
| `d411959d0` | U-19/F2-F3: observation faults get an explicit owner | Sol + lane | `f2-f3-20261025/` (16F->30P, M01-M08) |
| `748d548db` | U-20/F5 part A: retain nominal service failures through the tool batch | Sol (06:22, before the pause) | `u20-f5-a/report.md` (62 focused, 131 codec, each new test mutated) |
| `3035bfeeb` | U-20/F5 part B: a retained failure survives a required-publication failure | Sol, during the pause | `u20-part-b-20261025/` (9 tests, 4 mutants) |
| `fe9278445` | U-20/F4: Advisor hint admission failure reaches the operation owner | Sol, during the pause | `u20-f4-part-c/` (25 tests, 4 mutants; **one nominal R5 site needs adjudication**) |
| `ef4096ebb` | U-20/F6: safe provider rejection reason kept on both primary routes | Sol, during the pause | `f6-primary-rejection/` (67) |
| `c6a4e3f9f` | U-20/F7: ownership pins | Sol, during the pause | `u20-f7-part-e/` (35) |
| `9055b82a8` | U-19 test fix (Astra #4) | lane | commit body |
| `ddc1ec9e8` | F1 full-source guard digests re-derived (Astra #2, guards half only) | lane | commit body |

**The Astra branch review** (`exceptions/astra-branch.md`, static, over `d7ad18596..c6a4e3f9f`) raised 4 findings:
1. **MAJOR, OPEN, unowned (F5):** a P4 failure loses the unpersisted tool-evidence suffix (`service.py:3186`, `:2985`). Cleanup drains only LLM calls (`composer_turn.py:1920`). Fix: an owned evidence carrier for the unpersisted invocation suffix plus the publication disposition, consumed by failure cleanup. Test real recovery after both P4 failure exits, with exactly-once draining.
2. Minor, HALF FIXED: the guard digests are done (`ddc1ec9e8`), but the `tool_batch.py:1019` exact carrier model still omits `service_failure`.
3. **Minor, OPEN (F6):** `diagnostic_reference=None` on both primary rejection routes (`provider_gateway.py:755/814`), and the route test pins that omission (`test_primary_rejection_routes.py:88`). Fix: bind the stored provider-failure evidence reference once the audit identity exists, following the Advisor pattern.
4. Minor, FIXED (`9055b82a8`).

**Not done:** no Claude review of parts B-E; no keyless set-diff; no landing-rule pair chain; no PG; the fencing and reserve-contract files never finished (partial id lists in `exceptions/done.txt` and `f1-fencing-failed-ids.txt`). The runs the coordinator killed at about 09:25 were whole-file fencing runs.

**Next steps on resume (after Task 0 per RESUME-FIRST.md):**
1. Read `exceptions/astra-branch.md` and the five U-20 hand-off notes.
2. Fix Astra #1 (Major) and #3 test-first, plus the #2 carrier-model half. These are release defects, owned by this lane, and must be done before landing.
3. Get the F4 nominal R5 site adjudicated by an independent reviewer.
4. A Claude review of 3035bfeeb..c6a4e3f9f.
5. Landing-rule (id, signature) pair sets at each commit over a NAMED selection: one run at a time, `-n 3`, `timeout 2400`, both hang files `--ignore`d.
6. Keyless set-diff vs d7ad18596, and the whole-tree gates for the touched files.
7. Then hand over for landing: U-18/U-19 can land first, separately.

**Scratch copies, not work:** `exceptions/advisor_checkpoint.py.fixed` and `exceptions/d-fixed/src/` are copies of branch files. Delete them when the lane dir is retired.
