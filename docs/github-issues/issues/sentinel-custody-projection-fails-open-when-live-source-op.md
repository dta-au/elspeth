---
title: Sentinel custody projection fails open when live source options carry no blob_ref
labels: [area/composer, area/audit, type/bug]
---

When a live source's options carry a path but no `blob_ref`, sentinel custody validation passes without checking blob identity, and the projection then stamps the reviewed blob's sentinel onto a source that reads a different blob's bytes. The result is an affirmative, false custody claim on user- and provider-visible surfaces.

## What happens

Reproduced in-process against a saved production session state, read-only, with the mutation applied in memory only. A source whose live options contained a path and no `blob_ref` had its path replaced by the reviewed blob's sentinel:

```
live path BEFORE : <internal storage path for blob A>
PROJECTED path   : blob:<blob B>
```

The projection asserts custody of blob B for a source that actually reads blob A's bytes.

## Why

`validate_guided_reviewed_sentinel_source_mapping` in `src/elspeth/web/composer/guided_blob_refs.py` performs several checks, but only one of them is an identity check, and it is conditional. The others are shape checks: that the live carrier key set equals the binding's, and that each carrier value is a non-empty string containing no NUL byte and not already prefixed as a blob reference. The single check that compares the live blob against the reviewed blob is guarded by `"blob_ref" in options`. With that key absent, the guard is skipped and the mapping validates.

`redact_guided_snapshot_storage_paths` in `src/elspeth/web/composer/redaction.py` then projects the reviewed sentinel onto the live source on the strength of that validation.

The `blob_ref`-absent shape is not hypothetical — the codebase produces it. The guided manual `set_source` commit path strips `blob_ref` precisely because it cannot prove that `path` equals the blob's `storage_path`; the comment recording this sits beside the redaction call in `src/elspeth/web/sessions/routes/_helpers.py`. A freeform-authored source that omits `blob_ref` while colliding on the reviewed source's name lands in the same shape.

## Impact

The direction of failure is what makes this worse than its siblings. Related defects on this path fail **closed** — too late, but with a 500. This one fails **open**: it silently emits a custody claim that is not true, into the surfaces the audit trail exists to make trustworthy. A reader of the audit record has no signal that the identity was never verified.

## Fix

Sentinel custody projection must not assert identity it has not verified. Where the live options lack `blob_ref`, the projection should either establish identity by another means — for example, that the live path equals the reviewed blob's `storage_path` — or degrade to the same named custody-unavailable outcome as the fail-closed arm. Stamping the reviewed sentinel onto an unverified source should not be reachable.

This belongs with the terminal-direction work on the same path; the disagreement test that pins export, admission and projection directions should cover this arm as well.
