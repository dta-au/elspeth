# Current State — ELSPETH

**Checkpoint:** 2026-07-24
**Release branch:** `release/0.7.2`
**Release-prep issue:** `elspeth-64c319bf4d`
**Release milestone:** `elspeth-6343920a47`

## The Bet Right Now

**Prepare 0.7.2 as a distinct maintenance release, prove one unchanged
candidate, then hand the operator the signing and publication boundary.**

Commit `720d44133` is the semantic 0.7.1 release point. The current release
branch contains the subsequent production-path hardening; those changes must
not remain folded into the historical 0.7.1 notes.

## Current Release State

- The root package metadata and lockfile identify 0.7.2.
- Current release labels, container examples, website footers, and release
  documentation indexes identify the 0.7.2 line.
- `CHANGELOG.md` preserves the 0.7.1 session cutover at epoch 35 and assigns
  epoch-36 blob-deletion cleanup, the epoch-37 guided-plan decline contract,
  the epoch-38 decline replay message locator, the epoch-39 policy refusal
  code, and the epoch-40 coalesce timeout and epoch-41 node option summary payload
  cutovers, plus epoch-42 failed guided-operation replay enrichment,
  epoch-43 run-diagnostics writer attribution, epoch-44 honest
  planner-repair-exhaustion failure classification, and epoch-45
  operator-profiled Textract document authoring, to 0.7.2.
- `SESSION_SCHEMA_EPOCH` is 47, guided checkpoint schema is 11, and
  `SQLITE_SCHEMA_EPOCH` is 32. An upgrade from 0.7.1 recreates both a stale
  session store and a Landscape store below epoch 32.
- Web Composer freeform and guided authoring, Composer tools, YAML
  import/export, validation, and graph views support first-class, correlated
  `row_union` barriers for declared-order long-format processing.
- The row-union umbrella issue `elspeth-a5b86149d4` remains open: canonical
  configuration, build, contract, runtime, and guided evidence is present, but
  audit, recovery, concurrency, browser-backed round-trip, and scale acceptance
  remains deferred.
- No 0.7.2 tag or final release candidate has been published.

## Release Gates

- Resolve autonomous failures in the complete local release suite and bind the
  result to one unchanged release SHA.
- Re-run PostgreSQL, packaging/container, and live AWS acceptance against that
  final SHA; older Plan 12 evidence does not transfer to the moved branch tip.
- Complete the operator-held trust-tier judgment signing and regenerate the
  fingerprint baseline through the supported tooling. Do not bypass signature
  verification or hand-edit the baseline.
- Tag, push, publish images, and create the GitHub release only after every
  required gate passes on the same candidate.

## Next Session, Start Here

1. Inspect `elspeth-64c319bf4d` and the exact release-branch SHA.
2. Resume the first incomplete release gate without changing the candidate.
3. Keep signing and external publication operator-controlled.
