# Refactors rebased onto published streaming

The user requested that the oversized-file work land on top of the streaming HTTP branch. The root fetched and verified `origin/work/composer-https-streaming-083-20261007`, the published PR281 branch, at `9f26833c8f6a7c56abd2c5b35c80484ebe0c82bc`. Exactly the three local refactor commits were replayed onto that tip with shared rerere disabled. The rebase completed without conflicts; no streaming worktree was edited.

| Slice | Before rebase | After rebase |
|---|---|---|
| Processor transform tests and inventory | `794531426a48ed6666bb68a0b690126fe58690d0` | `1a1a3110d` |
| Executor responsibility suites | `fb6c7185f90c8e94b0b03452d453e8cc8c0da3e2` | `bd9859514` |
| DAG responsibility suites | `3f7de818ec2e425e21976cd20cc1e5699292c4da` | `df7deda0e31df7e5a50a85b9e16363893f41eb44` |

The old tip remains on local branch `backup/oversized-files-pre-streaming-20261009`. This operation rebased the owned patch only; it did not replay unrelated main changes or merge another branch.

## Base limitation

This is the current **published** streaming base, not the owner's entire work in progress. It excludes local integration commit `703cdd1674d01d422baa4d54f210105edccf498c`, its subsequent uncommitted repairs, and the main-only PR282 tutorial and PR283 chat changes. Neither main `9e481bc85a32ee15f808c5e2b16cab7634e57b8e` nor those merge commits is an ancestor of the rebased branch. Another rebase will be needed when the owner commits/publishes the intended integration base. No pending owner bytes were copied, committed or represented as incorporated.

The canonical 538-path reservation remains in force, SHA-256 `3149d9cf6e2b97e92a3e5002da4d0dc7a3c7430df0c5535eae7f8ad8366afc38`. Rebase is not an ownership release. All 27 owned paths are outside it and outside the base's changed-path set relative to the former main base.

## Patch preservation and validation

Raw measured results:

```text
rebase exit=0; conflicts=0
range-diff: all 3 commits '='
old/new owned path sets: equal, 27 paths
old/new owned modes and blobs: identical
focused tests: 627 passed in 28.64s; exit=0
affected gates: 2 failed, 24 passed in 117.81s; exit=1
canonical Python inputs: 3981 files; before/after dictionaries including HEAD identical
source inventory SHA-256: 0490b715e13ea0196b43b0b13b2168fda20a1d96dd7d49497939eb9ea3b34618
```

The 627 tests comprise the two processor suites, five executor/refusal suites and three DAG suites. They ran under the streaming base's added required-executor, watchdog and telemetry autouse fixtures. Their earlier main-based passes were not reused as evidence for this new environment.

The 26 affected checks comprise three mock-discipline checks, eleven canonical file-walker checks, five masquerade live/refusal/shape controls, one test-hasattr check and six template call-site checks. The failures are the live mock census and live masquerade baseline comparison. Their implicated files are byte-identical to the published base:

- `tests/unit/web/execution/test_recovery_coordinator.py`: three direct unspecced mock calls.
- `tests/unit/architecture/test_web_landscape_mutation_fencing.py` and `config/cicd/masquerade_baseline.yaml`: eight stale entries and three unbaselined private helper sites.

The two failed nodes were then run on a separate untouched checkout at published `9f26833c`: `2 failed in 86.95s`, exit 1. Its canonical 3,972-file input dictionaries stayed identical, SHA-256 `1f2f8b944f4522a27c8d65512e939b1ffa7030d40c9435debf493a1fc881c9b8`. The full eleven masquerade `(path, line, message)` tuples and the complete three-mock diagnostic match the rebased run exactly. Positive, missing-header and mutated-finding controls exercised that comparison. Independent Astra review repeated the comparison and verified the implicated source/configuration bytes against published Git objects.

These are reproduced inherited failures, not merely inferred from unchanged code or equal totals. They are not suppressed or repaired in the refactor lane, and the rebase is not claimed as a green whole-tree integration. Further test-only slices may establish unchanged findings against this measured baseline; publication still requires the streaming owner to supply a corrected, stable integration base.

Root used one bounded, low-priority serial test process at a time after checking streaming capacity. Raw logs and frozen input captures are retained in `.claude/lanes/oversized-files-20261009/rebase/`; the clean published baseline checkout has its own `.claude/lanes/oversized-baseline-20261009/` evidence. Independent Astra high review is in [streaming-rebase-review.md](streaming-rebase-review.md). No full suite, PostgreSQL suite, push or merge was performed.
