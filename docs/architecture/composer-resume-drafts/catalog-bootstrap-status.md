# Catalog bootstrap isolated source draft

Worktree: `/workspace/elspeth/.claude/worktrees/composer-catalog-bootstrap`
Branch: `codex/composer-catalog-bootstrap`
Base: `ad773c656ff6009bf13e7916e1f249076e8da9b2`
Patch including new files: `work/catalog-bootstrap.patch`.
No ROOT edits, commits, pushes, imports, tests, builds or measurements.

## Draft implementation

- Closed `PluginCatalogBootstrapResponse` contains existing public policy and
  source/transform/sink summaries. `/api/catalog/bootstrap` authenticates using
  the existing dependency, obtains one fresh request snapshot, calls each view
  listing once and renders policy from those exact lists/snapshot. Existing
  endpoints remain; policy rendering now takes snapshot control modes as well
  as its other captured facts. Private/no-store/Vary/snapshot headers remain.
- Client checks the closed envelope/policy shapes, nonempty header/body binding,
  group kinds and public summary fields, and agreement of visible group IDs
  with the policy inventory. Wire mismatch fails closed once through the real
  client; arbitrary malformed payloads are not retried.
- Cold, forced and known-stale frontend loads fetch the bundle; warm loads
  always request current policy, even with a matching caller fingerprint hint.
  Changed warm policy clears all old custody and installs the bundle's own
  possibly newer policy/groups atomically. Valid typed wrong-owner responses
  have a bounded three-attempt refusal path. Schema caches retain owner keys.
- Load coalescing includes auth generation. Distinct catalog generation changes
  only on actual catalog replacement/clear, so a matching warm policy check
  cannot strand an in-flight schema load. Late auth/principal/load/schema
  responses cannot overwrite a replacement catalog.

## Draft controls

`test_bootstrap.py` uses actual app authentication, real users/inventory and
request snapshot/view producers. Delegating counters compare the bundle with
the old four-route control (expected 1 vs 4 snapshots, 1 vs 2 view list calls
per group); these expectations remain UNMEASURED. It checks profile/ADI public
parity, principal inventory isolation, secret addition/rotation/removal, fresh
configured policy/profile/endpoint replacement, headers, closed response schema
and public-response mutation isolation.

Store tests preserve the five-case independently pinned policy fixture and
schema rotation/refusal assertions, adding cold atomicity, warm freshness,
newer-bundle installation, force/invalidate, provider switch, stale responses,
credential rotation/coalescing and in-flight schema warm refresh controls.
Client tests exercise malformed envelopes/header mismatch; a separate
store+actual-client+decoder test stubs only HTTP, proving fail-closed once.
Catalog drawer/narrative/execution and app integration fixture adapters now
supply the bundle while keeping their existing UI outcome assertions.

## Static review and pending execution

Astra found two genuine draft races (schema stranded by warm refresh, and
new credentials coalesced into the old request); both have source fixes and
new regression controls. A mocked retry claim was corrected to match the real
client's single malformed-wire refusal. Final static review found the draft ready for focused qualification only; runtime qualification and integration approval remain pending.
`git diff --check` completed exit 0; this is whitespace checking only.

Request a focused runtime window after the root profile/gate owner releases
capacity: affected authenticated routes/policy/schema tests, store/client/UI
tests, then frontend type/lint/build, contracts/census/affected architecture
inventories and real browser catalog interaction. Use both source roots,
`-n 0`, unique project-local temporary/log paths and explicit terminal receipts.
No golden/census/source-guard pins were changed. No latency or producer-count
benefit is claimed until the real counters and later isolated measurements run.
