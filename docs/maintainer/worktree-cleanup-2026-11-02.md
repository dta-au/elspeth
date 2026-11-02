# Historical worktree cleanup — 2026-11-02

Completed under John's authority to clean obsolete Elspeth worktrees **more
than 24 hours old**, oldest first, retaining unique content and excluding
the primary release checkout and active or uncertain writer/test contexts.
The final verification was at **2026-11-02 07:57:36 AEDT**
(`2026-11-01T20:57:36.586730Z`).

## Result and source of truth

**121 worktrees were removed. The authoritative Git registry went from 131
entries to 10.** Every removed directory and registration was checked absent;
all 113 named source branches from those trees still resolved to their exact
recorded tips. The other eight removed trees had detached HEADs; their tip
histories remain recoverable through the retained release ancestry and/or
verified bundles, as recorded individually.

Raw final verification output:

```text
initial_registry_entries: 131
removed_records: 121
current_registry_entries: 10
held_initial_entries: 10
new_registered_paths: []
retained_named_refs_verified: 113
failures: []
current_primary_branch: release/0.8.10-assembly
current_primary_head: 53945ac23d68997b027866fcd62c1503f3a7125c
```

The record of authority, inventory and original per-tree command receipts is
`.claude/lanes/worktree-cleanup-20261102/`. The complete final ledger is also
retained in existing local recovery storage:

`recovery/0.8.10-20261031-consolidation/worktree-cleanup-20261102/ledger.json`

Its sibling `final-verification.json` contains the raw final registry and
commit-object checks. Individual `NNN-<name>.json` receipts point to exact
bundle and file-preservation locations. Raw paths and local context remain
private in that ignored recovery area; no upload was performed.

## Age, order and custody

The intake inventory records 121 non-primary entries with a directory or Git
registration ctime already more than 24 hours old. The earliest observed of
those two times supplies a **conservative age lower bound**, not an exact
creation date. The 2026-10-21 inventory's original creation dates are retained
where available. Candidate IDs follow this observed-age ordering. Initial
passes held trees needing evidence preservation and returned to them in the
same ordering; a registered child was removed before its parent.

The primary checkout and nine recent/uncertain source or test contexts remain.
A recent ctime alone does not prove a directory was originally created within
24 hours; those entries did not have qualifying age established in this pass
and were excluded. They include the current `56b` baseline, G5/G6 and primary
budget contexts, three recent donor trees and both lock-hold bisection trees.

Before removal, checks covered host process cwd and command references,
HEAD stability, Git locks, nonignored status and ignored paths. OS-denied
process reads are recorded per candidate, so the result is not a claim that
every system process was inspectable. Later admission also explicitly checked
unfinished Git operations and retained worktree HEAD reflog data. No active
process was terminated and the implementation driver was not contacted.

The broad `work-at-risk.sh` traversal was deferred under the explicit low-I/O,
bounded-per-tree scope. No whole-repository ignored-source census or total
disk-space-reclaimed figure is claimed.

## Preservation and verification

Preservation categories below count candidates; bundle coverage overlaps the
three mutually exclusive file-preservation categories.

| Recorded preservation | Candidates | Evidence |
|---|---:|---|
| Incremental Git bundle | 116 | Native bundle verification, exact listed tip, SHA-256, pack checksum, native dry-run unpack and corrupt-pack rejection |
| Committed history only; no meaningful local bytes to preserve | 45 | Retained refs/release ancestry and readable commit/tree objects |
| Local file archive | 58 | Full selected-file/source readback; modes and symlinks checked; original indexes and staged blob bytes included for both dirty trees |
| Original ignored directories/files moved into recovery | 18 | Same-filesystem atomic renames; original root device/inode/mode/size/mtime retained; bounded readable-file sentinels checked |

The two dirty historical trees were `b3-base-20261025` and
`receiving-controls-20261026`. Their original working bytes, raw indexes,
staged blobs and absence states were preserved and read back before only
their exact historical dirty paths were normalized for non-force removal.
Nothing from those trees was integrated into the release checkout.

For larger ignored evidence sets, the original directories now live under
`NNN-<name>-original-ignored/`, with `relocation.json` recording every original
and retained root. Atomic rename preserved the original inodes and contents,
including special test artifacts, without a recursive copying/deletion pass.
These are original-byte retention claims, **not** an exhaustive descendant
hash verification. Final verification checked retained root identities again.

Bundles are incremental and depend on the prerequisite commit(s) listed in
their headers; their receipts identify that boundary. They are not standalone
repository backups. Named source branches were retained, and no branch was
created as an archive. No GC, object pruning, force worktree removal, reset,
stash, deployment, provider call, release integration or primary-index edit
was performed. The report is deliberately left unstaged in the shared checkout.

No separate recovery checkout or independent agent review was created. The
final pass checked current registry/disk/ref state, readable commit objects,
archive presence/size and retained original inode identities. Completed
pre-removal pack and file-byte validations are recorded in the individual
receipts; they were not all rehashed again at handover. Raw database bytes
were preserved; database restoration was not qualified.

Earlier removed worktree HEAD reflogs were not separately archived; named
branch refs/logs remain, and no unreachable-object inventory or GC was run.
Current-tip history recovery is the verified Git boundary. This report makes
no exhaustive claim about previously abandoned, unreachable history.

## Archaeology: exact carries, revised implementations and unresolved reserves

Every committed-history comparison below is pinned to release
`53945ac23d68997b027866fcd62c1503f3a7125c`.
`git merge-base`, ancestry, native diff statistics, complete outside-release
logs and `git cherry` results are retained per tree. Git cherry's `-` denotes
an exact patch equivalent, including rebased/cherry-picked changes whose
commit IDs differ. Its `+` only means no exact patch match; it does not prove
missing functionality, and it does not evaluate merge payloads or semantic
equivalence. No historical source was automatically replayed.

Two historical squash carries were re-proved from immutable Git objects:

- `composer07-rebaseline` at `0d216f712fc9bc4452856f9a774a6ef2c598b052`
  is carried in release ancestor `67ad14c22b3987a16f0e9a71155102ee451da342`.
  The full source patch and that carry restricted to **all eleven source paths**
  have patch ID `9dbd9570f67aa46871ecdace85739656b0f826f4`; all eleven final
  mode/blob records match. Whole-commit Git cherry alone reports `+`.
- `aca-first-launch` at `24ed4637f4271b9855e3881dfa067a87c5888198`
  and release ancestor `31a6cc6adaf338783eda2617d1e279360522880d`
  have the same complete tree `541bc68386754e97303746b7a823b18c639714b7`.
  This is a complete historical tree carry, despite different history.

`historical-squash-carries.json` retains these native outputs. Later release
edits may differ from those historical carry cuts.

The preserved reserves below are substantial, but their overlapping deltas
must not be summed or labeled wholesale as missing implementation:

| Preserved history | Measured size/provenance | Disposition |
|---|---|---|
| October 7 streaming WIP, `25882426efc51211a2df3095010ebf092f27f12f` | Compressed patch 830,781 bytes; decompressed 4,608,560 bytes; embedded inventory 314 paths | Both recorded digests verified. 92 path hashes match release, 209 differ, 13 are absent; semantic gaps are not established by those differences. This commit stores a historical patch artifact rather than installing it in its runtime tree. |
| October 9 checkpoint, `a7bf002c5f7e8b55564cf500742cb1976c383afb` | Native merge-base-to-tip delta has 2,616 paths: 34 under `src/`, 8 under `tests/`, 2,574 other paths, largely checkpoint payloads/docs | Source branch and verified 10,060,922-byte incremental bundle retained. The path total is not a count of missing production files. |
| U45 prototype/profile, `5932af0e95c67c8a4674d46a0441facc10a48b99` | Delta spans 213 paths under `src/` and 391 under `tests/`; Git cherry gives seven equivalents and three unmatched non-merge commits | Original WIP/prototype history preserved. Its prototype parent `897dabeb8968c880d7ddf819c06ec360f438fb38` is an ancestor of this profile, not a second independent reserve. |
| P4/U37 receiving cut, `a6753fa558ae57e8b7c509f4e81747addc9d7b31` | Delta spans 172 paths under `src/` and 276 under `tests/`; Git cherry gives 43 unmatched non-merge commits | Preserved private WIP/source joins; no blanket missing-functionality or release-acceptance claim. |
| U45 stage memo, `0348eecac264aa20f7d204a0dc139f56a5d63f02` | Source overlaps later receiving code despite whole-history divergence | Concrete checks below show existing carries and later revision; blind replay would duplicate or regress newer work. |

Three bounded source checks sharpen the last finding:

- `src/elspeth/web/credential_guard.py` is byte-identical between the U45
  stage-memo cut and the pinned release.
- `src/elspeth/web/continuation_retention_memo.py` differs only in its module
  docstring. Every byte after the module docstring matches. The memo
  implementation is already carried.
- `src/elspeth/web/composer/continuation_codec.py` exists at both cuts and has
  later changes, including `encode_mandatory_private_chunk` in the release.
  This is evidence of later revision, not a missing historical file or a
  semantic-equivalence proof for the complete codec.

`u45-current-source-overlap.json` and `u45-retention-memo-current.diff` retain
the blob digests, definitions and actual memo delta. The remaining private
commit families need source-by-source semantic reconciliation if implementation
questions are pursued. This cleanup established no additional missing-runtime
finding and did not qualify any historical WIP for integration.

## I/O posture

All candidate operations, archives, moves and removals ran serially. The later
pass used `ionice -c3` and `nice -n10`. A recorded `full avg10=5.76` caused a
yield before candidate 103; work resumed after pressure subsided. Final raw
pressure was:

```text
some avg10=0.00 avg60=0.18 avg300=0.55
full avg10=0.00 avg60=0.18 avg300=0.54
```

No TRIM, server setting change or process kill was used. Tests and broad
source scans were not run for this custody-only task.

## Per-tree record

Order follows the intake's observed age. Age is the conservative lower bound
in hours at intake. Paths starting with `~/.codex/` are outside the main
checkout; other paths are relative to the project root. `NNN` identifies the
full candidate record and its sibling recovery receipt. Exact source SHA,
branch, working/index status, ignored paths, commands and preservation
locations are in those records. All removed named refs remain unchanged.

| Order | Original worktree | Age h | History disposition | Preserved ref / receipt | Outcome |
|---:|---|---:|---|---|---|
| 000 | `.claude/worktrees/composer-streaming-wip-backup-20261007` | 602.7 | Git cherry: -0 / +1 | `backup/composer-streaming-wip-20261007`; `000-composer-streaming-wip-backup-20261007.json` | REMOVED; path + registry absent |
| 001 | `.claude/worktrees/composer-streaming-source-checkpoint-20261009` | 572.5 | Git cherry: -0 / +6 | `backup/composer-streaming-proof-wip-20261009`; `001-composer-streaming-source-checkpoint-20261009.json` | REMOVED; path + registry absent |
| 002 | `.claude/worktrees/composer-streaming-source16-checkpoint-20261009` | 561.6 | Git cherry: -0 / +2 | `backup/composer-streaming-source-20261009`; `002-composer-streaming-source16-checkpoint-20261009.json` | REMOVED; path + registry absent |
| 003 | `.claude/worktrees/proof-084-20261010` | 540.1 | Git cherry: -0 / +3 | `work/proof-084-20261010`; `003-proof-084-20261010.json` | REMOVED; path + registry absent |
| 004 | `.claude/worktrees/proof-perf-084-20261010` | 540.1 | Git cherry: -0 / +1 | `work/proof-perf-084-20261010`; `004-proof-perf-084-20261010.json` | REMOVED; path + registry absent |
| 005 | `.claude/worktrees/proof-progress-084-20261010` | 540.0 | Git cherry: -0 / +1 | `work/proof-progress-084-20261010`; `005-proof-progress-084-20261010.json` | REMOVED; path + registry absent |
| 006 | `.claude/worktrees/proof-workflow-084-20261010` | 539.8 | Git cherry: -0 / +1 | `work/proof-workflow-084-20261010`; `006-proof-workflow-084-20261010.json` | REMOVED; path + registry absent |
| 007 | `.claude/worktrees/proof-candidate-084-20261010` | 539.4 | Git cherry: -0 / +15 | `work/proof-candidate-084-20261010`; `007-proof-candidate-084-20261010.json` | REMOVED; path + registry absent |
| 008 | `.claude/worktrees/proof-retry-084-20261010` | 538.2 | Git cherry: -1 / +13 | `work/proof-retry-084-20261010`; `008-proof-retry-084-20261010.json` | REMOVED; path + registry absent |
| 009 | `.claude/worktrees/proof-workflow-three-084-20261010` | 537.7 | Git cherry: -1 / +11 | `work/proof-workflow-three-084-20261010`; `009-proof-workflow-three-084-20261010.json` | REMOVED; path + registry absent |
| 010 | `.claude/worktrees/proof-activation-084-20261010` | 537.0 | Git cherry: -0 / +16 | `work/proof-activation-084-20261010`; `010-proof-activation-084-20261010.json` | REMOVED; path + registry absent |
| 011 | `.claude/worktrees/release-085-20261011` | 519.7 | Git cherry: -1 / +9 | `release/0.8.5`; `011-release-085-20261011.json` | REMOVED; path + registry absent |
| 012 | `.claude/worktrees/public-site-085-20261011` | 519.3 | Git cherry: -0 / +1 | `codex/public-site-085-20261011`; `012-public-site-085-20261011.json` | REMOVED; path + registry absent |
| 013 | `.claude/worktrees/085-integration-preview-20261011` | 518.6 | Git cherry: -1 / +1 | `codex/085-integration-preview-20261011`; `013-085-integration-preview-20261011.json` | REMOVED; path + registry absent |
| 014 | `.claude/worktrees/084-ci-two-fixes-20261011` | 516.2 | Git cherry: -0 / +1 | `codex/084-ci-two-fixes-20261011`; `014-084-ci-two-fixes-20261011.json` | REMOVED; path + registry absent |
| 015 | `.claude/worktrees/release-084-consolidated-20261012` | 511.5 | Git cherry: -2 / +2 | `codex/release-084-consolidated-20261012`; `015-release-084-consolidated-20261012.json` | REMOVED; path + registry absent |
| 016 | `.claude/worktrees/runner-capacity-20261014` | 453.0 | Git cherry: -0 / +1 | `codex/runner-capacity-20261014`; `016-runner-capacity-20261014.json` | REMOVED; path + registry absent |
| 017 | `.claude/worktrees/aca-first-launch-20261018` | 357.0 | Historical full-tree squash carry proven | `codex/aca-first-launch-20261018`; `017-aca-first-launch-20261018.json` | REMOVED; path + registry absent |
| 018 | `.claude/worktrees/quota-remediation-20261019` | 320.0 | Exact HEAD ancestor | `codex/quota-remediation-20261019`; `018-quota-remediation-20261019.json` | REMOVED; path + registry absent |
| 019 | `.claude/worktrees/test-failure-repairs-20261020` | 318.0 | Git cherry: -0 / +1 | `codex/test-failure-repairs-20261020`; `019-test-failure-repairs-20261020.json` | REMOVED; path + registry absent |
| 020 | `.claude/worktrees/test-followup-20261020` | 316.4 | Git cherry: -0 / +1 | `codex/test-followup-20261020`; `020-test-followup-20261020.json` | REMOVED; path + registry absent |
| 021 | `.claude/worktrees/advisor-heuristic-resolution-20261020` | 315.5 | Git cherry: -0 / +1 | `codex/advisor-heuristic-resolution-20261020`; `021-advisor-heuristic-resolution-20261020.json` | REMOVED; path + registry absent |
| 022 | `.claude/worktrees/advisor-cleanup-main-20261020` | 307.9 | Git cherry: -0 / +2 | `codex/advisor-cleanup-alias-20261020`; `022-advisor-cleanup-main-20261020.json` | REMOVED; path + registry absent |
| 023 | `~/.codex/worktrees/composer-plus-20261020/elspeth` | 285.4 | Git cherry: -2 / +1 | `codex/composer-plus-20261020`; `023-elspeth.json` | REMOVED; path + registry absent |
| 024 | `~/.codex/worktrees/4545/elspeth` | 281.1 | Git cherry: -0 / +1 | `codex/composer-preflight-guard-checksum-20261020`; `024-elspeth.json` | REMOVED; path + registry absent |
| 025 | `~/.codex/worktrees/composer-request-rejection-20261020/elspeth` | 280.1 | Git cherry: -0 / +1 | `codex/composer-request-rejection-20261020`; `025-elspeth.json` | REMOVED; path + registry absent |
| 026 | `~/.codex/worktrees/tool-contract-audit-20261020/elspeth` | 279.3 | Git cherry: -0 / +1 | `codex/tool-contract-audit-20261020`; `026-elspeth.json` | REMOVED; path + registry absent |
| 027 | `~/.codex/worktrees/checksum-dot10-2e869240/elspeth` | 279.2 | Git cherry: -0 / +1 | `codex/checksum-dot10-2e869240`; `027-elspeth.json` | REMOVED; path + registry absent |
| 028 | `.claude/worktrees/promptshield-dot10-20261021` | 278.5 | Git cherry: -0 / +5 | `codex/promptshield-dot10-20261021`; `028-promptshield-dot10-20261021.json` | REMOVED; path + registry absent |
| 029 | `~/.codex/worktrees/2ab4/elspeth/.claude/worktrees/composer-continuation-clock-20261021` | 276.2 | Git cherry: -4 / +0 | `codex/composer-continuation-clock-20261021`; `029-composer-continuation-clock-20261021.json` | REMOVED; path + registry absent |
| 030 | `.claude/worktrees/tool011-validate-secret-ref-20261021` | 271.9 | Git cherry: -0 / +1 | `codex/tool011-validate-secret-ref-20261021`; `030-tool011-validate-secret-ref-20261021.json` | REMOVED; path + registry absent |
| 031 | `.claude/worktrees/tool011-list-secret-refs-20261021` | 271.7 | Git cherry: -1 / +0 | `codex/tool011-list-secret-refs-20261021`; `031-tool011-list-secret-refs-20261021.json` | REMOVED; path + registry absent |
| 032 | `.claude/worktrees/tool011-gate1-capability-20261021` | 271.7 | Git cherry: -0 / +1 | `codex/tool011-gate1-capability-20261021`; `032-tool011-gate1-capability-20261021.json` | REMOVED; path + registry absent |
| 033 | `.claude/worktrees/uploaded-file-node-spec-20261021` | 271.6 | Git cherry: -0 / +2 | `codex/uploaded-file-node-spec-20261021`; `033-uploaded-file-node-spec-20261021.json` | REMOVED; path + registry absent |
| 034 | `.claude/worktrees/security-boundaries-20261020` | 271.6 | Git cherry: -10 / +9 | `codex/security-boundaries-20261020`; `034-security-boundaries-20261020.json` | REMOVED; path + registry absent |
| 035 | `.claude/worktrees/tool011-mcp-new-session-20261022` | 270.9 | Git cherry: -0 / +1 | `codex/tool011-mcp-new-session-20261022`; `035-tool011-mcp-new-session-20261022.json` | REMOVED; path + registry absent |
| 036 | `.claude/worktrees/tool011-wire-secret-ref-20261022` | 270.7 | Git cherry: -0 / +1 | `codex/tool011-wire-secret-ref-20261022`; `036-tool011-wire-secret-ref-20261022.json` | REMOVED; path + registry absent |
| 037 | `.claude/worktrees/tool011-mcp-save-session-20261022` | 270.0 | Git cherry: -0 / +1 | `codex/tool011-mcp-save-session-20261022`; `037-tool011-mcp-save-session-20261022.json` | REMOVED; path + registry absent |
| 038 | `.claude/worktrees/tool011-mcp-load-session-20261022` | 270.0 | Git cherry: -0 / +1 | `codex/tool011-mcp-load-session-20261022`; `038-tool011-mcp-load-session-20261022.json` | REMOVED; path + registry absent |
| 039 | `.claude/worktrees/composer-fork-guidance-20261022` | 269.3 | Git cherry: -7 / +1 | `codex/composer-fork-guidance-20261022`; `039-composer-fork-guidance-20261022.json` | REMOVED; path + registry absent |
| 040 | `~/.codex/worktree-archives/elspeth/20261022-legacy-release-cleanup/retained-worktrees/daybreak-deadline-qualification-20261021` | 268.5 | Git cherry: -1 / +0 | `preserve/0.8.10-daybreak-deadline-qualification-20261024`; `040-daybreak-deadline-qualification-20261021.json` | REMOVED; path + registry absent |
| 041 | `~/.codex/worktree-archives/elspeth/20261022-legacy-release-cleanup/retained-worktrees/daybreak-npm-qualification-20261021` | 268.5 | Git cherry: -1 / +0 | `preserve/0.8.10-daybreak-npm-qualification-20261024`; `041-daybreak-npm-qualification-20261021.json` | REMOVED; path + registry absent |
| 042 | `~/.codex/worktree-archives/elspeth/20261022-legacy-release-cleanup/retained-worktrees/release089-recovered-20261019` | 268.5 | Git cherry: -0 / +30 | `codex/recover089-20261019`; `042-release089-recovered-20261019.json` | REMOVED; path + registry absent |
| 043 | `~/.codex/worktree-archives/elspeth/20261022-legacy-release-cleanup/retained-worktrees/composer07-rebaseline` | 268.5 | Historical 11-path squash carry proven | `codex/composer07-rebaseline-20261020`; `043-composer07-rebaseline.json` | REMOVED; path + registry absent |
| 044 | `.claude/worktrees/tool011-mcp-list-sessions-20261022` | 267.7 | Git cherry: -0 / +1 | `codex/tool011-mcp-list-sessions-20261022`; `044-tool011-mcp-list-sessions-20261022.json` | REMOVED; path + registry absent |
| 045 | `.claude/worktrees/tool011-mcp-delete-session-20261022` | 267.7 | Git cherry: -0 / +1 | `codex/tool011-mcp-delete-session-20261022`; `045-tool011-mcp-delete-session-20261022.json` | REMOVED; path + registry absent |
| 046 | `.claude/worktrees/tool011-contract-enforcement-design-20261022` | 266.1 | Git cherry: -0 / +1 | `codex/tool011-contract-enforcement-design-20261022`; `046-tool011-contract-enforcement-design-20261022.json` | REMOVED; path + registry absent |
| 047 | `.claude/worktrees/release089-sol-scan-20261022` | 247.8 | Exact HEAD ancestor | `codex/release089-sol-scan-20261022`; `047-release089-sol-scan-20261022.json` | REMOVED; path + registry absent |
| 048 | `.claude/worktrees/composer-continuation-integration-20261021` | 244.6 | Git cherry: -7 / +3 | `codex/composer-continuation-integration-20261021`; `048-composer-continuation-integration-20261021.json` | REMOVED; path + registry absent |
| 049 | `~/.codex/worktrees/composer-batch-qualification-20261023/elspeth` | 237.7 | Git cherry: -7 / +3 | `preserve/0.8.10-batch-qualification-20261024`; `049-elspeth.json` | REMOVED; path + registry absent |
| 050 | `.claude/worktrees/carve-partition-20261024` | 203.4 | Git cherry: -2 / +2 | `codex/0.8.10-carve-partition`; `050-carve-partition-20261024.json` | REMOVED; path + registry absent |
| 051 | `.claude/worktrees/u10-security-20261024` | 202.8 | Git cherry: -17 / +0 | `codex/0.8.10-u10-security`; `051-u10-security-20261024.json` | REMOVED; path + registry absent |
| 052 | `.claude/worktrees/gov-tooling-20261024` | 202.6 | Git cherry: -2 / +1 | `codex/0.8.10-governance-tooling`; `052-gov-tooling-20261024.json` | REMOVED; path + registry absent |
| 053 | `.claude/worktrees/ecs-aca-20261024` | 202.3 | Git cherry: -6 / +0 | `codex/0.8.10-ecs-aca`; `053-ecs-aca-20261024.json` | REMOVED; path + registry absent |
| 054 | `.claude/worktrees/design-runtime-20261024` | 202.2 | Git cherry: -14 / +0 | `codex/0.8.10-design-runtime`; `054-design-runtime-20261024.json` | REMOVED; path + registry absent |
| 055 | `.claude/worktrees/design-protocol-20261024` | 202.2 | Git cherry: -11 / +0 | `codex/0.8.10-design-protocol`; `055-design-protocol-20261024.json` | REMOVED; path + registry absent |
| 056 | `.claude/worktrees/u45-profile-20261024` | 202.1 | Git cherry: -7 / +3 | `codex/0.8.10-u45-fix-prototype`; `056-u45-profile-20261024.json` | REMOVED; path + registry absent |
| 057 | `.claude/worktrees/issue327-light-20261024` | 202.0 | Git cherry: -11 / +0 | `codex/0.8.10-issue327-light`; `057-issue327-light-20261024.json` | REMOVED; path + registry absent |
| 058 | `.claude/worktrees/gate1-fixes-20261024` | 201.9 | Git cherry: -2 / +0 | `codex/0.8.10-gate1-fixes`; `058-gate1-fixes-20261024.json` | REMOVED; path + registry absent |
| 059 | `.claude/worktrees/u46-overlay-a6e812e86` | 201.9 | Git cherry: -0 / +4 | `detached tip in recovery/release ancestry`; `059-u46-overlay-a6e812e86.json` | REMOVED; path + registry absent |
| 060 | `.claude/worktrees/u10-judge-shell-20261024` | 201.6 | Git cherry: -2 / +0 | `codex/0.8.10-u10-judge-shell`; `060-u10-judge-shell-20261024.json` | REMOVED; path + registry absent |
| 061 | `.claude/worktrees/stage-preview-agent` | 200.8 | Git cherry: -1 / +0 | `codex/0.8.10-stage-preview-agent`; `061-stage-preview-agent.json` | REMOVED; path + registry absent |
| 062 | `.claude/worktrees/u23-u24-20261024` | 200.7 | Git cherry: -3 / +0 | `codex/0.8.10-u23-u24`; `062-u23-u24-20261024.json` | REMOVED; path + registry absent |
| 063 | `.claude/worktrees/u53-notes-20261024` | 200.5 | Git cherry: -2 / +0 | `codex/0.8.10-u53-notes`; `063-u53-notes-20261024.json` | REMOVED; path + registry absent |
| 064 | `.claude/worktrees/composer-carries-20261024` | 200.4 | Git cherry: -1 / +0 | `codex/0.8.10-composer-carries`; `064-composer-carries-20261024.json` | REMOVED; path + registry absent |
| 065 | `.claude/worktrees/overlay-findings-20261024` | 200.1 | Git cherry: -1 / +0 | `codex/0.8.10-overlay-security-findings`; `065-overlay-findings-20261024.json` | REMOVED; path + registry absent |
| 066 | `.claude/worktrees/carve-b2-scratch-20261024` | 199.5 | Git cherry: -7 / +2 | `detached tip in recovery/release ancestry`; `066-carve-b2-scratch-20261024.json` | REMOVED; path + registry absent |
| 067 | `~/.codex/worktrees/2ab4/elspeth` | 199.4 | Git cherry: -0 / +2 | `codex/composer-continuation-plan-20261019`; `067-elspeth.json` | REMOVED; path + registry absent |
| 068 | `.claude/worktrees/continuation-overlay-20261024` | 199.3 | Git cherry: -1 / +9 | `codex/0.8.10-continuation-overlay`; `068-continuation-overlay-20261024.json` | REMOVED; path + registry absent |
| 069 | `.claude/worktrees/overlay-sec-fixes` | 199.0 | Git cherry: -2 / +8 | `codex/0.8.10-overlay-sec-fixes`; `069-overlay-sec-fixes.json` | REMOVED; path + registry absent |
| 070 | `.claude/worktrees/u22-20261025` | 198.8 | Git cherry: -0 / +8 | `codex/0.8.10-u22`; `070-u22-20261025.json` | REMOVED; path + registry absent |
| 071 | `.claude/worktrees/f1-continuation-revoke` | 198.5 | Git cherry: -1 / +0 | `codex/0.8.10-f1-continuation-revoke`; `071-f1-continuation-revoke.json` | REMOVED; path + registry absent |
| 072 | `.claude/worktrees/runtime-u38-verify-20261025` | 198.1 | Git cherry: -0 / +3 | `codex/0.8.10-runtime-u38`; `072-runtime-u38-verify-20261025.json` | REMOVED; path + registry absent |
| 073 | `.claude/worktrees/u46-doc` | 197.8 | Git cherry: -0 / +13 | `codex/0.8.10-u46-diagnosis`; `073-u46-doc.json` | REMOVED; path + registry absent |
| 074 | `.claude/worktrees/u46-fix` | 197.6 | Git cherry: -1 / +3 | `codex/0.8.10-u46-fix`; `074-u46-fix.json` | REMOVED; path + registry absent |
| 075 | `.claude/worktrees/envelope-walker` | 197.2 | Git cherry: -2 / +0 | `codex/0.8.10-envelope-walker`; `075-envelope-walker.json` | REMOVED; path + registry absent |
| 076 | `.claude/worktrees/advisor-quota-oracle` | 197.0 | Git cherry: -1 / +0 | `codex/0.8.10-advisor-quota-oracle`; `076-advisor-quota-oracle.json` | REMOVED; path + registry absent |
| 077 | `.claude/worktrees/b3-base-20261025` | 196.8 | Git cherry: -4 / +0 | `detached tip in recovery/release ancestry`; `077-b3-base-20261025.json` | REMOVED; path + registry absent |
| 078 | `.claude/worktrees/attachment-fixture-20261025` | 196.7 | Git cherry: -1 / +0 | `codex/0.8.10-attachment-history-fixture`; `078-attachment-fixture-20261025.json` | REMOVED; path + registry absent |
| 079 | `.claude/worktrees/t5d-measure` | 196.4 | Exact HEAD ancestor | `detached tip in recovery/release ancestry`; `079-t5d-measure.json` | REMOVED; path + registry absent |
| 080 | `.claude/worktrees/t5d-admitted-fakes` | 196.3 | Git cherry: -1 / +0 | `codex/0.8.10-t5d-admitted-fakes`; `080-t5d-admitted-fakes.json` | REMOVED; path + registry absent |
| 081 | `.claude/worktrees/t5d-gates` | 196.1 | Git cherry: -3 / +0 | `codex/0.8.10-t5d-gates`; `081-t5d-gates.json` | REMOVED; path + registry absent |
| 082 | `.claude/worktrees/t5e` | 195.5 | Git cherry: -0 / +1 | `codex/0.8.10-t5e-metadata-writer`; `082-t5e.json` | REMOVED; path + registry absent |
| 083 | `.claude/worktrees/issue327-tc006-landing-20261025` | 195.1 | Git cherry: -4 / +2 | `codex/0.8.10-issue327-tc006-landing`; `083-issue327-tc006-landing-20261025.json` | REMOVED; path + registry absent |
| 084 | `.claude/worktrees/runtime-20261025` | 194.2 | Git cherry: -0 / +22 | `codex/0.8.10-runtime-u35b`; `084-runtime-20261025.json` | REMOVED; path + registry absent |
| 085 | `.claude/worktrees/issue327-f3-20261025` | 194.2 | Git cherry: -4 / +17 | `codex/0.8.10-issue327-f3`; `085-issue327-f3-20261025.json` | REMOVED; path + registry absent |
| 086 | `.claude/worktrees/u45-fix-20261025` | 194.0 | Git cherry: -0 / +9 | `codex/0.8.10-u45-fix`; `086-u45-fix-20261025.json` | REMOVED; path + registry absent |
| 087 | `.claude/worktrees/u45-base-20261025` | 194.0 | Exact HEAD ancestor | `detached tip in recovery/release ancestry`; `087-u45-base-20261025.json` | REMOVED; path + registry absent |
| 088 | `.claude/worktrees/u45-bisect-20261025` | 194.0 | Git cherry: -0 / +3 | `detached tip in recovery/release ancestry`; `088-u45-bisect-20261025.json` | REMOVED; path + registry absent |
| 089 | `.claude/worktrees/issue327-u17-20261025` | 193.9 | Git cherry: -4 / +20 | `codex/0.8.10-issue327-u17`; `089-issue327-u17-20261025.json` | REMOVED; path + registry absent |
| 090 | `.claude/worktrees/overlay-fixes-20261025` | 193.5 | Git cherry: -1 / +6 | `codex/0.8.10-reserve-repins`; `090-overlay-fixes-20261025.json` | REMOVED; path + registry absent |
| 091 | `.claude/worktrees/protocol-20261025` | 193.5 | Git cherry: -0 / +17 | `codex/0.8.10-protocol`; `091-protocol-20261025.json` | REMOVED; path + registry absent |
| 092 | `.claude/worktrees/u02-engine-20261024` | 193.5 | Git cherry: -0 / +5 | `codex/0.8.10-u02-engine`; `092-u02-engine-20261024.json` | REMOVED; path + registry absent |
| 093 | `.claude/worktrees/issue327-rest-20261025` | 193.5 | Git cherry: -0 / +23 | `codex/0.8.10-issue327-rest`; `093-issue327-rest-20261025.json` | REMOVED; path + registry absent |
| 094 | `.claude/worktrees/residue-20261025` | 193.2 | Git cherry: -0 / +3 | `codex/0.8.10-codec-residue`; `094-residue-20261025.json` | REMOVED; path + registry absent |
| 095 | `.claude/worktrees/u30-20261025` | 192.9 | Git cherry: -0 / +2 | `codex/0.8.10-u30`; `095-u30-20261025.json` | REMOVED; path + registry absent |
| 096 | `.claude/worktrees/physical-audit-20261025` | 192.4 | Git cherry: -0 / +1 | `codex/0.8.10-tier-analysis`; `096-physical-audit-20261025.json` | REMOVED; path + registry absent |
| 097 | `.claude/worktrees/protocol-basescan` | 192.2 | Git cherry: -0 / +10 | `codex/0.8.10-residue-u45-integration`; `097-protocol-basescan.json` | REMOVED; path + registry absent |
| 098 | `.claude/worktrees/exceptions-20261025` | 192.0 | Git cherry: -0 / +9 | `codex/0.8.10-exceptions`; `098-exceptions-20261025.json` | REMOVED; path + registry absent |
| 099 | `.claude/worktrees/runtime-u35b-land` | 190.2 | Git cherry: -0 / +1 | `codex/0.8.10-runtime-u35b-land`; `099-runtime-u35b-land.json` | REMOVED; path + registry absent |
| 100 | `.claude/worktrees/runtime-u37` | 190.1 | Git cherry: -0 / +1 | `codex/0.8.10-runtime-u37`; `100-runtime-u37.json` | REMOVED; path + registry absent |
| 101 | `.claude/worktrees/task0-baseline-20261025` | 180.7 | Exact HEAD ancestor | `detached tip in recovery/release ancestry`; `101-task0-baseline-20261025.json` | REMOVED; path + registry absent |
| 102 | `.claude/worktrees/task0-candidate-qualification-20261025` | 177.3 | Git cherry: -1 / +12 | `detached tip in recovery/release ancestry`; `102-task0-candidate-qualification-20261025.json` | REMOVED; path + registry absent |
| 103 | `.claude/worktrees/task0-test-hangs-20261025` | 176.1 | Git cherry: -0 / +2 | `codex/0.8.10-task0-test-hangs-20261025`; `103-task0-test-hangs-20261025.json` | REMOVED; path + registry absent |
| 104 | `.claude/worktrees/task0-baseline-green-20261025` | 173.9 | Git cherry: -1 / +13 | `codex/0.8.10-producer-sdk-receiving-20261026`; `104-task0-baseline-green-20261025.json` | REMOVED; path + registry absent |
| 105 | `.claude/worktrees/u35b-finish-20261026` | 170.5 | Git cherry: -0 / +2 | `codex/0.8.10-u35b-finish-20261026`; `105-u35b-finish-20261026.json` | REMOVED; path + registry absent |
| 106 | `.claude/worktrees/receiving-controls-20261026` | 170.3 | Exact HEAD ancestor | `codex/0.8.10-receiving-controls-20261026`; `106-receiving-controls-20261026.json` | REMOVED; path + registry absent |
| 107 | `.claude/worktrees/task0-baseline-admission-20261026` | 169.5 | Git cherry: -1 / +15 | `codex/0.8.10-sg-binding-facts-20261026`; `107-task0-baseline-admission-20261026.json` | REMOVED; path + registry absent |
| 108 | `.claude/worktrees/protocol-finish-20261026` | 168.6 | Git cherry: -0 / +1 | `codex/0.8.10-protocol-receiving-20261026`; `108-protocol-finish-20261026.json` | REMOVED; path + registry absent |
| 109 | `.claude/worktrees/u30-finish-20261026` | 168.5 | Git cherry: -0 / +1 | `codex/0.8.10-u30-finish-20261026`; `109-u30-finish-20261026.json` | REMOVED; path + registry absent |
| 110 | `.claude/worktrees/t5e-finish-20261026` | 167.4 | Git cherry: -0 / +4 | `codex/0.8.10-task0-kernel-capacity-20261026`; `110-t5e-finish-20261026.json` | REMOVED; path + registry absent |
| 111 | `.claude/worktrees/approved-integration-strategy-20261026` | 157.8 | Git cherry: -0 / +1 | `codex/0.8.10-approved-integration-strategy-20261026`; `111-approved-integration-strategy-20261026.json` | REMOVED; path + registry absent |
| 112 | `.claude/worktrees/controller-handover-20261026` | 155.5 | Git cherry: -0 / +6 | `codex/0.8.10-controller-handover-20261026`; `112-controller-handover-20261026.json` | REMOVED; path + registry absent |
| 113 | `~/.codex/worktrees/c81f/elspeth` | 152.4 | Git cherry: -0 / +30 | `codex/0.8.10-u38c-admit-once-20261027`; `113-elspeth.json` | REMOVED; path + registry absent |
| 114 | `~/.codex/worktrees/t5f-schema-receiving-20261027/elspeth` | 150.6 | Git cherry: -0 / +22 | `codex/0.8.10-t5f-schema-receiving-20261027`; `114-elspeth.json` | REMOVED; path + registry absent |
| 115 | `~/.codex/worktrees/u30-receiving-20261027/elspeth` | 149.4 | Git cherry: -0 / +23 | `codex/0.8.10-u30-receiving-20261027`; `115-elspeth.json` | REMOVED; path + registry absent |
| 116 | `.claude/worktrees/ci-marker-repair-20261026` | 145.5 | Git cherry: -0 / +1 | `codex/0.8.10-foundation-receiving-20261027`; `116-ci-marker-repair-20261026.json` | REMOVED; path + registry absent |
| 117 | `~/.codex/worktrees/u45-stage-memo-20261027/elspeth` | 144.9 | Git cherry: -0 / +39 | `codex/0.8.10-u45-stage-memo-20261027`; `117-elspeth.json` | REMOVED; path + registry absent |
| 118 | `~/.codex/worktrees/u35b-protocol-batch-20261027/elspeth` | 143.8 | Git cherry: -0 / +23 | `codex/0.8.10-real-stop-20261027`; `118-elspeth.json` | REMOVED; path + registry absent |
| 119 | `.claude/worktrees/branch-safety-finish-20261026` | 143.3 | Git cherry: -0 / +24 | `codex/0.8.10-u38c-first-slice-20261027`; `119-branch-safety-finish-20261026.json` | REMOVED; path + registry absent |
| 120 | `~/.codex/worktrees/u37-drain-cut-preparation-20261027/elspeth` | 89.2 | Git cherry: -0 / +43 | `codex/0.8.10-u37-worker-receiving-20261028`; `120-elspeth.json` | REMOVED; path + registry absent |
| 121 | `.claude/worktrees/agent-a4d9e445eba0ea8c2` | 15.2 | Excluded; no new history claim | Original `refs/heads/v1-removal-editor-b` and directory retained | HELD: Age not established above 24 hours in current bounded pass |
| 122 | `.claude/worktrees/agent-a01d52168cd5a5cee` | 15.2 | Excluded; no new history claim | Original `refs/heads/worktree-agent-a01d52168cd5a5cee` and directory retained | HELD: Age not established above 24 hours in current bounded pass |
| 123 | `.claude/worktrees/agent-a1dd8f52572ae8dd1` | 14.2 | Excluded; no new history claim | Original `refs/heads/u40-c6-continuation-controls` and directory retained | HELD: Age not established above 24 hours in current bounded pass |
| 124 | `.claude/worktrees/lock-hold-bisect` | 14.0 | Excluded; no new history claim | Original `detached HEAD` and directory retained | HELD: Age not established above 24 hours in current bounded pass |
| 125 | `.claude/worktrees/lock-hold-bisect2` | 13.9 | Excluded; no new history claim | Original `detached HEAD` and directory retained | HELD: Age not established above 24 hours in current bounded pass |
| 126 | `.claude/worktrees/codex-0.8.10-g6-observer-integrity` | 7.7 | Excluded; no new history claim | Original `detached HEAD` and directory retained | HELD: Age not established above 24 hours in current bounded pass |
| 127 | `.claude/worktrees/codex-0.8.10-g5-preview-cancellation` | 6.6 | Excluded; no new history claim | Original `detached HEAD` and directory retained | HELD: Age not established above 24 hours in current bounded pass |
| 128 | `.claude/worktrees/codex-0.8.10-primary-budget-joins` | 6.3 | Excluded; no new history claim | Original `detached HEAD` and directory retained | HELD: Age not established above 24 hours in current bounded pass |
| 129 | `.claude/worktrees/codex-0.8.10-baseline-56b` | 2.0 | Excluded; no new history claim | Original `detached HEAD` and directory retained | HELD: Age not established above 24 hours in current bounded pass |
| 130 | `.` | — | Excluded; no new history claim | Original `refs/heads/release/0.8.10-assembly` and directory retained | HELD: Primary active release checkout |


The final post-state check reported no discrepancies. New worktree registrations
after the final snapshot would require fresh custody and age admission; this
report is not standing authority to remove an active context.
