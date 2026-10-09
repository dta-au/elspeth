# Independent review of the streaming-base rebase

Review status: approved for the bounded rebase and refactor scope. The three refactor commits preserve the reviewed changes exactly, and no introduced defect was found. Both failing whole-tree gates reproduce identically on the untouched published streaming base. Integration gates remain red; this is not a green-gate or merge-clearance statement.

## Ancestry and scope

Reviewed HEAD is `df7deda0e31df7e5a50a85b9e16363893f41eb44`, based on published streaming commit `9f26833c8f6a7c56abd2c5b35c80484ebe0c82bc`. An independent `git ls-remote --exit-code origin refs/heads/work/composer-https-streaming-083-20261007` returned that exact published commit. This verifies the published branch tip at review time, not the streaming owner's uncommitted integration or repair state.

The preserved pre-rebase tip is `3f7de818ec2e425e21976cd20cc1e5699292c4da`, also retained by `backup/oversized-files-pre-streaming-20261009`. The old base was `9e481bc85a32ee15f808c5e2b16cab7634e57b8e`. Independent Git measurements establish:

```text
new base is ancestor of new HEAD: exit 0
own commits above new base: 3
range-diff:
794531426 = 1a1a3110d  processor split
fb6c7185f = bd9859514  executor split
3f7de818e = df7deda0e  DAG split
old/new owned path sets: identical, 27 paths
all 27 old/new final Git blob IDs and file modes: identical
owned paths intersect changes from old base to new base: 0
owned paths intersect canonical streaming ownership list: 0
canonical ownership list: 538 unique paths
ownership-list SHA-256: 3149d9cf6e2b97e92a3e5002da4d0dc7a3c7430df0c5535eae7f8ad8366afc38
ownership intersection positive control: detected a real reserved path
```

The new history intentionally excludes the main-only commits for PRs #282 and #283. Only the three refactor commits were replayed. No streaming owner repair was copied, and no policy/configuration change was introduced to suppress the new base's failures. Confirmation that this published tip is the owner's final desired integration base remains outside this review.

## Test-impact review

The processor, executor, and DAG source bodies, assertions, decorators, helper identities, patch targets, and collection mapping previously reviewed are preserved by exact final blobs. The prior review reports remain historical evidence for their original bases; the refreshed run below supplies evidence on this streaming base.

The inherited `tests/conftest.py` adds three function-scoped autouse fixtures. I reviewed the complete conftest delta and their supporting ownership helpers. All 15 pre-existing top-level definitions retain the same AST, and the unit/core/engine conftests remain byte-identical. The three additions are:

- `explicit_required_executor_recovery`: checks custodian settlement before and after each test and installs explicit test recovery state. The settlement helper refuses outstanding reservations, futures, quarantined workers, or unfinished recovery observations; it does not hide them with cleanup.
- `explicit_owned_process_watchdog`: installs the explicitly owned test watchdog, with no subprocess or signal authority.
- `explicit_owned_test_telemetry`: supplies an isolated telemetry installation and offline exporter, then shuts down each test-owned installation. Its provider slot and registry are isolated from the global provider.

These are inherited changes that affect the moved tests through their existing conftest chain. Their scope and cleanup do not reveal a split-specific defect. The reviewed refactor adds no forwarding tests or duplicate collection sites.

## Refreshed evidence and gate failures

Read directly from completed raw logs and exit files under `.claude/lanes/oversized-files-20261009/rebase/`:

```text
focused.exit: 0
focused.log: 627 passed in 28.64s
gates.exit: 1
gates.log: 2 failed, 24 passed in 117.81s
before.json == after.json: true, including HEAD
canonical Python inputs: 3,981 files
input-dictionary SHA-256: 0490b715e13ea0196b43b0b13b2168fda20a1d96dd7d49497939eb9ea3b34618
```

The 627 affected cases comprise processor 226, executor/consumer 271, and DAG 130. I did not run pytest; the root agent held the shared host's serial validation slot.

The two failing gates are:

1. `tests/unit/test_mock_discipline_baseline.py::test_no_unspecced_direct_mock_constructors`: three unspecced mocks in `tests/unit/web/execution/test_recovery_coordinator.py`, at lines 121, 368, and 421.
2. `tests/unit/elspeth_lints/test_masquerade_gate.py::test_live_tree_has_zero_unbaselined_findings`: eight stale baseline entries and three unbaselined `_reserve_private_*` helpers in `tests/unit/architecture/test_web_landscape_mutation_fencing.py`.

The two implicated test sources, `config/cicd/masquerade_baseline.yaml`, and both failing gate modules are independently byte-equal to published base `9f26833c`. The root agent then ran precisely those two failing nodes on an isolated checkout of that base. I read its completed log and exit file, parsed both frozen-input captures, and independently compared the failure records:

```text
isolated base HEAD: 9f26833c8f6a7c56abd2c5b35c80484ebe0c82bc
failed-nodes.exit: 1
failed-nodes.log: 2 failed in 86.95s
base before.json == after.json: true, including HEAD
base canonical Python inputs: 3,972 files
base input-dictionary SHA-256: 1f2f8b944f4522a27c8d65512e939b1ffa7030d40c9435debf493a1fc881c9b8
complete masquerade finding tuples (path, line, message): 11, exactly equal
complete three-mock failure diagnostic: exactly equal
finding comparison mutation control: changed message rejected
```

The isolated-base evidence is under `.claude/lanes/oversized-baseline-20261009/` in the `oversized-streaming-baseline-20261009` worktree. Matching complete diagnostics, together with exact implicated blobs and frozen runs, establishes that these observed failures are inherited from the published base.

Severity: no refactor/rebase findings. The two whole-tree failures block a claim of clean integration gates and must remain visible. No unauthorized repair or allowlist adjustment is recommended in this lane.

## Limits

I independently parsed both pairs of input dictionaries and recomputed the SHA-256 values above. The completed refreshed and baseline runs are frozen-input evidence. This report does not certify a full repository suite, PostgreSQL tests, signed trust-tier clearance, the streaming owner's uncommitted repairs, or the final choice of owner integration base. Approval covers the three reviewed refactors and their exact replay onto the verified published tip; subsequent changes require their own affected checks.
