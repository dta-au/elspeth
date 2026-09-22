---
title: Repair composer live-review follow-ons: final reply, prompt provenance, disclosure, probe
labels: [area/composer, type/task]
---

A live review of the composer produced a set of related defects, recorded in
`docs/analysis/2026-09-15-composer-live-review-investigation.md`. This issue tracks
repairing them as one piece of work.

## Scope

- A bounded, tools-disabled final reply after a terminal review handoff.
- An accurate audit link for the approved prompt artifact, and an optional unused
  fallback.
- Honest disclosure of the adapted prompt, of failures, and of the model in use.
- A routine guided probe that does not surface an HTTP error.
- Readable approval labels.

Actual LLM outputs must be preserved throughout; none of the above may be achieved by
substituting or rewriting what the provider returned.

## Note

Several existing issues overlap this scope. Fixing the items listed here resolves a subset
of them and must not be reported as resolving the broader issues in full.
