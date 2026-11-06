# Worktree cleanup list

This is the maintained entry point for the worktree cleanup before 0.8.11.
The maintainer's sequence is: retain the 0.8.9 audit, repeat it after 0.8.10
merges, then make a concerted cleanup before starting 0.8.11. No deletion is
scheduled or being performed by this document.

## Recorded baseline

The [21 October 2026 inventory](../worktree-deletion-candidates-2026-10-21.md)
contains the full candidate paths, HEADs, ages, inclusion evidence and removal
holds against the 0.8.9 RC at
`dbbb6582492ce1fb22941e0cc395aa20d4eb386d`.

That recorded audit found 22 candidates among 47 linked worktrees older than
12 hours: 20 ancestors and two complete patch/blob carries. It left 25 older
trees unproven and excluded 11 younger trees. Only
`.claude/worktrees/advisor-heuristic-baseline-20261020` was removable under the
canonical helper at the audit time; the other 21 candidates had holds.
These are baseline findings, not current deletion clearance.

| Checkpoint | State | Next action |
|---|---|---|
| 0.8.9 candidate inventory | Recorded | Keep the dated evidence and all removal holds. |
| After 0.8.10 merges | Next refresh | Verify the actual merge and target SHA, then repeat the inventory below. |
| Before starting 0.8.11 | Planned cleanup | Resolve holds, remove the verified disposable cohort, and record what was removed or retained. |

## Refresh after the 0.8.10 merge

1. Verify the actual 0.8.10 merge from GitHub and Git. Record the PR, merge
   commit and exact resulting target SHA; do not substitute an old RC or
   infer inclusion from a release branch name.
2. Re-enumerate every registered worktree, including additions since this
   baseline. Recalculate the 12-hour cutoff using registration birth times;
   age measures creation, not inactivity.
3. Recheck ancestry, whole-commit patch identity, complete squash/larger-commit
   inclusion and merge payloads against that target. Keep anything whose
   complete inclusion remains unproven. Matching titles are not evidence.
4. Reinspect tracked, staged, untracked and ignored material, accessible
   process working directories and nested registered worktrees. Existing
   dirty state and young child worktrees remain holds until reconciled.
5. Run `scripts/worktree-cleanup.sh --base <verified-target-sha>` in its default
   dry-run mode. Reconcile its ancestry-only classification with any explicit
   squash/cherry-pick proofs.
6. Save a new dated inventory beside the baseline, then update this entry
   point with its link, target SHA and measured totals. Account for every
   previous candidate as retained, still held, newly eligible, already absent
   or removed; also record newly discovered candidates. Do not overwrite the
   historical audit to make old findings look current.

## Cleanup before 0.8.11

Use the refreshed list to work through the whole cohort together. Preserve
needed uncommitted work and review material in retained project locations,
resolve process ownership and nested-tree holds, and obtain the operator's
go-ahead for the concrete destructive cleanup cohort. The current instruction
sets the sequence; it does not authorize discarding unresolved material now.

Immediately before each removal, recheck the target, worktree HEAD, status and
process use. Use the canonical helper with explicit target/path filters for
eligible ancestry cases. Handle proved squash/cherry-pick cases explicitly;
do not force through an UNMERGED result without reconciling all local state.
Treat branch deletion and ignored-content disposal as explicit parts of the
chosen cohort, and leave unrelated and remote refs alone.

After cleanup, verify the Git registry, filesystem paths and branch refs. Record
actual removals and every retained hold here, with a link to the final dated
inventory, before starting 0.8.11. Recheck live state on resumption; this file
is the checklist, not a background monitor.
