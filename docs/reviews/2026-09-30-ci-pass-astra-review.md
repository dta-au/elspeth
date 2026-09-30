# CI repair branch: Astra review

**Verdict: GO. No actionable correctness, security, or regression findings identified.** Final acceptance still requires completed frozen gates and remote CI; this review does not certify those results.

Reviewed immutable candidate `af8624dc2e0352b3f840272334bb8f528b32431b` against parent `9b43550558ed9e15da2933ed3ab12a284f8b5084`. The comparison contained 24 changed files, 5,021 insertions, and 96 deletions, measured with `git diff --stat`. Previously integrated documentation outside that comparison was excluded. HEAD remained at the reviewed candidate and the worktree had no tracked changes at review completion.

## Assessment

- [CI sharding](../../scripts/ci_test_shards.py) partitions pytest's selected collection after normal marker selection. Atomic worker manifests, finalized coverage checksums, collection completeness/disjointness checks, and source-path validation protect aggregation. The [workflow](../../.github/workflows/ci.yaml) retains the existing coverage floors and requires the aggregate result in CI Success.
- [Export reservation retries](../../src/elspeth/core/landscape/execution/sink_effect_reservation.py) reuse the original sequence and predecessor under the existing fenced transaction and source-run lock. They reject missing streams, inconsistent counters/tails, and invalid predecessor positions. Tests cover open and finalized retries, damaged evidence, and a legitimate pipeline predecessor.
- [Forced header fingerprinting](../../src/elspeth/plugins/infrastructure/clients/fingerprinting.py) now requires HMAC in every mode. Tests check value/key binding and refusal before transport, audit recording, or telemetry; callers and documentation reflect the key requirement.
- The worker signal test delivers its signal during a real sandbox lookup while retaining the worker's limits and handlers. It removes CPU-timing dependence without bypassing the behavior under test.
- Added Landscape and orchestration tests exercise persistence, rollback, replay, and ownership refusals. Database failure cases use real transactions and triggers, with targeted fault injection for lost acknowledgements and external failures. No coverage-only production bypass or lowered acceptance threshold was found.
- Dependency bounds and lock entries agree for the PyJWT floor and OAuthlib constraint. The JWT regression checks successful authentication despite an unrelated malformed sibling key.

## Verification

The reviewer ran the focused [sharding tests](../../tests/unit/cicd/test_ci_sharding.py) with the candidate worktree's interpreter, explicit `src` and `elspeth-lints/src` import roots, and serial outer pytest execution:

```text
python -m pytest tests/unit/cicd/test_ci_sharding.py -n 0 -q
exit=0
19 passed in 42.07s
```

Import provenance matched the candidate worktree's `src/elspeth/__init__.py` and `elspeth-lints/src/elspeth_lints/__init__.py`. No broad suite, tracked edit, branch mutation, remote workflow trigger, or signing operation was performed by this reviewer.

## Artifact rerun assessment

Fixed logical artifact names without `overwrite: true` were examined as a possible rerun collision. The hypothesis was not substantiated: the official artifact client explicitly describes duplicate names arising from reruns, while the pinned downloader requests the latest artifact per name before pattern filtering. This supports selecting renewed shard evidence alongside untouched successful siblings. See the [artifact client](https://raw.githubusercontent.com/actions/toolkit/main/packages/artifact/src/internal/client.ts), [pinned downloader](https://raw.githubusercontent.com/actions/download-artifact/d3f86a106a0bac45b974a628896c90dbdf5c8093/src/download-artifact.ts), and [latest-artifact selection](https://raw.githubusercontent.com/actions/toolkit/main/packages/artifact/src/internal/find/list-artifacts.ts). A real partial rerun was not performed during this review.

## Acceptance limits

The parent agent owns completion of the frozen gate, PostgreSQL tests, and remote required checks, including hosted Python shards, coverage artifact transfer/aggregation, dependency audit, and CodeQL. Those results were not certified by this review. Existing keyless lint findings and operator signing status remain separate from CI acceptance.

## Subsequent local acceptance

The parent completed the canonical gate on the same reviewed candidate. The
recorded terminal results were:

```text
stage=ruff exit=0 seconds=0 fatal=1
stage=mypy exit=0 seconds=3 fatal=1
stage=contracts exit=0 seconds=19 fatal=1
stage=lints exit=1 seconds=124 fatal=0 findings=2438
stage=pytest exit=0 seconds=2535 fatal=1 59865 passed, 99 skipped, 2 xfailed, 9998 warnings in 2525.07s (0:42:05)
stage=testcontainer exit=0 seconds=1290 fatal=1 652 passed, 1 skipped, 60107 deselected, 12 warnings in 1274.40s (0:21:14)
after=af8624dc2e0352b3f840272334bb8f528b32431b 01ba4719c80b6fe9 frozen=yes
RESULT=PASS
```

Separate checks of the finalized coverage database exited 0 for all unchanged
floors: overall 85%, Landscape 92%, canonical 99%, orchestrator 90%, and
contracts 62%. The lint stage is the existing nonfatal keyless diagnostic;
this evidence does not assert operator signature clearance. Remote required
checks remain the delivery acceptance criterion.
