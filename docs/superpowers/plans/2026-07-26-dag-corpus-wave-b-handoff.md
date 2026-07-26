# DAG Corpus Wave B Integration and Resume Handoff

> **For agentic workers:** Use `superpowers:subagent-driven-development` for
> bounded implementation packets and `superpowers:verification-before-completion`
> before closing a tracker issue or accepting an integration. Use the
> project-local Filigree, Loomweave, and Warpline workflows at their normal
> gates. Keep one coordinator as the sole manifest integrator.

**Goal:** Preserve the verified pre-platform Wave B corpus, combine it with the
current release, rebase the paused deferred-platform work onto that combined
source, and reaccept every provisional recovery/runtime/audit claim before
candidate freeze.

**Architecture:** Wave B deliberately proves only the current single-process
production path. The deferred-platform branch remains the owner of distributed
session, run, lease, epoch, reclaim, and late-completion authority. Combining
the two branches is therefore a staged invalidation and reacceptance exercise,
not a textual merge followed by assumed acceptance.

**Tech stack:** Python 3.13, Pydantic v2, PyYAML, pytest, SQLAlchemy, Elspeth's
production `ExecutionGraph`, `Orchestrator`, `LandscapeDB`, checkpoint/recovery
APIs, PostgreSQL/SQLite, Filigree, Loomweave, Warpline, Ruff, and mypy.

---

## Final pre-platform snapshot

Recorded on 2026-07-27. Refresh all volatile facts before executing this
handoff.

| Surface | Exact recorded fact |
| --- | --- |
| Wave B worktree | `/home/john/elspeth/.worktrees/dag-corpus-wave-b` |
| Wave B implementation/evidence head | `210a92d544dbb10d5d0b438351f123e05da19f5d` (immutable evidence boundary) |
| Wave B integration tip | Capture `codex/dag-corpus-wave-b^{commit}` at execution; it must pass the allowed-tail checks below |
| Recorded release head (volatile) | `release/0.7.2@ad7f1e277e2ecab75e33bdda6d82c0342d418600` |
| Release/Wave B merge base | `5308e8c8867c6c6c26964a82eaf1081e2d1327d0` |
| Divergence at the implementation/evidence head | 35 release-only commits and 20 Wave-B-only commits |
| Expected divergence after this two-file handoff commit | 35 release-only commits and 21 Wave-B-only commits; verify dynamically rather than pinning the future commit ID |
| Paused platform worktree | `/home/john/elspeth/.worktrees/deferred-platform-completion` |
| Recorded paused-platform head (volatile) | `codex/deferred-platform-completion@132bd53232ea6b3885250675c361d3c057b19ac5` |
| Platform replay base | `696b3d1414ed7a6789c8f25bf5cbdc5450385bdd` |
| Recorded platform replay range | 13 commits in `696b3d1414ed7a6789c8f25bf5cbdc5450385bdd..132bd53232ea6b3885250675c361d3c057b19ac5`; refresh the tip at execution |
| Platform progress | Clean and paused after Task 5 of the live deferred-platform plan |
| Focused corpus gate | 541 passed, 2 expected xfailed, 3 known quorum warnings |
| Static gate | Focused Ruff check, Ruff format check, mypy, and `git diff --check` green |
| Parent issue | `elspeth-ef29ef6ba4`, open and `in_progress` |

Wave B is **not** based on the current release head. Never describe
`210a92d544dbb10d5d0b438351f123e05da19f5d` as the final Wave B branch head,
a combined commit, or a release-ready commit. It is the immutable
implementation/evidence boundary. The handoff commit itself is part of Wave B
and must be included in integration through the dynamically captured
`WAVE_B_INTEGRATION_TIP`.

After the single final two-file handoff commit, exactly one commit may follow
the implementation/evidence head, and that commit may change only these files:

- `docs/superpowers/plans/2026-07-26-dag-corpus-wave-b-handoff.md`
- `docs/superpowers/plans/2026-07-26-dag-corpus-wave-b-preplatform-sprint.md`

At execution, capture and validate the tip instead of comparing the branch to
a stale recorded SHA:

```bash
wave_b_worktree=/home/john/elspeth/.worktrees/dag-corpus-wave-b
wave_b_implementation_head=210a92d544dbb10d5d0b438351f123e05da19f5d
wave_b_integration_tip=$(git -C "$wave_b_worktree" \
  rev-parse codex/dag-corpus-wave-b^{commit})
git -C "$wave_b_worktree" merge-base --is-ancestor \
  "$wave_b_implementation_head" "$wave_b_integration_tip"
test "$(git -C "$wave_b_worktree" rev-list --count \
  "$wave_b_implementation_head..$wave_b_integration_tip")" -eq 1
test "$(git -C "$wave_b_worktree" diff --name-only \
  "$wave_b_implementation_head..$wave_b_integration_tip" | sort)" = \
  "$(printf '%s\n' \
    docs/superpowers/plans/2026-07-26-dag-corpus-wave-b-handoff.md \
    docs/superpowers/plans/2026-07-26-dag-corpus-wave-b-preplatform-sprint.md \
    | sort)"
git -C "$wave_b_worktree" diff --check \
  "$wave_b_implementation_head..$wave_b_integration_tip"
```

Review that one-commit documentation diff before integration. If the allowed
tail is absent, longer than one commit, or touches another path, stop and
reconcile it explicitly. The next durable combined source boundary must
contain the refreshed release tip and this validated Wave B integration tip.

The active deferred-platform plan is the complete file in the paused platform
worktree:

`/home/john/elspeth/.worktrees/deferred-platform-completion/docs/superpowers/plans/2026-07-26-finish-deferred-deployment-platforms.md`

Read it in full after every rebase. This handoff does not replace that plan.

The release-branch coordination reference
`docs/superpowers/plans/2026-07-26-p1-work-bucket-coordination-reference.md`
is absent from this Wave B branch. Refresh its stale R2/R3 facts only after the
branches are reconciled; do not manufacture a Wave B copy.

## What Wave B accepted

The final B4-A commit is
`210a92d544dbb10d5d0b438351f123e05da19f5d` (`test(dag): prove terminal
resume idempotence`). It closed `elspeth-8a6f52b2f6` and promoted only
`checkpoint-deterministic-resume.runtime` and
`checkpoint-deterministic-resume.audit`.

B4-A proves a single-process, fresh-object control-versus-reopen/resume path,
including terminal second-resume refusal with zero durable or output mutation.
It does not prove distributed ownership, checkpoint compatibility across the
deferred-platform rebase, or the complete checkpoint recovery contract. Its
runtime/audit promotion is provisional until post-rebase reacceptance.

### Final manifest ledger

The schema-v2 manifest contains 15 scenarios, 39 registered cases, 99 evidence
references, and 165 cells. Its verdict remains `not_complete`.

| Dimension | Pass | Partial | Fail | Unknown | N/A | Applicable non-pass |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `config` | 13 | 0 | 1 | 0 | 1 | 1 |
| `build` | 14 | 0 | 1 | 0 | 0 | 1 |
| `contracts` | 10 | 4 | 1 | 0 | 0 | 5 |
| `runtime` | 12 | 2 | 1 | 0 | 0 | 3 |
| `audit` | 9 | 5 | 0 | 0 | 1 | 5 |
| `recovery` | 3 | 6 | 0 | 5 | 1 | 11 |
| `concurrency` | 0 | 4 | 0 | 10 | 1 | 14 |
| `freeform` | 12 | 0 | 1 | 0 | 2 | 1 |
| `guided` | 0 | 2 | 11 | 0 | 2 | 13 |
| `round_trip` | 0 | 7 | 0 | 5 | 3 | 12 |
| `scale` | 0 | 1 | 0 | 13 | 1 | 14 |
| **Total** | **73** | **31** | **16** | **33** | **12** | **80** |

Current ownership encoded in the 80 applicable non-pass cells:

| Owner issue | Partial | Fail | Unknown | Total |
| --- | ---: | ---: | ---: | ---: |
| `elspeth-ef29ef6ba4` | 12 | 0 | 15 | 27 |
| `elspeth-7cf763da7c` | 7 | 0 | 5 | 12 |
| `elspeth-7e2dd67275` | 2 | 10 | 0 | 12 |
| `elspeth-cb1053fe46` | 1 | 0 | 13 | 14 |
| `elspeth-a5b86149d4` | 0 | 6 | 0 | 6 |
| `elspeth-3a6fa9141f` | 3 | 0 | 0 | 3 |
| `elspeth-b1f23d8d83` | 1 | 0 | 0 | 1 |
| `elspeth-f321e3ff21` | 1 | 0 | 0 | 1 |
| `elspeth-245b21351b` | 1 | 0 | 0 | 1 |
| `elspeth-2e66723070` | 1 | 0 | 0 | 1 |
| `elspeth-6f6bbbec00` | 1 | 0 | 0 | 1 |
| `elspeth-67b44040ee` | 1 | 0 | 0 | 1 |

Do not promote cells from aggregate counts, neighbouring scenarios, or a clean
rebase. Recompute this ledger mechanically after every manifest change.

## Integrated issue and commit ledger

| Relationship | Issue | Scope | Integrated close commit |
| --- | --- | --- | --- |
| Wave A child | `elspeth-e8acea2a55` | Runtime-consumed input evidence integrity | `5e799c06605ae8ddcd896a338189b5669672bc06` |
| Wave A child | `elspeth-d88d0e45c0` | Repository-relative link containment | `90e218f2bbe117e69157e4cd70238f238d179bf5` |
| Wave A child | `elspeth-a77a50d44d` | Bounded config/build graph cases | `be5f183aa7735adb3b5e3291709855056b54a68f` |
| Wave B child | `elspeth-e107732f81` | F0 exact evidence contracts | `0ae4bbfd4ce733b38934dcef3471586185420b8b` |
| Wave B child | `elspeth-d80523e188` | B1 runtime/audit | `8242fd4e5d1568fd286542375b9ee8d33c88440e` |
| Wave B child | `elspeth-6c1b21df32` | B2-5 partial terminal failure | `649d7bd667a79b57ea215334636cdd73428e8fd9` |
| Wave B child | `elspeth-e6a48f671b` | B2-6 coalesce matrix | `11153a0ac0b71ed620367e14b995510e480ea788` |
| Wave B child | `elspeth-dacc3c9e1f` | B2-7/8 composed coalesces | `4c929bbea396d04fcb3fba090c59d9b425a40764` |
| Wave B child | `elspeth-a19d62ab4c` | Parallel coalesce recovery | `906c6915dd4935a871fac3e34175b2633ba2cebe` |
| Wave B child | `elspeth-208c060cad` | B3 runtime/audit | `016bba6059b8e1815e147decf6d3fde01c68e290` |
| Wave B child | `elspeth-272a236fc8` | B3 aggregation/expansion recovery | `e4d71d392d9a1d10596615e5a61af9213525ce0a` |
| Authoritative external task | `elspeth-76bb92bc7d` | Full sink bundle/redrive | `7b2563c51871b992f7bba79b5fd9ecc25cf58520` |
| Wave B child | `elspeth-8a6f52b2f6` | B4-A terminal resume idempotence | `210a92d544dbb10d5d0b438351f123e05da19f5d` |

The implementation/evidence range is the 20 commits after
`5308e8c8867c6c6c26964a82eaf1081e2d1327d0` through
`210a92d544dbb10d5d0b438351f123e05da19f5d`. The integration range extends
through the validated dynamic `WAVE_B_INTEGRATION_TIP`, including the one
expected two-file handoff commit. Preserve that entire range; do not reduce the
ledger to close commits alone because supporting schema, fixture,
documentation, review, and handoff repairs are separate commits.

## Honest open blockers

### Expected xfails

The focused gate is not a zero-nonpass suite. It has two expected xfails:

- `test_b2_composed_coalesces_raw_identity_converges_across_equivalent_runs[sequential-nested-fork-coalesce-two-sequential-require-all]`
- `test_b2_composed_coalesces_raw_identity_converges_across_equivalent_runs[parallel-coalesces-two-parallel-require-all]`

Both cite `elspeth-3a6fa9141f`. Equivalent require-all executions still derive
durable parent ordinals, lineage, and sink-effect identity from arrival order.
Keep the three affected audit cells partial. A report that says only “541
passed” is false; preserve `541 passed, 2 xfailed, 3 warnings` until the bug is
fixed and the xfails are removed.

### Blocked recovery children

Seven P2 children remain open and blocked by P3
`elspeth-7dcc6554e7` (bounded TTL wait for `SinkEffectLeaseHeld`):

| Packet | Open children |
| --- | --- |
| B1-X | `elspeth-1805aec2a6`, `elspeth-3ae349b761`, `elspeth-9e74843418`, `elspeth-7729c0beb5` |
| B2-X | `elspeth-d09414a1a0`, `elspeth-e7a8fb1547`, `elspeth-cd09538cd0` |

This is a priority inversion: a P3 blocks seven P2 children and contributes to
an unfinished P1 umbrella. Reconcile the dependency and priority before
dispatch. Do not close the children or promote their recovery cells merely
because adjacent recovery cases pass.

### Product and platform blockers

- `elspeth-b1f23d8d83`: union collision policy is absent from canonical
  coalesce node and audit identity.
- `elspeth-3a6fa9141f`: coalesce parent order is arrival-dependent; owns the
  two xfails and three partial audit cells.
- `elspeth-d3960e8463`: public resume can skip residual `PENDING_SINK` work.
- `elspeth-0b0eaa63df`: fixed-schema `any` fields cannot be reconstructed from
  persisted JSON Schema. The bounded observed-schema proof does not close it.
- `elspeth-f321e3ff21` and `elspeth-245b21351b`: checkpoint compatibility and
  post-leadership cleanup remain on the paused platform branch.
- `elspeth-9a52eb80f9`: registered independent-process orchestration and sink
  redrive remains the authority for B4-B.

### Scale dependency deadlock

All 14 applicable scale cells point to `elspeth-cb1053fe46`, but that issue is
blocked by `elspeth-ef29ef6ba4`. The parent cannot satisfy its manifest-based
completion contract while its scale owner waits for the parent to close.
Reconcile that dependency before scale execution, normally by removing the
parent-as-blocker edge while keeping both issues open. Do not weaken either
acceptance contract merely to break the cycle.

## Required combine, rebase, and reacceptance sequence

This snapshot is still before deferred-platform Task 21, so use the
pre-freeze sequence unless live state proves Task 21 or later has begun.

1. **Refresh and freeze inputs.** Require clean worktrees. Capture the current
   release and platform tips as volatile execution inputs; do not require them
   to equal the recorded snapshot. Capture `WAVE_B_INTEGRATION_TIP` and require
   the implementation-head ancestry, one-commit allowed tail, exact two-file
   path set, and clean diff shown above. Refresh the parent, all blocker
   issues, the release task, Docker task, and operator P0. An active or
   human-owned claim is not available work.
2. **Create one combined release/Wave B commit.** Start an isolated integration
   branch from the refreshed `release/0.7.2` tip, merge or replay through
   `WAVE_B_INTEGRATION_TIP`, resolve conflicts semantically, and run the
   focused/static floor below. Require both the refreshed release tip and
   `WAVE_B_INTEGRATION_TIP` to be ancestors of the result. Record the resulting
   full `COMBINED_SHA`. Do not merge directly into the release checkout before
   review.
3. **Rebase the refreshed paused-platform replay range.** After review of the
   combined source, capture the live platform tip. Require the recorded replay
   base to remain its ancestor, review any commits after the recorded
   `132bd53232ea6b3885250675c361d3c057b19ac5` snapshot, and replay through the
   captured tip onto the reviewed `COMBINED_SHA`. Replay the captured commit,
   not a moving branch ref, then update the platform branch with a compare-and-
   swap so a concurrent branch move fails closed:

   ```bash
   (
   set -euo pipefail

   platform_worktree=/home/john/elspeth/.worktrees/deferred-platform-completion
   platform_branch=codex/deferred-platform-completion
   platform_replay_base=696b3d1414ed7a6789c8f25bf5cbdc5450385bdd
   combined_sha=2b47c25428ed0e978443df953e0fd587860aeed8
   test -z "$(git -C "$platform_worktree" status --short)"
   test "$(git -C "$platform_worktree" branch --show-current)" = \
     "$platform_branch"
   platform_integration_tip=$(git -C "$platform_worktree" rev-parse HEAD^{commit})
   test "$(git -C "$platform_worktree" rev-parse \
     "$platform_branch"^{commit})" = "$platform_integration_tip"
   test "$(git -C /home/john/elspeth rev-parse \
     "$combined_sha"^{commit})" = "$combined_sha"
   git -C /home/john/elspeth merge-base --is-ancestor \
     "$combined_sha" release/0.7.2
   git -C "$platform_worktree" merge-base --is-ancestor \
     "$platform_replay_base" "$platform_integration_tip"
   test "$(git -C "$platform_worktree" merge-base \
     "$combined_sha" "$platform_integration_tip")" = "$platform_replay_base"
   test -z "$(git -C "$platform_worktree" rev-list --merges \
     "$platform_replay_base..$platform_integration_tip")"
   git -C "$platform_worktree" switch --detach "$platform_integration_tip"
   git -C "$platform_worktree" rebase --onto \
     "$combined_sha" "$platform_replay_base" \
     "$platform_integration_tip"
   rebased_platform_tip=$(git -C "$platform_worktree" rev-parse HEAD^{commit})
   git -C "$platform_worktree" update-ref \
     "refs/heads/$platform_branch" "$rebased_platform_tip" \
     "$platform_integration_tip"
   git -C "$platform_worktree" switch "$platform_branch"
   )
   ```

   Review each conflict, the recorded 13-commit range, and any explicitly
   reviewed later platform commits. If the compare-and-swap fails, stop: the
   platform branch moved after capture and the detached replay is not authority.
   A textually clean rebase is not acceptance.
4. **Invalidate pre-platform evidence immediately.** Treat every B1-X, B2-X,
   B3-X, and
   B4-A recovery/runtime/audit proof as provisional at its recorded Wave B
   commit. Do not carry forward an evidence locator, fixture hash,
   compatibility/topology hash, acceptance hash, or cell status solely because
   the rebase applied cleanly.
5. **Resume platform implementation through its source-stable boundary.**
   Reverify the affected authority suites from already-landed Tasks 3 through
   5, then continue the live deferred-platform plan from Task 6 through Task
   13. Do not begin Task 14's provider packaging while the corpus remains
   invalidated.
6. **Implement B4-B only on the real distributed harness.** Once platform Task
   13's independent-process PostgreSQL/registered-worker matrix is green,
   execute scenario 15 with real Landscape claim epochs, lease expiry, reclaim,
   stale-worker fencing, and late-completion refusal. Scenario 15 cannot
   promote another scenario's concurrency cell by analogy.
7. **Reaccept the corpus before Task 14.** Regenerate topology and compatibility
   evidence, rerun every registered recovery case and seam-specific verifier,
   rerun the full corpus/static floor, and re-audit every promoted
   recovery/runtime/audit cell against the post-Task-13 production behavior.
   If Tasks 14 through 20 later touch a corpus-sensitive production seam,
   invalidate and rerun the affected evidence again before Task 21.
8. **Run candidate acceptance once on the combined tree.** Complete
   deferred-platform Tasks 21 through 27 only after source and evidence are
   stable. If Task 21 or later had already started, explicitly invalidate the
   candidate, image, live ACA/Azure Files evidence, and receipt, then restart
   at Task 21.

The operator P0 may continue diagnosis or staging in parallel. Its final
signature and fingerprint-baseline result must bind the frozen combined tree;
an earlier result is historical and must be repeated. Never hand-edit the
baseline, bypass signature verification, or reuse operator credentials.

## Exact verification floor

Run these commands on Wave B now, on the combined integration branch, and
again after the platform rebase.

```bash
env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 \
  tests/unit/architecture/test_dag_scenario_corpus_contract.py \
  tests/integration/core/dag/test_dag_scenario_production_path.py

env -u VIRTUAL_ENV uv run --frozen ruff check \
  tests/fixtures/dag_scenario_corpus \
  tests/unit/architecture/test_dag_scenario_corpus_contract.py \
  tests/integration/core/dag/test_dag_scenario_production_path.py

env -u VIRTUAL_ENV uv run --frozen ruff format --check \
  tests/fixtures/dag_scenario_corpus \
  tests/unit/architecture/test_dag_scenario_corpus_contract.py \
  tests/integration/core/dag/test_dag_scenario_production_path.py

env -u VIRTUAL_ENV uv run --frozen mypy \
  tests/fixtures/dag_scenario_corpus \
  tests/unit/architecture/test_dag_scenario_corpus_contract.py \
  tests/integration/core/dag/test_dag_scenario_production_path.py

git diff --check
git status --short --branch
```

The current pre-platform expected result is exactly `541 passed, 2 xfailed, 3
warnings`. Investigate any collection shrink, new skip/xfail, unexpected xpass,
or warning change.

After the platform rebase, run the complete two-file gate above and at least
these registered/seam-specific recovery node IDs:

```bash
env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 \
  tests/integration/core/dag/test_dag_scenario_production_path.py::test_declared_recovery_case_reopens_and_resumes_publicly \
  tests/integration/core/dag/test_dag_scenario_production_path.py::test_b3_recovery_rebuilds_fresh_settings_plugins_graph_and_config \
  tests/integration/core/dag/test_dag_scenario_production_path.py::test_checkpoint_reopen_resume_has_exact_restart_evidence \
  tests/integration/core/dag/test_dag_scenario_production_path.py::test_eof_aggregation_recovery_preserves_failed_batch_and_member_identity \
  tests/integration/core/dag/test_dag_scenario_production_path.py::test_expansion_recovery_preserves_parent_child_group_and_scheduler_identity \
  tests/integration/core/dag/test_dag_scenario_production_path.py::test_pending_sink_redrive_recovery_preserves_complete_bundle_and_exactly_once_publication \
  tests/integration/core/dag/test_dag_scenario_production_path.py::test_parallel_coalesces_recovery_reuses_finalized_first_sink \
  tests/unit/architecture/test_dag_scenario_corpus_contract.py::test_terminal_resume_idempotence_pins_terminal_equivalence_and_every_no_mutation_view
```

Copy the exact Task 13 PostgreSQL/testcontainer commands from the live
deferred-platform plan after rebase; do not preserve a second, drift-prone copy
here. Run every additional Warpline reverification item.

Before final candidate freeze, run the repository gate:

```bash
env -u VIRTUAL_ENV uv run --frozen ruff check \
  src/ tests/ scripts/ examples/ elspeth-lints/src/
env -u VIRTUAL_ENV uv run --frozen ruff format --check \
  src/ tests/ scripts/ examples/ elspeth-lints/src/
env -u VIRTUAL_ENV uv run --frozen mypy src/ elspeth-lints/src/
env -u VIRTUAL_ENV uv run --frozen pytest tests/ -v \
  -m "not slow and not stress and not performance and not testcontainer"
```

Tasks 21, 25, and 27 additionally run the exact agent/operator local release
gates required by the live deferred-platform plan. Provider acceptance,
receipt binding, signatures, and image facts must all refer to the same frozen
combined source.

## Tracker handoff and closure rule

`elspeth-ef29ef6ba4` remains open. Eighty applicable cells are still non-pass,
seven recovery children are blocked, the two xfails remain, distributed B4-B
is absent, and the scale dependency is unresolved.

After this handoff is added to the parent issue, release the
`codex-dag-wave-b-coordinator` claim without reverting workflow status. Do not
close the parent. Future workers claim bounded children or authoritative
external issues; they do not hold the umbrella for the duration of the
backlog.

Close the parent only when every applicable manifest cell has executable
passing evidence, or an unsupported capability has an explicit executable
fail-closed rejection contract and the remaining product work has honest live
ownership.

## Stop conditions

Stop and return to coordination when:

- a worktree is dirty, a captured execution tip changes during integration,
  or the refreshed release/platform ancestry cannot be explained and reviewed;
- `210a92d544dbb10d5d0b438351f123e05da19f5d` is not an ancestor of the Wave B
  integration tip, more or fewer than one commit follows it, or that tail
  changes anything outside the two allowed handoff files;
- the combined commit does not contain both the refreshed release tip and the
  validated Wave B integration tip;
- a conflict is resolved by accepting one whole side without reviewing the
  production authority and corpus contract together;
- the two expected xfails are omitted from a result, change unexpectedly, or a
  new skip/xfail appears;
- a cell would be promoted from analogy, aggregate counts, or pre-rebase
  evidence;
- platform Task 21 or later has begun without invalidating and restarting
  combined acceptance;
- B4-B uses threads, one process, web-session ownership, or a corpus-only lease
  model instead of registered production workers against shared PostgreSQL;
- the scale dependency cycle remains while either issue is proposed for
  closure;
- operator authority, provider acceptance, signing, or release publication is
  missing; or
- any source, manifest, image input, trust input, or receipt input changes
  after final acceptance without returning to Task 21.
