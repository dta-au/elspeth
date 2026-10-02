---
title: Planner byte-budget ledger is unchecked prose and has drifted 96 bytes from the tree
labels: [area/tests, area/composer, type/task, priority/P3]
---

The record of how large the Composer planner's fixed scaffolding is, and why, lives entirely in
a hand-maintained comment. Nothing derives it and nothing checks it. It and the tree it
describes currently disagree by a measured 96 bytes, and no test reported that, because the only
assertion over the figure is a ceiling with several kilobytes of slack.

## Background

The Composer is ELSPETH's web authoring surface, where a language model plans a pipeline through
a tool loop. Every planner turn re-sends a fixed scaffold — the system message, the fixed
payload and the tool palette — uncached, so the scaffold's size is a standing per-turn cost.
That cost is recorded as a chain of measured byte figures and their deltas, each attributed to
the change that moved it.

## Where to start

`tests/unit/web/composer/test_pipeline_planner.py`. The ledger is the comment block at
`:140-192`, its tail figure is the `110,706 B` at `:186`, the ceiling constant is at `:193`, and
the only assertion over any of it is at `:1944`.

## The record and the tree disagree

Measured using the method the comment itself prescribes — lower the ceiling to 1 so the
assertion prints its own left-hand operand, applied at collection time rather than by editing
the file:

```
assert 110802 <= 1      # [live] parametrisation
assert 109107 <= 1      # [small] parametrisation
```

The ledger's last entry says 110,706 B. The `[live]` scaffold measures 110,802 B — 96 bytes
higher, with no entry in the chain accounting for the difference. Real headroom is 8,596 B, not
the roughly 8.7 KiB the chain implies.

Two things this is not. **The scaffolding is not oversized**: 110,802 B is well inside the
119,398 B ceiling, and nothing about the shipped request is wrong. **This is not a production
defect** of any kind; see Impact. What is wrong is the record.

The drift is also now expensive to attribute. The tail figure was written on 2026-09-03, and 165
commits have touched the Composer source since, so recovering which of them moved the scaffold
means re-measuring rather than reading.

## Why nothing caught it

The chain's internal arithmetic does close — `109,924 +59 → 109,983 +60 → 110,043 +285 →
110,328 +236 → 110,564 +189 → 110,753 −47 → 110,706`, each stated delta equalling the
difference of its neighbours — so the chain is self-consistent while being wrong about the
tree. The only mechanical check is `_FIXED_SCAFFOLDING_MAX_CANONICAL_BYTES`, which is
`106 KiB × 1.10 = 119,398 B`, asserted with `<=` at `:1944`. Anywhere under that ceiling, a
wrong figure, a skipped step or a stale tail is invisible.

This is the second occurrence, not the first. The chain has been repaired once before: an entry
recorded +285 B against a predecessor two steps above it, and closing the gap meant re-measuring
every intervening commit, because a reader adding the chain up landed 119 B low. The rule the
comment then adopted — every figure must be a measured predecessor of the next — has no
enforcement behind it, which is why it has now slipped a second time.

The ledger also does not record which parametrisation its figures describe, so a reader cannot
tell whether a given number should be compared against `[live]` or `[small]`.

## Impact

Nothing that ships is affected. This is a test-side cost tripwire, not a correctness limit — the
production fail-closed cap is `composer_planner_max_request_bytes` at 2 MiB, and it is
untouched. The cost is record integrity: the chain is the project's only account of why the
scaffold is the size it is, and anyone auditing a future scaffolding change against it is
working from a baseline that is already 96 B wrong, with no way to notice.

## Fix

Move the chain out of the comment and into a data structure the test reads — a sequence of
`(commit, bytes, note)` entries — and assert over it that each delta equals the difference of
its neighbours and that the tail is under the ceiling. The prose stays as the note field; the
arithmetic becomes derived rather than maintained by hand.

Two constraints on any implementation:

- **Do not reintroduce a strict ratchet.** Pinning the measured size to the last recorded
  figure, by equality or a tight tolerance, is the arrangement that was deliberately replaced on
  2026-09-03 by the 10% band, so that scaffolding edits stop costing a measure-and-resubmit
  round trip. The ceiling assertion should be left exactly as it is.
- **The migration must transcribe the measured tail, not the written one** — 110,802 B for the
  `[live]` parametrisation — and record which parametrisation each figure describes.
  Transcribing eight figures by hand is the same failure mode this issue is about, so the
  transcription is the part to review carefully.

You would know it holds when deleting a delta, or changing one figure without changing its
neighbours, makes the test fail, while a legitimate scaffolding change with a correctly recorded
entry passes.

**Size.** Small to moderate. The code is a short helper and one assertion; the care goes into
the transcription and into re-measuring the current tail before writing it down.
