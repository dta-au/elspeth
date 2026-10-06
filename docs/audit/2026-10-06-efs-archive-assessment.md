# EFS session archive repair — 2026-10-06

## Defect and scope

The Nyx `BUGREP.md` reports session DELETE failing when EFS rejects Linux
`renameat2(RENAME_NOREPLACE)` with EINVAL. Local errno injection reproduced the
real archive helper failure and HTTP 500 while preserving the session and bytes.
Ordinary `os.rename` overwrites an empty competing directory; controlled existing
collision tests fail under that substitution, so it cannot be a safe fallback.

The repair is isolated on `fix/efs-archive-20261006`. Its initial base was
`edc844699a350a90a624089e12a5a1e75b7b2dde`; normal integration of landed PR #275
produced candidate `b8c34c05b7494e64db1705e9cb481bd34ef2dcf5`, based on main
`23822a7477624d6264c60fade0905c19e8c552d7`. The untracked report remains unchanged
in the main checkout (SHA-256
`5df889ed9e3e32277b71a0a667e236d5bdc66a2d3c1fbd06b502c125fb14009f`).
The operator explicitly approved the bounded archive/recovery implementation.
This repair authors no release metadata, Composer authoring/retry path, gateway
provenance, infrastructure, security configuration, credentials, or deployment
change. It consumes landed PR #275's release and dependency/gateway fixes from
main. Independent Git-object comparison of that integration measured 15 archive
and 59 main changed paths with no overlap and exact parent-object preservation.

## Recovery representation

Native no-overwrite relocation remains the preferred path. Only EINVAL, ENOSYS,
EOPNOTSUPP, or an unavailable native primitive at **stage** selects in-place mode,
after checking that the exact source remains and no payload appeared. EXDEV,
collisions, invalid paths, access/I/O failures, and fence failures remain errors.

In-place mode leaves canonical bytes in place until the normal fenced database
archive-delete is confirmed. A private immutable canonical JSON record carries
the session/operation/epoch and source root inode. Exclusive hardlinks publish
the record in the operation directory and inside the source root as a witness.
The source witness must share the record's current-mount inode/device identity;
ownership, mode, link counts, canonical encoding, and root inode are checked.
Numeric device identity is not persisted because it is mount-local.

An exact current lease can remove its witness and restore ordinary canonical
state without deleting bytes. A restored-phase hardlink survives interrupted
rollback. Lost or unknown database authority retains all obligations. Consumed
cleanup deletes children through a no-follow root descriptor, records a cleaned
phase, removes the witness and empty root, fsyncs the parent, then retires its
records. Terminal phase hardlinks survive interrupted metadata retirement.
An empty operation directory after manifest unlink is recoverable residue.

Every cleanup validates the operation control entry set and manifest-temp
binding before deleting bytes or evidence. The consumed scanner validates all
session obligations before deleting any one of them. Unexpected entries,
multiple native payload obligations, malformed identities, half-present database
session/fence pairs, and substituted source paths fail closed.

## Authority and replica coordination

The shared blob custody primitive now lives in `sessions/locking.py`; its old
blob-service import remains available. Archive in-place stage/rollback drains
custody and checks exact live authority before and after filesystem work.
Consumed cleanup holds that same custody namespace and proves both session and
fence absent in a short read-only transaction. No transaction spans filesystem
deletion; no lease is fabricated from disk metadata.

Startup runs consumed recovery before inline custody reconciliation and before
membership registration. The periodic recovery task uses the same path.
Background recovery attempts custody without waiting: busy live or terminal
candidates are skipped for a later periodic retry. Discovery tolerates a peer
retiring a snapshotted session directory, still rejecting present substituted
symlinks/non-directories. Operations are re-listed under custody.

PostgreSQL acquisition/commit and reset/unlock/commit failures invalidate the
physical connection. Session-level advisory locks survive rollback; an uncertain
connection must not return to the pool retaining custody. This also repairs the
existing blocking custody helper's equivalent failure path.

Periodic archive OSErrors use the existing bounded failure budget, logging class,
errno, and count without exception text or filesystem paths. Integrity failures
remain fatal. Startup recovery failures prevent serving unresolved custody.

## Verification and limits

Completed local controls before the full gate:

```text
reproduction-complete.log: exit=0; 5 passed in 5.98s
native-integrity-controls.log: exit=0; 6 passed, 91 deselected in 0.28s
unsafe-fallback-negative-control.log: exit=1 (intentional);
  2 failed, 1 passed, 94 deselected in 0.51s
implementation-existing-tests.log: exit=0; 233 passed in 18.70s
recovery-passing-controls.log: exit=0; 152 passed in 5.95s
postgres-contention-focused.log: exit=0; 3 passed in 4.90s
locking-postgres-final.log: exit=0; 24 passed in 6.40s
control-validation-negative.log: exit=1 (intentional);
  2 failed, 30 deselected in 0.28s
masquerade-final.log: exit=0; 244 passed in 182.66s
```

The new controls cover actual authenticated upload and DELETE, current/committed/
unknown/lost terminal outcomes, another replica recovering consumed obligations,
interrupted publication/restore/purge/retirement, invalid control entries, source
substitution, extra hardlinks, changed current-mount device numbers, peer
retirement, startup ordering, bounded retries, and PostgreSQL custody contention.
Independent review found recovery and locking defects during development; these
were fixed and given regression controls. The completed development review approved
the corrected patch and rechecked its filesystem/lock controls on unchanged
source hashes. The whole-tree scanner initially timed out under an explicit
60-second test limit; its completed 300-second rerun above passed. These are
development-stage measurements; the integrated candidate's validation and review
are recorded below. Required protected checks remain a merge requirement.

PR #276's first CI run exposed stale PostgreSQL test-double signatures and
exact mutation-authority inventory drift. The doubles now accept and forward
the explicit custody callbacks. Independent live baseline/candidate scans
reviewed the relocated acquisitions and updated exact identities, including
five ordinal changes assessed by their actual statement anchors. The scalar
try-lock receives one read admission bound to the reviewed advisory helper;
the blocking custody wrapper remains an explicit connection escape, without
mutation authority. Scanner and table-policy implementations were not widened.

All prior caller mutation/escape controls remain, with new live-source controls
for advisory-helper DELETE/leak/yield and the consumed predicate's DML/connection
escape. Independent CI-repair review approved the corrected repair and passed all
seven new controls. Completed local results on unchanged test inputs:

```text
postgres-repair-focused.log: exit=0; 3 passed, 17 deselected in 5.14s
postgres-repair-file.log: exit=0; 20 passed in 9.45s
mutation-gate-final.log: exit=0; 271 passed, 1 xfailed in 179.42s
  before/after architecture and PostgreSQL test hashes: OK
ruff-ci-repair.log: exit=0; All checks passed!
```

The expected xfail remains the repository's existing writer-authority burn-down;
the relocated raw custody escape is explicitly visible in that inventory.
These checks validate the focused test/inventory repair. On archive head
`bba870e35d90d301f1485c6130389cd30b40767f`, CI run `37409235806` passed all eight
Python shards, PostgreSQL contention proofs, frontend checks, and coverage. Its
dependency audit failed on fsspec 2026.1.0 and multidict 6.7.0; the normal main
integration consumes PR #275's fsspec 2026.6.0 and multidict 6.9.1 fixes.

The integrated candidate used a task-private Python 3.13.15 environment from the
frozen all-extras lock, with both owned source roots explicitly selected. Its
canonical static gate recorded ruff, mypy, and contracts exit 0 and a frozen tree.
Keyless lints remained nonzero; the controlled normalized comparison to the
reviewed archive candidate had 1468 distinct emitted findings on each side, with
no additions or removals. This does not establish operator signature clearance.
The complete affected serial selection exited 0: 1526 passed, one expected
writer-inventory xfail, one quorum-policy warning, in 661.97 seconds. HEAD and
tracked state were unchanged. This is not a local full Python/PostgreSQL suite.
An earlier restricted-sandbox attempt stalled and was terminated with exit 143;
the unchanged isolated metrics test, full app file, and original combined order
then passed outside the sandbox. The original stall's cause remains unproven.

John approved transmission of this archive candidate and relevant source,
excluding secrets/credentials, through the existing ChatGPT-authenticated CLI.
The read-only CLI review selected `gpt-daybreak-blue-latest`, exited 0, and gave
correctness GO conditional on protected CI. It identified one P3 documentation
provenance issue, corrected in this assessment. Its prose self-label contradicted
the CLI model metadata; the unedited report and invocation record are retained
separately. Subsequent changes require review appropriate to their scope, and the
final head must pass fresh protected CI before merge. No Astra substitution was
made for that completed invocation.

This is a local EFS-like errno simulation, not a live EFS measurement. Stable
source inode identity across clients of the same filesystem is required.
The final empty-directory rmdir relies on cooperating writers honoring custody;
it is not inode-conditional against an arbitrary actor ignoring that protocol,
the same limitation as existing quarantine-directory retirement. Existing native
payloads needing reverse relocation on an unsupported filesystem remain
fail-closed. No production tests, paid provider tests, or deployment were run.
