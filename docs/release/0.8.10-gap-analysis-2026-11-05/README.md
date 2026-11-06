# 0.8.10 release gap analysis — implementation handoff

Updated **2026-11-06**, following the original 2026-11-05 source audit. This
refresh revisits only the seven previously open gaps and eight open questions.
Previously completed units retain their earlier assessment; none was reopened.
The folder name is retained so existing implementation-agent links keep working.
This is working documentation, not release approval.

## Start here

Give the agent this folder and the prompt in
[IMPLEMENTATION-BRIEF.md](IMPLEMENTATION-BRIEF.md). The package contains:

| File | Purpose |
|---|---|
| [CONFIRMED-GAPS.md](CONFIRMED-GAPS.md) | Current dispositions of the seven findings, evidence and remaining acceptance |
| [OPEN-QUESTIONS.md](OPEN-QUESTIONS.md) | Scope discrepancies and acceptance work not established by the audit |
| [UNIT-COVERAGE.md](UNIT-COVERAGE.md) | All 51 release units, qualification reviewer and closed prior findings |
| [VALIDATION.md](VALIDATION.md) | Reproduction, focused checks, negative controls and final qualification boundaries |

## Snapshot and evidence boundary

- Repository branch: `release/0.8.10-assembly`.
- Original full audit: `a6990ffb72bd8b49e8298fbb4b152d586ce683a5`.
- Refreshed source: **`ae88147c6130051b1e3d497a1341a69bd56924e5`**.
  The focused review began at `c9f568e63b8df0cdac3ea07763c41192d194fa17`;
  the subsequent single-commit delta was checked for the affected open items.
- Local `main` and cached `origin/main`: `3b28d537be98234da8135c418bf185a998c18b68`.
- The shared checkout contained ongoing work. Only pinned committed source
  receives implementation credit. Later commits and dirty edits require a
  receiving check; this document does not follow a moving checkout implicitly.
- The repeat audit assigned a distinct subagent to each of 51 declared units,
  plus a separate qualification reviewer, under four coordinating agents.
- This refresh used six focused reviewers plus parent reconciliation. Evidence
  came from Git objects, production callers, test bodies and bounded raw run
  artifacts. No new application tests or project imports ran. The parent also
  queried GitHub issue327 and exact-commit CI read-only. Existing test results
  are identified by receipt and source match; test presence is not a pass.
- Source line numbers refer to the commit explicitly identified in each item;
  retained historical pointers are labelled. Relative
  links open the receiving checkout; use `git show <audited-commit>:<path>` to
  inspect the original evidence if HEAD or the worktree has moved.
- The source audit used current code to test historical claims. Older report
  statuses are not evidence that a unit is implemented or absent.

Historical overall branch size at the original audit, including production,
tests, proofs and docs (not remeasured as a current completion metric):

```text
git diff --shortstat main...a6990ffb72bd8b49e8298fbb4b152d586ce683a5
1788 files changed, 527851 insertions(+), 79124 deletions(-)
```

## Assessment

Most substantive implementation is present, including live model-requested and
selected automatic successor execution, cold reconstruction, paid-call custody,
recovery, signal-driven process handoff, provider preparation, and PostgreSQL
contention tests. The audit does not justify a numeric completion percentage:
the units differ greatly in size and include implementation, proof and release
obligations.

## What changed in this refresh

| Item | Current disposition |
|---|---|
| G01 | **Narrowed.** Later-Compose quantum/watermark successors now work through the actual driver and have real elapsed-clock test source. First-Compose/non-Compose cuts still use same-sequence recovery. |
| G02 | **Open: 2 of 13 guards stale**, down from 7. At intermediate c9f only authority was stale; ae881 also changed session service. |
| G03/G04/G05 | **Open, unchanged:** deleted API test callers, epoch 85 PostgreSQL assertions against 88, obsolete legacy-publication test/reference. |
| G06/G07 | **Open:** release/schema/policy wording and continuation-result teaching/discovery still contradict current source. |
| O02 | **Narrowed acceptance question:** new joined Read/Review/iteration/Stop handover tests strengthen coverage; the prescribed combined cross-sequence scenario remains unestablished. |
| O03 | **Open with measured failure evidence:** R220's production and test sources match ae881 and its actual read/end-retry workflow fails at preparation timeout. Full production-bound performance acceptance remains outstanding. |
| O05 | **Narrowed:** migrated TITLE PostgreSQL tests cover current atomic settlement, rollback, duplicate writers, cold reclaim and postcommit handoff. Only remaining exceptional-case/matrix mapping is open. |
| O04 | **Open:** current donor difference is 1,629 paths; no newly unexplained missing production module found. |
| O01/O06/O07/O08 | Requirement/document reconciliation, final qualification and issue mapping remain open; see refreshed evidence. |

No whole gap is claimed closed. Completed portions are removed from the active
work instructions. The more precise runtime and proof findings should prevent
duplicate implementation of work already received.

## Current rulings to preserve

- Continuation is the only Composer lifecycle. The 2026-11-01 ruling supersedes
  historical V1/V2 rollout, default-off admission and version-header requirements.
  Persisted policy payload tags are a different concern: a suffix alone does
  not prove that a helper or codec is obsolete.
- At the audited commit, Sessions epoch is **88**, Landscape epoch is **50**.
  Read live constants again before correcting current documents. Historical
  epoch80/82/84/85 statements do not override the code.
- **6 November ruling: no backwards compatibility before 1.0.0.** Remove obsolete
  implementations/adapters/fallbacks/upgrade machinery as current callers are
  consolidated; do not preserve them solely for old formats or policies. Current
  dependencies must still work. State the supported contract and actual breaks,
  preserve records/evidence/backups and explicitly refuse unsupported formats.
  This authorizes no migration, store reset, deletion or downgrade.
- The provider authors pipeline structure. Tutorial and ordinary sessions use
  the same backend; no server-authored or tutorial-special workaround.
- Signing is suspended. No signing, rekeying, fingerprint-baseline regeneration
  or operator key access. Track trust-tier changes with the keyless set-diff.
- The separate gateway worker architecture was deliberately dropped. Do not
  resurrect it as a missing security fix; adapters are operator-trusted code.

The governing scope is the [master plan](../0.8.10-master-plan.md), interpreted
with its later rulings and current source, not its historical status columns.

## Suggested execution order

1. Rebind branch, commit, dirty state, applicable instructions and test capacity.
   Reproduce each assigned finding on the receiving commit before editing.
2. Repair G03/G04/G05/G07 in bounded lanes. Diagnose O03's measured preparation
   failure and inspect newer receiving evidence before declaring that workflow
   repaired. Investigate O01/O02 alongside the remaining G01 cuts.
3. Complete only G01's remaining required cuts or obtain a precise scope
   disposition; preserve the now-received cooperative successor implementation.
   Finish runtime changes before deriving G02's final source binds.
4. Reconcile G06 against final schema/policy values. Re-derive and review G02
   after all changes to its guarded sources, with sensitivity controls.
5. Complete the selected acceptance/reconciliation work in OPEN-QUESTIONS and
   qualify the final frozen source using VALIDATION. Do not infer qualification
   from this document or focused test results.

Shared owners such as `continuation_driver.py`, session service/repository,
policy and the fencing proof need one integration owner. Parallel review is
useful; overlapping edits and concurrent broad suites are not.
