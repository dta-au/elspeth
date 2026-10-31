# Worktree deletion candidates — 21 October 2026

This is the dated 0.8.9 baseline. The [maintained cleanup list](maintainer/worktree-cleanup.md)
records the next refresh after 0.8.10 merges and the cleanup before 0.8.11.

Inventory measured at **2026-10-21 20:51:47 AEDT**. “Older than 12 hours” means the Git worktree registration `gitdir` file has a filesystem birth time before **2026-10-21 08:51:47 AEDT**. Age means creation age, not inactivity or commit age.

Comparison target: [PR #328, Release 0.8.9](https://github.com/dta-au/elspeth/pull/328), branch `codex/composer-request-rejection-20261020`, exact head **`dbbb6582492ce1fb22941e0cc395aa20d4eb386d`**. Target `pyproject.toml` declares version `0.8.9`. [CI run 39577742087](https://github.com/dta-au/elspeth/actions/runs/39577742087) was queued on the final API recheck. Inclusion in this candidate does not establish passing CI or a merge to main.

The main checkout is detached at `ce4103539fe97f07fc60fb0cd096256c207c8bc5`; its unrelated untracked review documents were preserved. Repository orientation covered `AGENTS.md`, `CONTRIBUTING.md`, `README.md`, package entry points, Git refs/registrations, cleanup helper, and the live RC/CI records. CLI/YAML and Web Composer share contracts, engine and Landscape; this audit concerns Git custody, not runtime or code-quality acceptance.

## Result

**22 included worktrees are the candidate deletion list:** 20 by ancestry and two by complete patch/blob carry. Of those, **one is immediately removable under the canonical helper**, while **21 have preservation or process-use holds**. No worktree, branch, index, or remote ref was deliberately changed. This document is a proposal; no removal was executed.

The measured inventory totals, derived from the Git registry and filesystem birth records, are:

```text
registered worktrees = 59
main checkout = 1
linked worktrees older than 12h = 47
linked worktrees younger than 12h = 11
older HEADs that are ancestors of RC = 20
additional fully carried source patches = 2
older inclusion not established = 25
```

Paths beginning `.claude/` are relative to this repository root; `~/` means the current user home. A dirty path differing from the RC is a custody hold, not a finding that its contents are new or useful. Staged and working-file states were both inspected; a matching final file does not justify discarding a different staged version.

## Complete candidate list

All ancestry rows below have raw `git merge-base --is-ancestor HEAD dbbb65824` exit code **0**, and `git rev-list dbbb65824..HEAD` returned **no commits**. The two carry rows have the separate proofs below. All candidates exceed the fixed 12-hour cutoff.

| Worktree path | HEAD | Age (h) | Inclusion | Removal hold |
|---|---|---:|---|---|
| `~/.codex/worktrees/2ab4/elspeth` | `d647dd929d2b` | 46.3 | ancestry: exit 0 | 18 dirty/untracked paths; 18 final path states differ from / are absent in RC; 22 observed processes; contains a younger registered worktree; ignored material: `.claude/lanes/`, `.claude/worktrees/` |
| `~/.codex/worktrees/8bde/elspeth` | `1b7aaa6662a6` | 84.2 | ancestry: exit 0 | 100 dirty/untracked paths; 93 final path states differ from / are absent in RC; 7 observed processes; contains a younger registered worktree; ignored material: `.benchmarks/`, `.claude/lanes/`, `.claude/worktrees/` |
| `~/.codex/worktrees/composer-request-rejection-20261020/elspeth` | `9c549d6eb850` | 27.1 | ancestry: exit 0 | 8 dirty/untracked paths; 8 final path states differ from / are absent in RC; ignored material: `.benchmarks/`, `src/elspeth/web/frontend/node_modules` |
| `~/.codex/worktrees/tool-contract-audit-20261020/elspeth` | `67ad14c22b39` | 21.7 | ancestry: exit 0 | 27 dirty/untracked paths; 27 final path states differ from / are absent in RC; ignored material: `.benchmarks/`, `.claude/lanes/` |
| `~/Documents/Codex/2026-10-10/task/deep-research-worktree` | `9e481bc85a32` | 274.5 | ancestry: exit 0 | 8 dirty/untracked paths; 8 final path states differ from / are absent in RC; ignored material: `.benchmarks/` |
| `.claude/worktrees/advisor-cleanup-baseline-20261020` | `2d5c0557e2d3` | 33.1 | ancestry: exit 0 | ignored material: `.benchmarks/` |
| `.claude/worktrees/advisor-heuristic-baseline-20261020` | `24a02a1e29a8` | 40.7 | ancestry: exit 0 | **REMOVABLE in canonical dry-run** |
| `.claude/worktrees/advisor-heuristic-resolution-20261020` | `24a02a1e29a8` | 40.8 | ancestry: exit 0 | 13 dirty/untracked paths; 13 final path states differ from / are absent in RC; ignored material: `.benchmarks/`, `src/elspeth/web/frontend/node_modules` |
| `.claude/worktrees/ce410-clean-qualification-20261021` | `ce4103539fe9` | 16.9 | ancestry: exit 0 | ignored material: `.benchmarks/`, `src/elspeth/web/frontend/node_modules`, `tests/_scratch/` |
| `.claude/worktrees/composer-https-streaming-083-repairs-20261008` | `703cdd1674d0` | 322.1 | ancestry: exit 0 | 12 dirty/untracked paths; 6 final path states differ from / are absent in RC; ignored material: `.benchmarks/`, `src/elspeth/web/frontend/node_modules/` |
| `.claude/worktrees/composer-https-streaming-20261006` | `d2b73990d137` | 366.5 | ancestry: exit 0 | 809 dirty/untracked paths; 629 final path states differ from / are absent in RC; ignored material: `.benchmarks/`, `.claude/lanes/`, `.orchestrator/`, `node_modules/`, `src/elspeth/web/frontend/dist/`, `src/elspeth/web/frontend/node_modules/`, `tests/_scratch/` |
| `.claude/worktrees/quota-remediation-20261019` | `6e2a8852a56a` | 45.3 | ancestry: exit 0 | 3 observed processes; ignored material: `.benchmarks/`, `.claude/lanes/`, `src/elspeth/web/frontend/node_modules`, `src/elspeth/web/frontend/tsconfig.tsbuildinfo`, `tests/_scratch/` |
| `.claude/worktrees/release-084-20261010` | `4d5222633d5c` | 265.5 | ancestry: exit 0 | 8 dirty/untracked paths; 8 final path states differ from / are absent in RC; ignored material: `.benchmarks/`, `src/elspeth/web/frontend/node_modules` |
| `.claude/worktrees/release086-runtime-remediation-20261016` | `ac7e5aac43b4` | 130.4 | ancestry: exit 0 | 4 dirty/untracked paths; 4 final path states differ from / are absent in RC |
| `.claude/worktrees/research-084-20261010` | `cb6b40956e85` | 265.5 | ancestry: exit 0 | 8 dirty/untracked paths; 8 final path states differ from / are absent in RC; ignored material: `.benchmarks/` |
| `.claude/worktrees/sql-boundary-084-20261013` | `b96c33d00d85` | 192.5 | ancestry: exit 0 | 6 dirty/untracked paths; 6 final path states differ from / are absent in RC; ignored material: `.benchmarks/` |
| `.claude/worktrees/sql-boundary-084-merged299-20261013` | `9bd1b7ad18a0` | 191.4 | ancestry: exit 0 | 19 dirty/untracked paths; 5 final path states differ from / are absent in RC; ignored material: `.benchmarks/` |
| `.claude/worktrees/test-failure-repairs-20261020` | `d970e79be4f8` | 43.4 | ancestry: exit 0 | 83 dirty/untracked paths; 26 final path states differ from / are absent in RC; ignored material: `.benchmarks/`, `.claude/lanes/`, `src/elspeth/web/frontend/node_modules` |
| `.claude/worktrees/test-followup-20261020` | `0ca94d34231a` | 42.4 | ancestry: exit 0 | 134 dirty/untracked paths; 13 final path states differ from / are absent in RC; ignored material: `.benchmarks/`, `.claude/lanes/` |
| `.claude/worktrees/ux-review-20261006` | `23822a747762` | 358.1 | ancestry: exit 0 | 1 dirty/untracked paths; 1 final path states differ from / are absent in RC |
| `~/.codex/worktrees/composer07-rebaseline/elspeth` | `0d216f712fc9` | 32.8 | complete 11-path carry | ignored material: `.benchmarks/`, `.claude/lanes/`, `.elspeth/staged-reviews/`, `src/elspeth/web/frontend/node_modules/` |
| `.claude/worktrees/aca-first-launch-20261018` | `24ed4637f427` | 90.8 | complete 13-path carry | ignored material: `.benchmarks/`, `src/elspeth/web/frontend/dist/`, `src/elspeth/web/frontend/node_modules` |

## Carry proofs missed by `git cherry`

The source worktree is `~/.codex/worktrees/composer07-rebaseline/elspeth`. Its only source-only commit is `0d216f712fc9bc4452856f9a774a6ef2c598b052`, based on `2d5c0557e2d3dfcf6f176b94e9d17290d3da7a31`. `git cherry` reports `+` because the RC carries it inside the larger commit `67ad14c22b3987a16f0e9a71155102ee451da342`.

```text
source full patch-id:                   9dbd9570f67aa46871ecdace85739656b0f826f4
RC commit patch-id on all source paths: 9dbd9570f67aa46871ecdace85739656b0f826f4
source delta paths: 11
final RC mode/blob records matching: 11
final RC mode/blob records differing: 0
```

Every path in the source delta was included in the comparison, including modes and absence states. Matching titles or a partial cherry-pick was not used as proof. Its ignored `.elspeth/staged-reviews/` and lane material still require preservation/disposal review.

`.claude/worktrees/aca-first-launch-20261018` at `24ed4637f4271b9855e3881dfa067a87c5888198` is also fully carried. RC ancestor `31a6cc6adaf338783eda2617d1e279360522880d` has exactly the same full Git tree, and its parent is the source merge-base. This proves the full source merge payload despite a different commit history. Later RC changes explain final-head blob differences.

```text
source merge-base = target parent: cb078ab46ea66dd2259ef4024491a864eb0d1414
source tree = target ancestor tree: 541bc68386754e97303746b7a823b18c639714b7
aggregate source patch-id:          aef81c2757099975fc13ca7f3c0fa1a870318699
target patch-id on all 13 paths:    aef81c2757099975fc13ca7f3c0fa1a870318699
before/after mode/blob matches:     all 13 paths
```

## Older worktrees retained: inclusion not established

These are excluded from the candidate list. A `+` means Git did not find a whole-commit patch equivalent; `-` means it did. Merge commits are counted separately because `git cherry` omits them. Nonmatching final net-delta blobs and failed aggregate historical subset matching do not prove that no functionality was carried; they mean complete inclusion has not been established.

| Worktree path | HEAD | Age (h) | Raw cherry + / - / source-only merges |
|---|---|---:|---:|
| `~/.codex/worktrees/38f0/elspeth` | `acfcc58bd982` | 142.0 | 1 / 1 / 0 |
| `~/.codex/worktrees/4545/elspeth` | `e8ac256ef7dc` | 30.8 | 1 / 0 / 0 |
| `~/.codex/worktrees/composer-plus-20261020/elspeth` | `52543bcc1954` | 22.3 | 2 / 0 / 0 |
| `~/.codex/worktrees/d66a/elspeth` | `774b122851e6` | 143.5 | 22 / 9 / 2 |
| `~/.codex/worktrees/fcee/elspeth` | `b6a430a85e09` | 141.8 | 1 / 0 / 0 |
| `.claude/worktrees/084-ci-two-fixes-20261011` | `ddf656e6ce86` | 241.5 | 1 / 0 / 0 |
| `.claude/worktrees/085-integration-preview-20261011` | `23c7003b4b46` | 243.9 | 1 / 1 / 0 |
| `.claude/worktrees/advisor-cleanup-main-20261020` | `e6b247943f91` | 33.1 | 2 / 0 / 0 |
| `.claude/worktrees/composer-streaming-source-checkpoint-20261009` | `a7bf002c5f7e` | 297.7 | 6 / 0 / 0 |
| `.claude/worktrees/composer-streaming-source16-checkpoint-20261009` | `a358f15e3858` | 286.8 | 2 / 0 / 0 |
| `.claude/worktrees/composer-streaming-wip-backup-20261007` | `25882426efc5` | 328.0 | 1 / 0 / 0 |
| `.claude/worktrees/proof-084-20261010` | `9dcd25f643fa` | 265.5 | 3 / 0 / 0 |
| `.claude/worktrees/proof-activation-084-20261010` | `827a2145caf0` | 262.4 | 16 / 0 / 6 |
| `.claude/worktrees/proof-candidate-084-20261010` | `b56ca0d8634a` | 264.7 | 15 / 0 / 6 |
| `.claude/worktrees/proof-perf-084-20261010` | `955b84758ece` | 265.4 | 1 / 0 / 0 |
| `.claude/worktrees/proof-progress-084-20261010` | `aea6f4df4e59` | 265.2 | 1 / 0 / 0 |
| `.claude/worktrees/proof-retry-084-20261010` | `45edb51cf935` | 263.5 | 13 / 1 / 1 |
| `.claude/worktrees/proof-workflow-084-20261010` | `fe6320054c88` | 265.1 | 1 / 0 / 0 |
| `.claude/worktrees/proof-workflow-three-084-20261010` | `c245f5ba9044` | 263.0 | 11 / 1 / 1 |
| `.claude/worktrees/public-site-085-20261011` | `e9909cdcb8d3` | 244.6 | 1 / 0 / 0 |
| `.claude/worktrees/release-084-consolidated-20261012` | `bf39754f18d4` | 236.8 | 2 / 2 / 1 |
| `.claude/worktrees/release-085-20261011` | `cdb09bda5cb1` | 244.9 | 9 / 1 / 0 |
| `.claude/worktrees/release089-recovered-20261019` | `2fbf186a48ff` | 57.2 | 29 / 0 / 4 |
| `.claude/worktrees/runner-capacity-20261014` | `d0dc6d637654` | 178.3 | 1 / 0 / 0 |
| `.claude/worktrees/security-boundaries-20261020` | `b6e09a538b74` | 25.8 | 12 / 0 / 2 |

## Younger worktrees excluded by age

| Worktree path | HEAD | Age (h) |
|---|---|---:|
| `~/.codex/worktrees/2ab4/elspeth/.claude/worktrees/composer-continuation-clock-20261021` | `375f8af2bddd` | 1.6 |
| `~/.codex/worktrees/8bde/elspeth/.claude/worktrees/engine010-qualified-20261021` | `527b74e4e3ee` | 4.4 |
| `~/.codex/worktrees/checksum-dot10-2e869240/elspeth` | `1e9185d55da3` | 4.4 |
| `.claude/worktrees/composer-continuation-integration-20261021` | `51bb8131723b` | 4.6 |
| `.claude/worktrees/daybreak-deadline-qualification-20261021` | `0b8cfa9e92fa` | 1.4 |
| `.claude/worktrees/daybreak-final-qualification-20261021` | `2e869240a388` | 5.2 |
| `.claude/worktrees/daybreak-normal-qualification-20261021` | `b003e0cd9c6c` | 6.1 |
| `.claude/worktrees/daybreak-npm-qualification-20261021` | `820aa43c7525` | 3.0 |
| `.claude/worktrees/promptshield-dot10-20261021` | `98d06827d7ed` | 4.7 |
| `.claude/worktrees/promptshield-views-qualification-20261021` | `18095dfed15f` | 3.5 |
| `.claude/worktrees/release-0.8.9-docs-20261021` | `dbbb6582492c` | 0.5 |

## Verification and limits

- The canonical `scripts/worktree-cleanup.sh --base dbbb6582492ce1fb22941e0cc395aa20d4eb386d` dry-run exited **0**. It classified only `.claude/worktrees/advisor-heuristic-baseline-20261020` as REMOVABLE. Its ancestry-only UNMERGED classifications for the two fully carried sources above are superseded by the explicit patch/blob proofs, not by a deletion command.
- Worktree HEAD/branch registrations matched before and after the initial instrument. Every Git status read used `--no-optional-locks`; no working-tree diff or reset was run. No tests/builds or CI reruns were launched for this audit.
- Final registry/HEAD recheck returned `registry_HEADs_unchanged=YES; registered=59`. A final path-filtered canonical dry-run again returned `removable=1` and exit **0** for `advisor-heuristic-baseline-20261020`.
- The independent scan covered all 27 older worktrees initially unproven by ancestry/whole-commit cherry matching. It checked their aggregate deltas and each remaining nonmerge source patch against target history since the respective merge-base. Both instruments exited **0**. The other 25 retain at least one unmatched nonmerge patch. Patches split across multiple target commits, conflict-adjusted implementations and semantic equivalence were not guessed; these cases remain unproven.
- Controls passed for positive/negative ancestry, strict age boundary, cherry inclusion with a mutated `+` result, missing/merge guards, known blob identity with mutated bytes, and stable patch-id with a mutated added line. The independent carry detector also rejected a missing path member, mutated blob and mutated patch hunk.
- `/proc/*/cwd` supplied observed process use. 716 process cwd entries were inaccessible, so an empty observed-process list is not a guarantee that no other user has a process there. Active services/tools may also have no cwd inside a worktree. Recheck custody and the current HEAD/status before any deletion.
- Ignored artifacts were inventoried using the canonical helper’s cache exclusions; contents of held artifacts were not declared disposable. The two older worktrees containing young registered children must be retained as parent directories.
- The candidate list binds the exact RC SHA above. Re-evaluate if PR #328 changes. Green auxiliary workflows are not a substitute for the still-pending CI run.

Local working evidence is under `.claude/lanes/worktree-audit-20261021/`: `inventory.json` (all registered trees, birth times, HEADs, status, ignored entries, processes), `audit-output.txt` (raw ancestry/cherry counts), `preservation.json` (dirty path/blob reconciliation), `controls.json`, `crosscheck.json` (aggregate patch/blob proofs), `individual-crosscheck.json`, `aca-squash-blobs.json`, `inclusion-crosscheck.md`, `cleanup-dry-run.txt`, `final-ready-dry-run.txt`, and the corresponding instruments. These lane files are intentionally untracked and must not be staged.
