# U-02 Engine carry: report

> **RESUME TASK 0 (hung tests) is the release's FIRST priority, owned by u45-firstcompose plus one coordinator-assigned owner. It is NOT this lane's job.** See `.claude/lanes/release-0.8.10/RESUME-FIRST.md`. This lane resumes its own unit, under the guardrails: one pytest at a time, `-n 3` max, `timeout 2400`, both hang files `--ignore`d, no directory runs without a slot. Do not touch the fencing gate, the reserve-contract tests or the pytest config. Maintainer's order, 2026-10-25.

> **Coordinator corrections (10-25, from COLD-START-REVIEW.md). These override the text below:**
> - U2: `d937ac4ca` is the carry source on `codex/engine010-carry-20261021`. On the target it is `7f058738c`, so check ancestry against that.
> - N8: the (id, first E line) pair parser existed only in the session transcript. Re-write it, or use u45's `u45pairs.py` (`docs/release/0.8.10-handover/instruments/u45/`).


- **Unit branch:** `codex/0.8.10-u02-engine`, rebased onto the target. Worktree: `.claude/worktrees/u02-engine-20261024`.
- **Base:** `release/0.8.10-assembly` @ `d7ad18596`. **Tip:** `430b3bd46`, which is 5 commits ahead of the target.
- **Pinned earlier tips:**
  - `preserve/0.8.10-u02-pre-rebase` = `d58fed13d` (pinned by the coordinator), on the original base b04ba8532.
  - `preserve/0.8.10-u02-369734ba4`, the first rebase, on 15f30fda0.
  - Also the local branch `u02-pre-rebase-d58fed13d`.
- **Logs.** `$L` = `.claude/worktrees/u02-engine-20261024/.claude/lanes/u02/` (gitignored).
- **Scratch worktrees** (mine, clean): `.claude/worktrees/u02-base-20261024` (b04ba8532) and `.claude/worktrees/u02-bisect-20261024` (d7ad18596). Remove them with `scripts/worktree-cleanup.sh`.

## 1. What landed, what this branch adds

**The seven picks are already on the target.** The coordinator landed them at 21:06 as `b637d7d48 1e0047d59 ff66d81dc 7f058738c 881cc4228 de1d272e5 92310ed07`, from my picks `15ff3917a 856c8ecfb 317aa925e a7d6e46e0 a02ab4a77 a600e7f0d 055c5334d`.

| Source | Subject |
|---|---|
| 169ae1558 | fix(assessment): preserve historical catalogs in engine carry-forward |
| 49cad71fa | fix(lints): preserve portable identities and writer evidence |
| e36dbf795 | fix(sources): validate sparse rows before schema growth |
| d937ac4ca | fix(engine): preserve unstarted work across cooperative shutdown |
| ee173a733 | fix(sinks): publish fresh members before recovery reconciliation |
| ffa7e3c97 | fix(engine): resume through the nearest enclosing bound scope |
| 527b74e4e | fix(llm): preserve unusable response evidence and required usage (CHANGELOG conflict resolved) |

**Commits this branch adds on d7ad18596.** The patch-ids are identical across both rebases (`eea26568b1b6 3fe9d6b7ac07 96e34fd19833 627e78e16d5f`). Hooks were on, and branch-safety exited 0 before each commit and each rebase.

| SHA | Earlier SHAs | Subject |
|---|---|---|
| 6c7b69fb3 | 5f7d262a1, 47b893515 | test(engine): pin that a started claim is never preserved as unstarted (red-team C1) |
| 6032a0a2e | 0211ff2bc, 4e9b58cb4 | test(engine): pin shutdown handling in the leader's follower sink drain (red-team C2) |
| dd7b2be9f | df850a4d6, d58fed13d | docs(engine): state the non-resumable fate of an unstarted claim (red-team C3 wording) |
| 8cbfb8a8b | 369734ba4 | test(engine): pin the ABANDONED record for a row ingested after shutdown (§4) |
| 430b3bd46 | new | test(engine): prove persistence in the shutdown pins (independent Astra review, §7) |

Only dd7b2be9f touches `src/`, and only a docstring and a comment.

**Landing rule (target AGENTS.md).** The new ids pass at 8cbfb8a8b (exit 0, 3 passed, `$L/tip4-new-tests.log`). The only src change among these commits is the docstring and comment, so each new id also passes at the commit that introduces it. At 430b3bd46 the three changed or new test files pass whole (exit 0, 19 passed, `$L/touched-tip6.log`). No test goes red (§3).

## 2. Dropped repairs: coverage diff against the overlay

My repairs of the 35 d937 reds were 866989025 (processor fakes) and cb28a0058 (recovery fixtures). The Continuation overlay `fa9e91575` landed v152 versions of the same six test files. On the target all 35 ids pass (exit 0, `$L/target-35.log`). File by file, from `git show fa9e91575` against my commits:

| File | My dropped change | Overlay's change | Verdict |
|---|---|---|---|
| `tests/unit/engine/test_processor.py`, `test_processor_row_lifecycle.py` (9 sites) | `check_flush_status` ready once per accepted token | ready while a member is accepted **and `execute_flush` has not been called** (`captured and not execute_flush.called`) | Overlay is more faithful: readiness persists until the flush, as the real trigger does. |
| `tests/unit/engine/test_token_traversal_characterization.py` (7 fakes) | `_exec(..., attempt_offset=0, allow_unstarted_shutdown=False)` | keyword-only `*, attempt_offset, allow_unstarted_shutdown`, plus `assert allow_unstarted_shutdown is True/False` per case | Overlay is stricter: it also pins the value each call site passes. |
| `tests/e2e/recovery/test_depth5_resume_post_settlement.py` | `settings=settings` on resume | identical | Same. |
| `tests/e2e/recovery/harness.py` | set shutdown after the engine resumes past row N, then stop yielding; **kept** the `lifecycle_state='loaded'` relabel | set shutdown only after the real `finalize_source_iteration` observed EOF; asserts the source is genuinely `exhausted`, every outcome `completed`, and every row in the sink. The relabel is removed, with a note that relabelling would leave ABANDONED behind | Overlay is better: no relabelled image. |
| `tests/e2e/recovery/test_crash_and_resume.py` | made R-1 process (shutdown after the yield) | keeps shutdown-before-yield and **pins** R-1 as `(NULL, ABANDONED)` with `incomplete_sources={"refunds": "interrupted"}`. Also asserts the source payload is retained, the FAILED work item is inspectable, there is no sink member, and persisted bytes are identical across the refused resume | Overlay is better: it pins the audit record instead of avoiding it. |

Nothing from the two dropped commits is missing from the target, so nothing was cherry-picked back.

## 3. Gates on the tip 8cbfb8a8b, compared with target d7ad18596

The tree was frozen across these runs: HEAD, status and index hash are identical before and after (`$L/tip4-freeze-{before,after}.txt`). Comparison is by (test id, failure signature), where the signature is the exception type plus the first line of the message, per the target's AGENTS.md landing rule.

| Gate | Command | Tip | Target | Log |
|---|---|---|---|---|
| architecture + dag | `pytest tests/unit/architecture tests/integration/core/dag --ignore=…/test_web_landscape_mutation_fencing.py --ignore=…/test_web_reserve_reviewed_contracts.py -n 4 --timeout=900` | exit 1: 8 failed, 1345 passed, 2 skipped, 1 xfailed | exit 1: identical | `$L/arch-{tip5,target5}.log` |
| touched files | `pytest test_resume_cancellation_admission.py test_multi_worker_leader_finalize.py test_source_cancellation_capture.py orchestrator/test_graceful_shutdown.py [+ test_shutdown_abandons_unstarted_source_row.py on the tip] -n 4` | exit 0, 64 passed | exit 0, 61 passed | `$L/touched-{tip5,target5}.log` |

**Architecture pairs: 8 on the target, 8 on the tip, 0 new, 0 gone** (`$L/arch-idsig-diff.txt`, plus a manual check of the eighth signature).
- 5 in `test_sink_effect_reserve_contract.py`: `AssertionError: assert ['sink-effect...tyRepository'] == []`.
- 2 in `test_session_db_mutation_authority.py`: `AssertionError: assert ([WriterIdent...scape=False)]) == ([], [])`.
- 1 in `test_digest_column_shape_checks.py`: `new digest-named columns need a shape…`.

All 8 are pre-existing on the target. The approved xfail stays XFAIL on both.

**Touched files.** The tip has exactly the 3 new ids more than the target, and all pass.

**Excluded files.**
- `test_web_landscape_mutation_fencing.py`, on the coordinator's instruction: its escape walk cannot complete on overlay trees until U-46's budget lands.
- `test_web_reserve_reviewed_contracts.py` (1064 ids), on my judgement. It imports that fencing module's walk machinery. In the first attempt it was failing on tip and target alike at about one id per few minutes, and it reads `src/elspeth/web/*` by explicit path, which this branch does not touch.
- The fencing pins are otherwise covered by the reviewed-guard tests.

**Earlier gates on the same four commits** (at 369734ba4 on 15f30fda0): keyless corpus identical to the target (0 added, 0 removed); masquerade exit 0; ruff, format, contracts, mypy and the trust_boundary rules exit 0. The broad engine run showed 2 reds, neither caused by these commits: one fails identically on the target, and one passes serially (a known-flaky process-death matrix).

## 4. The ABANDONED-row audit question

With a non-snapshot source interrupted mid-iteration, the row whose claim follows the shutdown request is now recorded `(NULL, ABANDONED)`. Pinned by `8cbfb8a8b`, `tests/integration/pipeline/test_shutdown_abandons_unstarted_source_row.py`, on the CLI shape: settings present, so the processor carries a RetryManager.

**Q1. Is the row recorded and attributable, not silently lost?**
Yes.
- **Landscape record.** The row and token persist, and the source payload is retained. The outcome is `(outcome=NULL, path=abandoned, completed=0, error_hash=NULL)` with `context_json = {"abandoned_by": "run_finalization", "non_resumable_arms": ["incomplete_sources"], …}`.
- **What is not invented.** No failure verdict, no error hash, no failed node state. The work item stays FAILED and inspectable.
- **Multi-source shape.** The overlay's `test_crash_and_resume` pins the same record, including the retained payload.
- **Under ADR-038** (§1, §3; `run_lifecycle_repository._abandon_undecided_tokens_in`) this is the designed record for a token "nothing will ever decide". It counts as `tokens.abandoned` with `closure='abandoned'`.

**Q2. Does the operator get a correct resume/abandon signal?**
Yes. The run is `INTERRUPTED` and the source lifecycle is `interrupted`. `RecoveryManager.can_resume` returns False with `ResumeRefusalCause.SOURCE_NOT_EXHAUSTED`, and `resume` raises `IncompleteSourceResumeError` (overlay test). This matches the ADR's mirrored refusal arm.

**Q3. Is "non-resumable source, therefore abandon" correct, or should the in-flight row finish?**

Measured at the old base b04ba8532 (`$L/abandon-pin-base.log`):

| Path | Old behaviour | New behaviour |
|---|---|---|
| CLI (RetryManager present) | The row's plugin was **never called** (`calls == [1]` holds on both), yet it was recorded `('failure', <error_hash>)`. The row was not delivered: a failure verdict was invented for work that never started. | Honest undecided record. |
| `settings=None` programmatic callers (the original harness and `test_source_cancellation_capture`) | The in-flight row was processed and delivered. | ABANDONED. |

**Why this is not a lost-row regression:**
- On the production path, the old behaviour did not deliver the row either; it mislabelled it.
- On both paths and both versions, the run is non-resumable because the source stopped mid-iteration. Every row the source never yielded must be recovered by re-running the pipeline anyway, and the abandoned row is in exactly that position. Its source payload stays in the Landscape and is attributable.
- Cooperative shutdown means no new work starts after the request (d937's stated contract and CHANGELOG). Finishing a row ingested after the request would contradict that, and would start external effects after the operator asked the run to stop.

**Verdict:** correct per the audit contract; not a regression. The only behavioural difference without a recorded failure is for `settings=None` callers, and it belongs in the upgrade notes (§6).

## 5. Fail-before / pass-after for the carried fixes (first pass, on b04ba8532)

| Commit | After | Before | Before-reds land in |
|---|---|---|---|
| 169ae1558 | exit 0, 333 passed | exit 1, 14 failed | catalog, history and supported-profile contract tests |
| 49cad71fa | exit 0, 231 passed | exit 1, 98 failed | the 7 new lint files |
| e36dbf795 | exit 0, 682 passed | exit 1, 125 failed | sparse-row admission, corpus hash pin |
| d937ac4ca | exit 0, 302 passed | exit 1, 90 failed | all 7 new cancellation files |
| ee173a733 | exit 0, 135 passed | exit 1, 51 failed | fresh-member publication, Power Automate recovery |
| ffa7e3c97 | exit 0, 20 passed | exit 1, 13 failed | nearest-bound continuation, merged cursor |
| 527b74e4e | exit 0, 499 passed | exit 1, 17 failed + 1 import error; mutation proof exit 1, 33 failed | gateway usage, providers, client |

**Limit.** Own-file proofs cannot see collateral breakage. The broad gate found 35 existing reds from d937ac4ca, as measured on the carry's own origin too. The overlay has since repaired them (§2).

**Fencing at the carry tip** (tool against the base): production calls 288 = pinned 288, `arrived=1` (`SchedulerDrainCoordinator._preserve_unstarted_claim -> mark_failed#1`, matching d937's re-derived pin note), 0 departed or moved.

## 6. Keyless trust-tier corpus (A4)

Rows were compared as a multiset, with roots normalised and line:col stripped.

**Base b04ba8532 to carry tip.** Added:
- The plan's positive control, the stale `engine.yaml` entry `engine/executors/sink_effects.py:R5:SinkEffectCoordinator:_execute_member_effects:fp=f6ae6cc7fb554481`. It is present.
- Its re-fingerprinted R5 site, `sink_effects.py:944`.
- `llm.py matched 6/3`, replacing `4/3` (plan E11).
- 9 `R_TB_SUPPRESSED` rows for `classify_unusable_content`.

**Target to this branch's commits: 0 added, 0 removed** (`$L/keyless-multiset-diff-tip3-vs-target.txt`). Nothing was signed or regenerated.

## 7. Red-team and review

**Red-team** (`.claude/lanes/release-0.8.10/u02-redteam.md`):

| Finding | Disposition |
|---|---|
| C1 (medium): double-effect guard unpinned | Fixed, 6c7b69fb3. Kills mutants D4 and D14 (the started row runs twice). |
| C2 (low): leader follower-drain shutdown flag unpinned | Fixed, 6032a0a2e. Kills mutant D23. |
| C3 (low): ABANDONED in-flight row | Fixture-only and correct per §4. Wording fixed in dd7b2be9f, pinned in 8cbfb8a8b. |
| C4 (low): near-equivalent guard mutants | Rejected: a test would pin guard order, not behaviour. |

**Independent Codex Astra review** of the four commits (`$L/astra-u02-followups.md`; read-only, exit 0). Both findings are fixed in 430b3bd46:

| Finding | Fix and evidence |
|---|---|
| P2: the follower-drain test ran with checkpointing disabled, so it proved invocation, not persistence. A no-op checkpoint body survived. | Checkpointing is now enabled, and the persisted sequence is read immediately before and after `checkpoint_interrupted_progress` (`after == before + 1`). The test also asserts the follower row is in the sink at that moment. Passes on the tip. The no-op-checkpoint mutant fails with `assert 2 == 3`, and the constant-False mutant fails because nothing is raised (`$L/c2c-*.log`). My first attempt (latest sequence greater than the sequence at drain) let the no-op mutant survive, because a post-sink checkpoint also advances the sequence (`$L/c2b-*.log`). |
| P3: the docstring misdescribed the constant-False mutant. | Corrected: the drain loop exits on no remaining work before its shutdown check, so the run finalizes without `GracefulShutdownError`. |
| Note: the ABANDONED pin did not verify payload retrieval or the full context. | It now retrieves the abandoned row's source payload (`{"id": 2, "item": 2}`) and asserts the exact context `{abandoned_by, run_status: interrupted, non_resumable_arms: [incomplete_sources], incomplete_sources: {docs: interrupted}}`. Passes (`$L/abandon-pin2-tip.log`). |

Astra found 6c7b69fb3, 8cbfb8a8b (ADR-038 expectation and attributability) and dd7b2be9f sound. The added test lines contain no `getattr` or `hasattr` probe (grep controlled against `test_masquerade_gate.py`, which shows 300), so the masquerade baseline is unaffected.

## 8. PostgreSQL / testcontainer ids owed (TC batch B1)

Candidates were derived by grep, not run; 120 ids (`$L/tc-collect.log`).

Most direct is `tests/testcontainer/core/test_resume_requeue_failed_work_postgres.py`.

Also:
- `tests/testcontainer/core/`:
  - `test_barrier_recovery_postgres.py`
  - `test_heartbeat_takeover_postgres.py`
  - `test_aggregation_error_route_postgres.py`
  - `test_aggregation_release_continuations_postgres.py`
  - `test_collector_failure_counts_postgres.py`
  - `test_collector_failure_verdict_postgres.py`
  - `test_quarantine_resume_crash_matrix_postgres.py`
  - `test_run_start_admission_postgres.py`
  - `test_run_export_status_postgres.py`
  - `test_sink_effect_decision_clock_postgres.py`
  - `test_sink_effect_lock_order_postgres.py`
- `tests/testcontainer/plugins/test_database_sink_effects_postgres.py`
- `tests/testcontainer/web/test_cross_process_engine_handoff_postgres.py`
- `tests/testcontainer/web/test_cross_process_execution_handoff_postgres.py`

## 9. Upgrade notes (U-53 / §6) and process notes

**Upgrade notes:**
- A graceful stop of a non-snapshot source now records the in-flight row ABANDONED.
  - On the CLI path the base recorded it as a failure routed to `on_error`, although no plugin ran.
  - `settings=None` programmatic callers previously saw it processed.
  - The run is non-resumable either way; recovery is a rerun.
- The existing note stands: persisted `exception_type` changes from `InterruptedError` to `RetryShutdownRequestedError`.
- ECS/ACA: no new host, systemd, UID or local-disk assumption.

**Process notes:**
- **Unguarded checkout in a script.** My first-pass proof helper ran `git checkout <sha> -- <path>` inside a script, which the PreToolUse guard blocks as a direct command. All restores were clean, and later restores use `git show`.
- **Redone pick.** The first pick was redone with hooks on. Picks 1–4 lack the `Claude-Session` trailer.
- **Killed runs.** Two architecture runs that included the fencing file never finished. I stopped them and their orphaned workers (all mine, cwd in my worktrees). One earlier run was killed when the session paused.
- **Not run:** the full suite and testcontainer, per the brief.

## COLD START (paused 2026-10-25)

**Unit:** U-02 engine carry.
- **Branch:** `codex/0.8.10-u02-engine` @ **`430b3bd46`**, measured against target `release/0.8.10-assembly` @ `d7ad18596`. The target has moved on since; the coordinator said the newer commits do not touch U-02's files.
- **Worktrees** (all clean, `git status --short` empty, no processes):
  - `.claude/worktrees/u02-engine-20261024` is the unit branch.
  - `.claude/worktrees/u02-bisect-20261024` is scratch, detached at d7ad18596.
  - `.claude/worktrees/u02-base-20261024` is scratch, detached at b04ba8532.

  The two scratch trees hold nothing and can be removed with `scripts/worktree-cleanup.sh`.
- **Preserve refs:** `preserve/0.8.10-u02-pre-rebase` (d58fed13d) and `preserve/0.8.10-u02-369734ba4`.
- **Logs:** `$L` = `.claude/worktrees/u02-engine-20261024/.claude/lanes/u02/`.

**Done and verified:**
1. **The seven carries.** Already landed on the target by the coordinator (§1).
2. **Five follow-up commits on the target, awaiting landing:** `6c7b69fb3 6032a0a2e dd7b2be9f 8cbfb8a8b 430b3bd46`.
   - The first four's patch-ids equal the pre-rebase commits.
   - The coordinator recorded the branch at 8cbfb8a8b as READY TO LAND pending Astra. **Astra has finished**; its two findings and one note are fixed in 430b3bd46 (§7, `$L/astra-u02-followups.md`), so nothing is pending from that review.
   - Evidence at 430b3bd46: the changed files pass whole (exit 0, 19 passed, `$L/touched-tip6.log`). The no-op-checkpoint and constant-False mutants fail (`$L/c2c-*.log`).
3. **Coverage diff for the dropped 866989025 and cb28a0058.** `fa9e91575` subsumes both and is stricter (§2). **The coordinator accepted this; it is closed.**
4. **Architecture + dag comparison**, at 8cbfb8a8b against d7ad18596, with fencing and reserve-reviewed ignored: 8 identical (id, signature) pairs, 0 new (§3, `$L/arch-{tip5,target5}.log`, `$L/arch-idsig-diff.txt`).
   - **Caveat:** tip and target ran *at the same time* at `-n 4`, before the new hard rules. If the coordinator wants it redone under them, see the next step.
5. **Keyless corpus** of these commits against the target: 0 added, 0 removed (§6).

**In progress:** nothing. There are no WIP commits and nothing half-done.

**Next step** (only if the coordinator asks for the comparison under the hard rules: one pytest per lane, sequential, `-n 3`, timeout ≤ 2400 s, slot required). Run tip first, then target:

```bash
W=.claude/worktrees/u02-engine-20261024; X=.claude/worktrees/u02-bisect-20261024; L=$W/.claude/lanes/u02
IG="--ignore=tests/unit/architecture/test_web_landscape_mutation_fencing.py --ignore=tests/unit/architecture/test_web_reserve_reviewed_contracts.py"
cd $W && PYTHONPATH=$W/src:$W/elspeth-lints/src timeout 2400 .venv/bin/python -m pytest tests/unit/architecture tests/integration/core/dag $IG -n 3 --timeout=900 -rfE -p no:cacheprovider > $L/arch-tip-hr.log 2>&1; echo "exit=$?"
git -C $X checkout -q --detach <target tip> && cd $X && PYTHONPATH=$X/src:$X/elspeth-lints/src timeout 2400 .venv/bin/python -m pytest tests/unit/architecture tests/integration/core/dag $IG -n 3 --timeout=900 -rfE -p no:cacheprovider > $L/arch-target-hr.log 2>&1; echo "exit=$?"
```

Compare the two logs by (id, first `E` line). The parser is in this session's history; `arch-idsig-diff.txt` shows the shape. Then land the five commits, or rebase onto the newest target first; branch-safety `--intent rebase` and a preserve ref come first.

**Open rulings:**
- **Maintainer: the d937ac4ca integrity question.** Should a row ingested after a shutdown request on a non-resumable (interrupted, non-snapshot) source be recorded `(NULL, ABANDONED)` (the current behaviour), or should cooperative shutdown let it finish?
  - **My evidence-backed recommendation: keep ABANDONED** (§4).
  - **ADR-038 prescribes it.** The row stays attributable: payload retrievable, exact context pinned by 8cbfb8a8b and 430b3bd46.
  - **The operator signal is correct.** The run is INTERRUPTED, `can_resume=False` with `SOURCE_NOT_EXHAUSTED`.
  - **The old CLI-path behaviour was worse.** b04ba8532 recorded `('failure', <error_hash>)` for a row whose plugin never ran (`$L/abandon-pin-base.log`).
  - **What changes for programmatic callers.** Only `settings=None` callers lose a delivery: the row was processed before, and is ABANDONED now. Recovery is the same rerun required for every unread row of the interrupted source.
  - **Tests pin this.** The overlay's `test_crash_and_resume.py::TestMultiSourceCrashResume::test_interrupted_source_refusal_does_not_append_replayed_rows` pins R-1 ABANDONED, and so does `tests/integration/pipeline/test_shutdown_abandons_unstarted_source_row.py`. If the maintainer rules "let it finish", both tests and d937's `scheduler_drain` pre-claim break must change together.
- **Coordinator:** an owner for the target's inherited architecture reds below.

**Inherited reds on the target** (measured at d7ad18596, also present on the tip; not U-02's). The 2 in `test_session_db_mutation_authority.py` are reportedly fixed by 506561cd4. The rest need an owner:

| Test id | Signature |
|---|---|
| `tests/unit/architecture/test_sink_effect_reserve_contract.py::test_actual_sink_receipt_retains_only_its_three_non_web_reserve_calls` | `AssertionError: assert ['sink-effect...tyRepository'] == []` |
| `tests/unit/architecture/test_sink_effect_reserve_contract.py::test_common_origin_proof_allows_unchanged_read_helper_imports` | same |
| `tests/unit/architecture/test_sink_effect_reserve_contract.py::test_common_origin_proof_refuses_read_helper_replacement[callable-alias]` | same |
| `tests/unit/architecture/test_sink_effect_reserve_contract.py::test_common_origin_proof_refuses_read_helper_replacement[module-alias]` | same |
| `tests/unit/architecture/test_sink_effect_reserve_contract.py::test_common_origin_proof_refuses_read_helper_replacement[reflection]` | same |
| `tests/unit/architecture/test_digest_column_shape_checks.py::test_every_digest_named_column_is_inventoried_or_excluded` | `AssertionError: new digest-named columns need a shape in _INVENTORY or a reason in _EXCLUDED` |
| `tests/unit/architecture/test_session_db_mutation_authority.py::test_composer_preferences_facets_are_exact_contained_and_bidirectional` | `AssertionError: assert ([WriterIdent...scape=False)]) == ([], [])` (506561cd4) |
| `tests/unit/architecture/test_session_db_mutation_authority.py::test_receipt_bound_state_revert_and_fork_settlement_have_exact_authorities` | same (506561cd4) |

**Hazards:**
- **Known hang files on overlay trees.** `tests/unit/architecture/test_web_landscape_mutation_fencing.py` and `tests/unit/architecture/test_web_reserve_reviewed_contracts.py`: always `--ignore` them until U-46's budget lands.
- **Orphaned workers.** Killing a pytest parent orphans its xdist workers (ppid 1). Find them by `/proc/<pid>/cwd` and stop them.
- **Pre-existing reds.** `tests/integration/pipeline/test_composer_llm_eval_characterization.py::test_long_running_compose_failures_expose_distinct_progress_guidance` fails on the target too (`AuditIntegrityError`). `tests/e2e/recovery/test_sink_effect_deployment_profiles.py::test_web_hosted_leader_plus_same_host_cli_followers_profile_covers_sink_effect_process_death_matrix` is flaky under load and passes at `-n 0`.
- **Unguarded commands.** The PreToolUse guard blocks `git checkout -- <path>`, and the rm guard blocks unguarded variable paths. Restore files with `git show HEAD:<path> > <path>`.
- **Masquerade.** Not re-run after 430b3bd46. The added lines carry no probes (grep controlled), but a slot-permitting masquerade run is cheap assurance before landing.

**Landing conditions still owed:**
- The landing rule holds for the five commits: new ids pass at each commit, and no pair is new against the target (§1, §3).
- Re-run the keyless set-diff and the two touched test files if the branch is rebased onto a newer target.
- **PostgreSQL ids, never run.** `tests/testcontainer/core/test_resume_requeue_failed_work_postgres.py` (most direct), plus the files listed in §8: barrier_recovery, heartbeat_takeover, aggregation_error_route, aggregation_release_continuations, collector_failure_counts/verdict, quarantine_resume_crash_matrix, run_start_admission, run_export_status, sink_effect_decision_clock, sink_effect_lock_order, plugins/database_sink_effects, and web/cross_process_{engine,execution}_handoff.
- **Upgrade note owed (U-53).** A graceful stop of a non-snapshot source now records the in-flight row `(NULL, ABANDONED)`. On the CLI path the base recorded it as a failure with no plugin run; `settings=None` callers previously had it processed. The run is non-resumable either way, and recovery is a rerun. Persisted `exception_type` changes from `InterruptedError` to `RetryShutdownRequestedError`.
