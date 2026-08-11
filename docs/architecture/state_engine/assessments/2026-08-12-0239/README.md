# State Engine Assessment — 2026-08-12 02:39 AEST

This full v2 assessment pins the durable state-engine contract at
`af79b34040f5ce5fd989aa0d42a1b80ad8366829` (tree
`c4abd284a970783be8a19cb70dccc3e59072ec87`). Evidence ran in a clean detached
worktree at that exact commit with a worktree-local Python 3.13.1 environment.
The assessment documents were the only overlay in the package-bearing branch.

## Verdict

**Not complete.** All 73 catalog legs retain unknown mandatory cells and
`HG-09-mandatory-leg-unresolved` is open. No v1 pass was inherited. Four
focused SQLite vectors pass 128 checks and selected PostgreSQL 16 lease and
coordination paths pass four testcontainer checks, but those runs predate
case/profile reporter instrumentation in the selected tests and therefore do
not promote v2 proof cells.

PostgreSQL 16 remains required and first-class for the maintained AWS
single-leader Landscape profile. The absence of a complete PB-11 matrix is a
completion gap, not a reason to make that profile optional. Multi-replica
scheduling remains unsupported.

## Package

- [assessment.json](assessment.json) — machine-readable 73-leg result,
  environment identity, live ownership, hard gates, and limitations.
- [evidence.md](evidence.md) — exact focused vectors, results, structural and
  tracker captures, and negative claims.
- `artifacts/observation-index.json` plus `EV-OBS-*` JUnit/stdout/stderr/exit
  files — exact non-promoting focused observations and artifact hashes; raw
  outputs containing captured trailing whitespace use deterministic gzip.
- `artifacts/loomweave-analysis.json` and `artifacts/warpline-*.json` — the
  exact-commit structural run and explicitly partial temporal result.
- `artifacts/filigree-*.json` — complete `has_more=false` state-engine, ready,
  and blocked snapshots; `artifacts/filigree-show-records.ndjson` retains exact
  `show --json` records for the hierarchy, linked dependencies, and Python 3.14
  blocker.
- [review.md](review.md) — architecture, evidence, and reproducibility review.
- [Current proof matrix](../../proof-matrix.md) — readable family and owner
  view.

## Live work tree

Filigree milestone `elspeth-4b3d734e3a` owns the v2 program. Its five
implementation/proof cohort steps are `elspeth-d262ace360`,
`elspeth-eefd990b46`, `elspeth-cc0b256aca`, `elspeth-f227dd8d2f`, and
`elspeth-67be892457`; final assessment/gates are `elspeth-f89d82e925`. Six
pre-existing open state-engine issues remain linked as dependencies rather
than duplicated. The Python 3.14 Runtime-VAL defect discovered by the clean
environment run is tracked unclaimed as `elspeth-61350c4744`.

## Reproduce

Follow the parent [assessment program](../../assessment-program.md). Create a
clean detached worktree at the full baseline commit, use
`uv sync --python 3.13 --frozen --all-extras`, verify that `elspeth.__file__`
resolves inside that worktree, then execute the vectors in [evidence.md](evidence.md).
Running plain `uv sync --frozen --all-extras` on the capture host selected
Python 3.14.3 and exposed the separately recorded Runtime-VAL compatibility
failure; that failed observation must not be erased by the 3.13 rerun.
