---
title: Sentinel custody projection fails open when live source options carry no blob_ref
labels: [area/composer, area/audit, type/bug]
---

Status: repaired in the pre-publication security patch for `release/0.8.1`.
Public issue import remains held. See the [review and verification record](../../reviews/2026-09-23-pre-publication-security-review.md).

Before the repair, when a live source's options carried a file path but no `blob_ref`, custody validation passed without checking blob identity, and the code then stamped the approved blob's placeholder onto a source that read a different blob's bytes. The result was an affirmative, false custody claim on surfaces users and the model provider could see. The sections below describe the original defect.

## What happens

Some terms first. A *blob* is an uploaded file held in internal storage. A *reviewed blob* is one a human has approved for use. *Custody* is the audit trail's claim about which bytes a given source actually read. Because internal storage paths must not be shown to users or sent to the model provider, they are replaced on the way out by a *sentinel* — a `blob:<id>` placeholder. The option keys holding those paths are called *carriers*.

Reproduced in-process against a saved production session state, read-only, with the mutation applied in memory only. A source whose live options contained a path and no `blob_ref` had its path replaced by the wrong blob's sentinel:

```
live path BEFORE : <internal storage path for blob A>
PROJECTED path   : blob:<blob B>
```

The audit record now asserts custody of blob B for a source that reads blob A's bytes.

## Why

The work starts in `src/elspeth/web/composer/guided_blob_refs.py`, in `validate_guided_reviewed_sentinel_source_mapping`. It performs several checks, but only one compares the live blob against the reviewed blob, and that one is conditional. The rest are shape checks: that the live carrier key set equals the binding's, and that each carrier value is a non-empty string containing no NUL byte and not already prefixed as a blob reference. The single identity check is guarded by `"blob_ref" in options`. With that key absent, the guard is skipped and the mapping validates.

`redact_guided_snapshot_storage_paths` in `src/elspeth/web/composer/redaction.py` then substitutes the reviewed sentinel on the strength of that validation.

The `blob_ref`-absent shape is not hypothetical — the codebase produces it. The manual `set_source` commit path strips `blob_ref` precisely because it cannot prove that `path` equals the blob's `storage_path`; the comment recording this sits beside the redaction call in `src/elspeth/web/sessions/routes/_helpers.py`. A source authored through the free-form Composer mode that omits `blob_ref` while colliding on the reviewed source's name lands in the same shape.

## Impact

The direction of failure is what makes this worse than neighbouring defects on the same path, which fail **closed** — too late, but with a 500 and no false claim. This one fails **open**: it silently emits a custody claim that is not true, into exactly the surfaces the audit trail exists to make trustworthy. Nothing in the record signals that the identity was never verified.

## Fix

The validator now requires a canonical live `blob_ref` matching the reviewed identity.
The trusted guided materializer retains that identity when resolving a reviewed
sentinel into a private storage path. It does not infer identity from arbitrary paths.

Missing identity rejects active projection, export and persistence admission.
Terminal and explicitly tolerant history projections instead report
`custody_unavailable` and mask the private paths. Regressions cover both carrier
keys, conflicting and missing identities, legitimate materialization, export and
persistence. The historical false-custody cases now require refusal or degradation;
their original corpus and golden JSON remain unchanged.
