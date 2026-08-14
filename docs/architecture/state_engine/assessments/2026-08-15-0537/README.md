# State Engine Assessment — 2026-08-15 05:37 AEST

First full assessment on the `elspeth-state-engine-v3` catalog
(catalog schema 2, assessment schema 3).

## Baseline and verdict

- Assessment ID: 2026-08-15-0537
- Full commit: `2b4b04a8a852a839b7b395b0bcdfceb95676606b`
  (tree `a12309c2c95c363e5fabaa8c6334027507dd3012`), branch
  `codex/state-engine-v3-assessment`
- Mode: full
- Verdict: **not_complete**

All three local SQLite WAL lanes executed green at the frozen commit —
712, 30, and 3 collected nodes, every node passed, zero skips — and 489
of the catalog's 7,010 required cells are promoted from that retained,
reporter-bound evidence (417 pass; 72 partial, where independent review
judged the evidence real but short of the catalog's per-family
acceptance text). Every one of the 73 legs still retains at least
one unknown mandatory cell, so `HG-09-mandatory-leg-unresolved` is open
and the overall verdict derives `not_complete`.

The two structural gap owners:

- `elspeth-82592e3aa1` — the PostgreSQL 16 AWS composition lane and all
  38 protected live provider lanes are unexecuted (AWS environment torn
  down 2026-08-10; the manual workflow is not activated on the default
  branch). 1,780 cells.
- `elspeth-efb47cb5fd` — local cells whose dimensions have no mapped
  test assertion in the retained runs (including the 38 provider-backed
  PB-09 cases with no local internal-composition evidence). 4,741 cells.

## Package

- `assessment.json` — machine-readable schema-3 evidence and result.
- `evidence.md` — exact commands, environment identity, mapping
  provenance, adjudications, and limitations.
- `review.md` — findings, dispositions, and re-review record.
- `evidence/` — retained JUnit XML, profile reports, stdout/stderr, and
  collected-node indexes for the three lane runs.

## Reproduce

Follow `docs/architecture/state_engine/assessment-program.md` from a
package-bearing checkout and execute against the recorded baseline
worktree. Every evidence record binds the exact argv, the checkout-local
Python executable, and the retained artifact digests.

Before replaying any recorded argv, read the **Reproduction
preconditions** section of this package's `evidence.md` — it is
normative. In particular: rewrite the `--junitxml` and
`--state-engine-profile-report` arguments to throwaway paths (the
recorded paths are the retained evidence), rerun only on a quiet host
(one deployment probe is contention-sensitive, with its failure
signature documented there), and compare against the retained
`profile.json` by node identities and outcomes rather than expecting
byte-identical JUnit.
