---
title: Pre-merge review findings for release/0.8.1, 2026-09-10 snapshot
labels: [area/tests, type/epic, needs-triage]
---

Consolidated findings from a maximum-effort code review of `release/0.8.1` plus an independent suite measurement on 2026-09-10. This is a dated snapshot of a branch under review, not a single defect.

## Branch state at the time of review

HEAD was `25d1886c4` ("bump: update version to 0.8.1 in pyproject.toml"). `git diff @{upstream}...HEAD` was empty — the branch was level with its remote — so the review target was the working-tree diff of 148 files, out of 151 dirty files comprising a documentation citation sweep and 0.8.1 release-note and version work.

## Suite state, measured

The full-suite log recorded `SUITE exit=1`: 26 failed, 50507 passed, 48 skipped, 2 xfailed in 982 s. The tree hash taken before the run was identical to the one taken after it, so the run is evidence rather than a measurement over a moving tree.

One observation worth carrying forward on its own: the wrapper that launched the suite reported exit code 0. That was the wrapper's exit code, not pytest's. A run labelled "final clean full-suite run" was not clean, and nothing in the notification said otherwise. An independent reviewer's own full run read 27 failures; its extra red was `test_two_process_claim_hammer_with_dashboard_reads`, which passes under `-n 0` and is a known parallelism flake.

## Three independent clusters

1. **Version-surface repair left incomplete**, producing 9 acceptance failures. Not resolved within the review — it turns on which version surface is authoritative.
2. **17 failures on the DAG scenario production path.** These also reproduce on the tip of `release/0.8.0`, so the cause is wider than this branch.
3. **15 review findings**, including release notes describing the wrong release, three gates that cannot go red, two live-code edits inside a documentation-only sweep, and dangling documentation links.

## Static-analysis impact, measured

Signed-allowlist fingerprint drift across the tree was zero, with the measuring instrument positive-controlled 8 out of 8. Every changed Python file was line-count-neutral, so the AST-path bindings in that allowlist were stable. The diff did not make that state worse.

## Note

The per-finding evidence lives in child tickets that are not part of this set, so this issue records the measurement and the three clusters only. No code was changed and nothing was committed as part of the review. All figures are as at 2026-09-10 and should be re-measured before they are relied on.
