# EFS session archive repair — 2026-10-06

## Defect and scope

The Nyx `BUGREP.md` reports session DELETE failing when EFS rejects Linux
`renameat2(RENAME_NOREPLACE)` with EINVAL. Local errno injection reproduced the
real archive helper failure and HTTP 500 while preserving the session and bytes.
Ordinary `os.rename` overwrites an empty competing directory; controlled existing
collision tests fail under that substitution, so it cannot be a safe fallback.

The repair is isolated on `fix/efs-archive-20261006`, based on
`edc844699a350a90a624089e12a5a1e75b7b2dde`. The untracked report remains unchanged
in the main checkout (SHA-256
`5df889ed9e3e32277b71a0a667e236d5bdc66a2d3c1fbd06b502c125fb14009f`).
The operator explicitly approved the bounded archive/recovery implementation.
No release metadata, Composer authoring/retry path, gateway provenance,
infrastructure, security configuration, credentials, or deployment is changed.
PR #275 was OPEN at `f8cc1be1928a0f5f6a72b308984c7a8259333e32` when compared
through GitHub's file-list API; its version-only changes did not overlap this fix.

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
were fixed and given regression controls. The final independent review approved
the corrected patch and rechecked its filesystem/lock controls on unchanged
source hashes. The whole-tree scanner initially timed out under an explicit
60-second test limit; its completed 300-second rerun above passed. Full-suite
validation, Daybreak review, and protected PR checks remain merge requirements.

This is a local EFS-like errno simulation, not a live EFS measurement. Stable
source inode identity across clients of the same filesystem is required.
The final empty-directory rmdir relies on cooperating writers honoring custody;
it is not inode-conditional against an arbitrary actor ignoring that protocol,
the same limitation as existing quarantine-directory retirement. Existing native
payloads needing reverse relocation on an unsupported filesystem remain
fail-closed. No production tests, paid provider tests, or deployment were run.
