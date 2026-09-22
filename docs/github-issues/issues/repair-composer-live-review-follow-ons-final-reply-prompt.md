---
title: "Repair composer live-review follow-ons: final reply, prompt provenance, disclosure, probe"
labels: [area/composer, type/task]
---

A live review of the composer surfaced five small defects that share a theme: what the
interface tells the user about a completed session does not match what actually happened.
They are recorded together here because they were found together, not because they must be
fixed together.

Start by reading `docs/analysis/2026-09-15-composer-live-review-investigation.md`. It is
the specification for this work and contains the observations behind each item below. The
code is in the composer, under `src/elspeth/web/composer/` and its frontend in
`src/elspeth/web/frontend/src/components/composer/`.

## Scope

Each item is separable; a new contributor can take one without taking the rest.

1. A bounded final reply, with tools disabled, after a terminal review handoff — so a
   session that has been handed on still closes with an answer rather than silence.
2. An accurate audit link for the approved prompt artifact, plus an optional unused
   fallback.
3. Honest disclosure of the adapted prompt, of failures, and of which model was used.
4. A routine guided probe that completes without surfacing an HTTP error to the user.
5. Readable approval labels.

Throughout, the actual outputs returned by the provider must be preserved. None of these
items may be achieved by substituting, truncating or rewriting what the model produced;
the fix is always in what the interface reports, never in the content it reports on.

## Fix

Done, per item, is that the surface in question states something that is true of the
session it describes, demonstrated by a test that asserts the reported value against the
recorded one rather than against a fixture constant.

Size: five independent repairs, each small. This is a reasonable first ticket for someone
new to the composer, taken one item at a time.

## Note

Several other recorded issues overlap this scope. Completing the items above resolves part
of them and must not be reported as resolving any of them in full.
