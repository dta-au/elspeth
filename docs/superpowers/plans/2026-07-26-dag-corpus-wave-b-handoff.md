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

## Current pre-platform snapshot

Refreshed on 2026-07-27 after Wave B integration, the resulting correctness
fixes, the independent recovery durable/export parity oracle, and the seven
bounded B1-X/B2-X recovery children. Refresh volatile platform and tracker
facts before executing the remaining handoff.

| Surface | Exact recorded fact |
| --- | --- |
| Wave B worktree | Retired after integration |
| Wave B implementation/evidence head | `210a92d544dbb10d5d0b438351f123e05da19f5d` (immutable evidence boundary) |
| Wave B integration tip | `ca65ecab32f77c8cbd2b33ce8685c381011078e0` |
| Combined Wave B merge | `2b47c25428ed0e978443df953e0fd587860aeed8` |
| Current reviewed production/evidence floor | `release/0.7.2@55727d54c6b057b77a926778deeb933208d78543` |
| Future platform replay anchor | Capture the exact reviewed `release/0.7.2` tip after the bug-fix/corpus sprint; require it to descend from the recorded floor |
| Coordination-doc refresh | The commit containing this refresh is documentation-only and does not create new production evidence |
| Paused platform worktree | `/home/john/elspeth/.worktrees/deferred-platform-completion` |
| Recorded paused-platform head | `codex/deferred-platform-completion@132bd53232ea6b3885250675c361d3c057b19ac5` |
| Platform replay base | `696b3d1414ed7a6789c8f25bf5cbdc5450385bdd` |
| Recorded platform replay range | 13 commits in `696b3d1414ed7a6789c8f25bf5cbdc5450385bdd..132bd53232ea6b3885250675c361d3c057b19ac5` |
| Platform progress | Clean and paused after Task 5 of the live deferred-platform plan |
| Focused corpus gate | 593 passed, 0 xfailed, 3 known quorum warnings across the contract, production path, and seven topology-specific recovery suites |
| Static gate | Focused Ruff check, Ruff format check, and mypy green at the source floor; task commits pass `git diff --check` |
| Code-index/change-impact state | Loomweave was refreshed before implementation; Warpline sees the changed corpus entities, but its dependency snapshot is partial and 271 commits stale, so its blast-radius result is advisory only |
| Parent issue | `elspeth-ef29ef6ba4`, open and `in_progress`; leave unassigned between bounded packets |

Wave B is already integrated. Do not recreate the merge, recover the retired
worktree, or rebase the platform branch onto the historical Wave B head.
`210a92d544dbb10d5d0b438351f123e05da19f5d` remains the immutable Wave B
implementation/evidence boundary, while
`55727d54c6b057b77a926778deeb933208d78543` is the reviewed
production/evidence floor after integration, the correctness fixes, the
independent recovery durable/export oracle, and the seven bounded recovery
children. The commit containing this documentation refresh changes no
production or corpus source. At the eventual platform rebase, capture the
exact reviewed `release/0.7.2` tip so no later bug-fix/corpus commits are
omitted.

At execution, capture and validate the source and platform boundaries:

```bash
release_worktree=/home/john/elspeth
platform_worktree=/home/john/elspeth/.worktrees/deferred-platform-completion
recorded_source_floor=55727d54c6b057b77a926778deeb933208d78543
source_anchor=$(git -C "$release_worktree" \
  rev-parse release/0.7.2^{commit})
platform_replay_base=696b3d1414ed7a6789c8f25bf5cbdc5450385bdd
platform_tip=$(git -C "$platform_worktree" rev-parse HEAD^{commit})
test "$(git -C "$release_worktree" rev-parse "$source_anchor"^{commit})" = \
  "$source_anchor"
git -C "$release_worktree" merge-base --is-ancestor \
  "$recorded_source_floor" "$source_anchor"
test -z "$(git -C "$platform_worktree" status --short)"
test "$(git -C "$platform_worktree" merge-base \
  "$source_anchor" "$platform_tip")" = "$platform_replay_base"
```

The release checkout was clean at this refresh. Recheck it immediately before
capturing the source anchor; unrelated later working-tree changes remain
outside this handoff.

The active deferred-platform plan is the complete file in the paused platform
worktree:

`/home/john/elspeth/.worktrees/deferred-platform-completion/docs/superpowers/plans/2026-07-26-finish-deferred-deployment-platforms.md`

Read it in full after every rebase. This handoff does not replace that plan.

The release-branch coordination reference is
`docs/superpowers/plans/2026-07-26-p1-work-bucket-coordination-reference.md`.
Treat this current handoff and live tracker state as authoritative when older
R2/R3 facts in that historical reference disagree.

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

The schema-v2 manifest contains 15 scenarios, 46 registered cases, 107 evidence
references, and 165 cells. Its verdict remains `not_complete`.

| Dimension | Pass | Partial | Fail | Unknown | N/A | Applicable non-pass |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `config` | 13 | 0 | 1 | 0 | 1 | 1 |
| `build` | 14 | 0 | 1 | 0 | 0 | 1 |
| `contracts` | 11 | 3 | 1 | 0 | 0 | 4 |
| `runtime` | 12 | 2 | 1 | 0 | 0 | 3 |
| `audit` | 12 | 2 | 0 | 0 | 1 | 2 |
| `recovery` | 8 | 5 | 0 | 1 | 1 | 6 |
| `concurrency` | 0 | 4 | 0 | 10 | 1 | 14 |
| `freeform` | 12 | 0 | 1 | 0 | 2 | 1 |
| `guided` | 0 | 2 | 11 | 0 | 2 | 13 |
| `round_trip` | 0 | 7 | 0 | 5 | 3 | 12 |
| `scale` | 0 | 1 | 0 | 13 | 1 | 14 |
| **Total** | **82** | **26** | **16** | **29** | **12** | **71** |

Current ownership encoded in the 71 applicable non-pass cells:

| Owner issue | Partial | Fail | Unknown | Total |
| --- | ---: | ---: | ---: | ---: |
| `elspeth-ef29ef6ba4` | 11 | 0 | 11 | 22 |
| `elspeth-cb1053fe46` | 1 | 0 | 13 | 14 |
| `elspeth-7cf763da7c` | 7 | 0 | 5 | 12 |
| `elspeth-7e2dd67275` | 2 | 10 | 0 | 12 |
| `elspeth-a5b86149d4` | 0 | 6 | 0 | 6 |
| `elspeth-f321e3ff21` | 1 | 0 | 0 | 1 |
| `elspeth-245b21351b` | 1 | 0 | 0 | 1 |
| `elspeth-2e66723070` | 1 | 0 | 0 | 1 |
| `elspeth-67b44040ee` | 1 | 0 | 0 | 1 |
| `elspeth-6f6bbbec00` | 1 | 0 | 0 | 1 |

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
| Post-integration P1 fix | `elspeth-3a6fa9141f` | Canonical coalesce parent identity | `229e2245d84e1195dc6693cf6ab55684190bcf9b` |
| Post-integration P1 fix | `elspeth-b1f23d8d83` | Union collision policy identity | `8c124889e7275eadb0b99242d9617a2c3601b727` |
| Post-integration P1 fix | `elspeth-d3960e8463` | Residual pending-sink resume | `c8f6094a7a5b60c995a7c5e0f3e500ec4d5c45b3` |
| Post-integration P1 fix | `elspeth-0b0eaa63df` | Fixed-any schema reconstruction | `a3838611cd7931eb71b9d42a6e9e2bd2e78d14b9` |
| Independent corpus oracle | `elspeth-168c26e22e` | Direct-table recovery durable/export parity | `cb07cfa91f252c86737e8e15c711ccd1ec9fe76e` |
| Recovery prerequisite | `elspeth-7dcc6554e7` | Bounded sink-effect lease contention waits | `2c47097bb` |
| B1-X child | `elspeth-1805aec2a6` | Linear public fresh-object recovery | `40ebf7dad7a9188dab112367b1023cbdf7e6c321` |
| B1-X child | `elspeth-3ae349b761` | Independent roots public recovery | `0fe4af494` |
| B1-X child | `elspeth-9e74843418` | Queued fan-in public recovery | `b80bf9aa2` |
| B1-X child | `elspeth-7729c0beb5` | Conditional route public recovery | `23a160926` |
| B2-X child | `elspeth-d09414a1a0` | Partial-terminal public recovery | `924739492` |
| B2-X child | `elspeth-e7a8fb1547` | Post-coalesce pending-sink public recovery | `eb6f341a7ca606cdd304910367c68ac41c44e44d` |
| B2-X child | `elspeth-cd09538cd0` | Sequential terminal-publication public recovery | `55727d54c6b057b77a926778deeb933208d78543` |

The Wave B implementation/evidence range is the 20 commits after
`5308e8c8867c6c6c26964a82eaf1081e2d1327d0` through
`210a92d544dbb10d5d0b438351f123e05da19f5d`. The integrated Wave B tip is
`ca65ecab32f77c8cbd2b33ce8685c381011078e0`, incorporated by merge
`2b47c25428ed0e978443df953e0fd587860aeed8`. Preserve that entire history; do
not reduce the ledger to close commits alone because supporting schema,
fixture, documentation, review, and handoff repairs are separate commits.

The current reviewed production/evidence floor is
`55727d54c6b057b77a926778deeb933208d78543`. It includes the
scheduler-quiescence impact-audit follow-up, the independent recovery
durable/export oracle, the bounded lease-contention fix, and all seven
integrated recovery children. Refresh the replay anchor after every later
reviewed production or corpus commit.

## Honest open blockers

### Resolved post-Wave-B findings

The two former coalesce identity xfails are now ordinary passing tests.
`elspeth-3a6fa9141f` canonicalized coalesce parents before lineage and
sink-effect derivation at `229e2245d84e1195dc6693cf6ab55684190bcf9b`.
`elspeth-b1f23d8d83` bound union collision policy into canonical node and audit
identity at `8c124889e7275eadb0b99242d9617a2c3601b727`.

The resume findings `elspeth-d3960e8463` and `elspeth-0b0eaa63df` are also
closed at `c8f6094a7a5b60c995a7c5e0f3e500ec4d5c45b3` and
`a3838611cd7931eb71b9d42a6e9e2bd2e78d14b9`.

`elspeth-168c26e22e` is resolved at
`cb07cfa91f252c86737e8e15c711ccd1ec9fe76e`. The durable side of recovery
parity now selects the claimed persisted record families directly from
`LandscapeDB` tables with explicit identity/material fields, ordering, and
normalization. An adversarial regression proves that omitting the
`validation_error` family from the shared portable serializer is detected.
Existing exact projections, static evidence, and direct recovery assertions
remain in force. The current augmented corpus result is `593 passed, 0
xfailed, 3 warnings`.

### Completed bounded recovery children

`elspeth-7dcc6554e7` bounded sink-effect lease contention waits, unblocking all
seven P2 recovery children. All seven are now integrated and closed:

| Packet | Closed children |
| --- | --- |
| B1-X | `elspeth-1805aec2a6`, `elspeth-3ae349b761`, `elspeth-9e74843418`, `elspeth-7729c0beb5` |
| B2-X | `elspeth-d09414a1a0`, `elspeth-e7a8fb1547`, `elspeth-cd09538cd0` |

The four B1-X recovery cells and the partial-terminal B2-X cell now pass. The
S6 fork/coalesce and S7 sequential-coalesce recovery cells remain partial
because the evidence proves post-coalesce terminal publication, not a held
coalesce branch or a literal between-merge seam. The parallel-coalesce
recovery cell was already partial for the analogous held-barrier gap.

### Product and platform blockers

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

## Required platform rebase and reacceptance sequence

The release/Wave B combination, its immediate correctness fixes, and all seven
bounded recovery children are complete. The remaining sequence starts from the
reviewed source floor and is still before deferred-platform Task 21.

1. **Refresh and freeze inputs.** Keep the platform worktree clean. Capture its
   current tip, verify that the replay base remains its ancestor, and capture
   the exact reviewed `release/0.7.2` tip as `source_anchor`. Require
   `55727d54c6b057b77a926778deeb933208d78543` to be its ancestor so the
   correctness fixes, independent recovery oracle, bounded lease fix, and
   seven recovery children remain included. Refresh the parent, all blocker
   issues, the release task, Docker task, and operator P0. An active or
   human-owned claim is not available work.
2. **Rebase the refreshed paused-platform replay range.** Capture the live
   platform tip. Require the recorded replay
   base to remain its ancestor, review any commits after the recorded
   `132bd53232ea6b3885250675c361d3c057b19ac5` snapshot, and replay through the
   captured tip onto the reviewed source anchor. Replay the captured commit,
   not a moving branch ref, then update the platform branch with a compare-and-
   swap so a concurrent branch move fails closed:

   ```bash
   (
   set -euo pipefail

   platform_worktree=/home/john/elspeth/.worktrees/deferred-platform-completion
   platform_branch=codex/deferred-platform-completion
   platform_replay_base=696b3d1414ed7a6789c8f25bf5cbdc5450385bdd
   recorded_source_floor=55727d54c6b057b77a926778deeb933208d78543
   source_anchor=$(git -C /home/john/elspeth \
     rev-parse release/0.7.2^{commit})
   test -z "$(git -C "$platform_worktree" status --short)"
   test "$(git -C "$platform_worktree" branch --show-current)" = \
     "$platform_branch"
   platform_integration_tip=$(git -C "$platform_worktree" rev-parse HEAD^{commit})
   test "$(git -C "$platform_worktree" rev-parse \
     "$platform_branch"^{commit})" = "$platform_integration_tip"
   test "$(git -C /home/john/elspeth rev-parse \
     "$source_anchor"^{commit})" = "$source_anchor"
   git -C /home/john/elspeth merge-base --is-ancestor \
     "$recorded_source_floor" "$source_anchor"
   git -C "$platform_worktree" merge-base --is-ancestor \
     "$platform_replay_base" "$platform_integration_tip"
   test "$(git -C "$platform_worktree" merge-base \
     "$source_anchor" "$platform_integration_tip")" = "$platform_replay_base"
   test -z "$(git -C "$platform_worktree" rev-list --merges \
     "$platform_replay_base..$platform_integration_tip")"
   git -C "$platform_worktree" switch --detach "$platform_integration_tip"
   git -C "$platform_worktree" rebase --onto \
     "$source_anchor" "$platform_replay_base" \
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
3. **Invalidate pre-platform evidence immediately.** Treat every B1-X, B2-X,
   B3-X, and
   B4-A recovery/runtime/audit proof as provisional at its recorded Wave B
   commit. Do not carry forward an evidence locator, fixture hash,
   compatibility/topology hash, acceptance hash, or cell status solely because
   the rebase applied cleanly.
4. **Resume platform implementation through its source-stable boundary.**
   Reverify the affected authority suites from already-landed Tasks 3 through
   5, then continue the live deferred-platform plan from Task 6 through Task
   13. Do not begin Task 14's provider packaging while the corpus remains
   invalidated.
5. **Implement B4-B only on the real distributed harness.** Once platform Task
   13's independent-process PostgreSQL/registered-worker matrix is green,
   execute scenario 15 with real Landscape claim epochs, lease expiry, reclaim,
   stale-worker fencing, and late-completion refusal. Scenario 15 cannot
   promote another scenario's concurrency cell by analogy.
6. **Reaccept the corpus before Task 14.** Regenerate topology and compatibility
   evidence, rerun every registered recovery case and seam-specific verifier,
   rerun the full corpus/static floor, and re-audit every promoted
   recovery/runtime/audit cell against the post-Task-13 production behavior.
   If Tasks 14 through 20 later touch a corpus-sensitive production seam,
   invalidate and rerun the affected evidence again before Task 21.
7. **Run candidate acceptance once on the combined tree.** Complete
   deferred-platform Tasks 21 through 27 only after source and evidence are
   stable. If Task 21 or later had already started, explicitly invalidate the
   candidate, image, live ACA/Azure Files evidence, and receipt, then restart
   at Task 21.

The operator P0 may continue diagnosis or staging in parallel. Its final
signature and fingerprint-baseline result must bind the frozen combined tree;
an earlier result is historical and must be repeated. Never hand-edit the
baseline, bypass signature verification, or reuse operator credentials.

## Exact verification floor

Run these commands at the current source anchor and again after the platform
rebase.

```bash
env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 \
  tests/unit/architecture/test_dag_scenario_corpus_contract.py \
  tests/integration/core/dag/test_dag_scenario_production_path.py \
  tests/integration/core/dag/test_dag_recovery_independent_roots.py \
  tests/integration/core/dag/test_dag_recovery_queued_fan_in.py \
  tests/integration/core/dag/test_dag_recovery_conditional_route.py \
  tests/integration/core/dag/test_dag_recovery_partial_terminal.py \
  tests/integration/core/dag/test_dag_recovery_s6_pending_sink.py \
  tests/integration/core/dag/test_dag_recovery_s7_terminal_publication.py

env -u VIRTUAL_ENV uv run --frozen ruff check \
  tests/fixtures/dag_scenario_corpus \
  tests/unit/architecture/test_dag_scenario_corpus_contract.py \
  tests/integration/core/dag/test_dag_scenario_production_path.py

env -u VIRTUAL_ENV uv run --frozen ruff format --check \
  tests/fixtures/dag_scenario_corpus \
  tests/unit/architecture/test_dag_scenario_corpus_contract.py \
  tests/integration/core/dag/test_dag_scenario_production_path.py

env -u VIRTUAL_ENV uv run --frozen mypy \
  src/ elspeth-lints/src/

git diff --check
git status --short --branch
```

The current pre-platform expected result is exactly `593 passed, 0 xfailed, 3
warnings`. Investigate any collection shrink, skip/xfail, or warning change.
The project-supported strict mypy surface is `src/ elspeth-lints/src/`;
`tests/` and `scripts/` are deliberately outside that gate.

After the platform rebase, run the complete gate above and at least these
registered/seam-specific recovery node IDs:

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

The latest Warpline worklist for the independent-oracle commit is not
authoritative: its edge snapshot is partial and 271 commits behind that
commit. It identifies the changed corpus harness/integration-test entities but
cannot safely narrow the verification set. Preserve the full focused corpus
gate above until a fresh complete snapshot is captured.

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

`elspeth-ef29ef6ba4` remains open. Seventy-one applicable cells are still
non-pass, distributed B4-B is absent, two recovery seam gaps remain explicit,
and the scale dependency is unresolved. Independent durable/export parity is
resolved by `elspeth-168c26e22e`; all seven bounded recovery children are
closed.

After this handoff is added to the parent issue, release the
`codex-dag-corpus-refresh` claim without reverting workflow status. Do not
close the parent. Future workers claim bounded children or authoritative
external issues; they do not hold the umbrella for the duration of the
backlog.

Close the parent only when every applicable manifest cell has executable
passing evidence, or an unsupported capability has an explicit executable
fail-closed rejection contract and the remaining product work has honest live
ownership.

## Stop conditions

Stop and return to coordination when:

- the platform worktree is dirty, its captured tip changes during replay, or
  the refreshed release/platform ancestry cannot be explained and reviewed;
- `55727d54c6b057b77a926778deeb933208d78543` is not an ancestor of the captured
  source anchor, a later production/corpus commit has not been reviewed and
  reverified, or the platform/source merge base is no longer the recorded replay
  base;
- a conflict is resolved by accepting one whole side without reviewing the
  production authority and corpus contract together;
- the 593-test collection shrinks, any skip/xfail appears, or a warning changes
  without review;
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
