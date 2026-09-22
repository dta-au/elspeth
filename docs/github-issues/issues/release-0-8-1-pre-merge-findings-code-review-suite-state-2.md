---
title: Pre-merge review findings for release/0.8.1, 2026-09-10 snapshot
labels: [area/tests, type/epic, needs-triage]
---

A dated record of what a code review and an independent test-suite run found on the `release/0.8.1` branch on 2026-09-10. This is a snapshot of a branch under review rather than a single defect, and every number in it needs re-measuring before it is relied on.

## Branch state at the time

The branch head was commit `25d1886c4` ("bump: update version to 0.8.1 in pyproject.toml"), level with its remote — `git diff @{upstream}...HEAD` was empty. The review target was therefore the uncommitted working tree: 148 changed files out of 151 dirty ones, comprising a documentation citation sweep plus release-note and version work for 0.8.1.

## Suite state, measured

The full run recorded exit status 1: 26 failed, 50507 passed, 48 skipped, 2 expected failures, in 982 seconds. A hash of the working tree taken before the run matched the one taken after, so the run measured a tree that was not moving underneath it.

One observation is worth carrying on its own merits, independently of this branch. The wrapper that launched the suite reported exit code 0. That was the wrapper's own exit code, not the test runner's. A run labelled a final clean full-suite run was not clean, and nothing in the notification said otherwise. Any tooling that reports a suite result by way of a wrapper needs to propagate the inner exit code or it will certify a red run as green again.

A second reviewer's independent full run read 27 failures. The extra one was `test_two_process_claim_hammer_with_dashboard_reads`, which passes when the suite is run serially and is a known parallelism flake.

## Three independent clusters

1. **An incomplete version-surface repair**, producing 9 acceptance-test failures. Unresolved within the review, because it turns on which version surface is authoritative — a decision, not a code change.
2. **17 failures on the DAG scenario production path.** These also reproduce on the tip of `release/0.8.0`, so whatever causes them is older and wider than this branch. Anyone picking this up should start by confirming that still holds.
3. **15 review findings**: release notes describing the wrong release, three checks that cannot fail by construction, two live-code edits inside a sweep that was meant to touch documentation only, and dangling documentation links.

## Static-analysis impact, measured

The signed-allowlist fingerprints used by the static-analysis gate showed zero drift across the tree, with the measuring instrument positive-controlled 8 times out of 8. Every changed Python file was neutral in line count, so the syntax-tree path bindings in that allowlist were stable. The diff did not make that state worse.

## What to do

In priority order, and expecting each to become its own issue:

1. Re-run the suite on the current branch head and produce a fresh failing set. None of the numbers above are current.
2. Take the version-surface decision in cluster 1. Nobody can start that work until someone rules on which surface is authoritative.
3. Confirm cluster 2 still reproduces on both branches. If it does, it belongs to the DAG scenario code, not to the release, and should be re-filed there.
4. Fix the wrapper exit-code propagation described above. That one is small, independent of the release, and stops a whole class of false green.

## Note

The per-finding evidence lived in child tickets that are not part of this set, so this issue records only the measurement and the three clusters — a new reader will need to re-derive the detail of clusters 1 and 3 from a fresh run. No code was changed and nothing was committed as part of the review.
