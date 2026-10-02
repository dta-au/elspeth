---
title: Partial failed-aggregation terminalisation wedges every resume on membership mismatch
labels: [area/engine, type/bug]
---

A crash part-way through recording the outcomes of a failed aggregation leaves two records
of that batch's membership permanently disagreeing. Every subsequent resume raises the
same `AuditIntegrityError`, so the run cannot be recovered at all.

This is engine work, in `src/elspeth/engine/`. Two concepts are needed to read what
follows. An aggregation collects several rows (its *members*) and processes them as one
batch; while they wait, they are held at a barrier. The *journal* is the engine's
crash-recovery record of which members are still held, and on resume
`src/elspeth/engine/journal_restore.py` validates those journal rows against the batch
membership persisted in the audit trail before letting the run continue.

## Why

Members of a failed aggregation receive their terminal outcomes in separate commits. If a
crash lands after member A commits but before member B:

1. Restore reconciliation releases the already-terminal member A. That path is
   `src/elspeth/engine/barrier_coordination.py:1925-1960`, scoped to
   `(FAILURE, UNROUTED)`: `find_failed_unrouted_terminal_token_ids` selects the terminal
   ids, and `mark_blocked_barrier_terminal` releases them with
   `reason="failed_flush_crash_reconcile"`.
2. The retry of the incomplete batch still copies the *original* persisted membership
   `{A, B}` rather than what survived that release. This was traced through the
   aggregation flush handling in `src/elspeth/engine/processor.py`; the specific lines
   cited in the original report have since moved, so no line reference is given here.
3. Restoration therefore holds a residual journal set of `{B}` against a `member_order` of
   `{A, B}`. `_reconcile_journal_batch_members`
   (`src/elspeth/engine/journal_restore.py:970`) compares the two as sets and raises
   `AuditIntegrityError` on the difference.

Every later resume repeats the same comparison and the same failure. The mechanism was
traced at `3dc67fb1d`; the citations above are against the current tree.

## Impact

The run is permanently unresumable — not degraded, not partially recoverable. It requires
a failed aggregation with more than one member and a crash inside the window where their
outcomes are being written, so it is narrow; within that window the outcome is total.

Independently reproduced with a two-member failed aggregation and a crash injected after
the first member's terminal outcome: the first resume releases A, then rejects journal
`{B}` against retry membership `{A, B}`.

## Fix

Correct behaviour is that a resume after a partial terminalisation either completes or
fails for a real reason — never because the engine compared a post-release journal against
a pre-release membership. Two candidate approaches, and choosing between them is the first
task:

- Make the failed-batch terminalisation and the scheduler release atomic, so a crash
  cannot land between them.
- Derive the retry membership from what the journal still holds, rather than copying the
  original persisted membership, so the two records cannot disagree by construction.

Either way, fail-closed corruption detection must be preserved: `_reconcile_journal_batch_members`
must still raise when the journal and the audit trail genuinely disagree. The goal is to
stop manufacturing a disagreement that is an artefact of a partial commit, not to relax
the check.

You would know it holds by re-running the reproduction above — a two-member failed
aggregation with a crash injected after the first member's terminal outcome — and seeing
the resume proceed; and by confirming that a deliberately corrupted membership still
raises. Both cases belong in the test suite, since only the pair distinguishes a fix from
a weakened check.

Size and readiness: not a one-line change, and it should not be started without settling
the approach above — the atomicity route touches commit boundaries in the barrier
coordination path, while the derivation route changes what a retry considers its
membership. Both are in code where a wrong answer is an audit-integrity failure.

This is distinct from two previously recorded issues in the same area: one covering the
window between writing all-terminal outcomes and releasing them, and one covering a
side-effect exception interrupting the loop.
