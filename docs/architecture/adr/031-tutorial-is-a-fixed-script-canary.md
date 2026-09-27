# ADR-031: The Tutorial Is a Fixed-Script Canary for Freeform Composer

**Date:** 2026-07-22
**Status:** Accepted
**Deciders:** ELSPETH maintainer
**Tags:** composer, tutorial, freeform, backend-parity, testing-doctrine

## Context

The first-run tutorial sends a fixed example brief through the ordinary
freeform Composer. It then walks the learner through proposal review, an
explicit Run, inspection of the resulting audit evidence, and Graduation.
The brief is intentionally fixed so a broken general Composer affordance
cannot be hidden by rephrasing or an adaptive scripted driver.

## Decision

1. The tutorial uses the same provider-backed authoring, validation,
   proposal, execution, and audit paths as any other freeform session. It
   has no tutorial-only backend normalization, planner shortcut, or
   server-authored graph. Public sample-page addresses may be resolved at
   runtime, but the server does not compose or repair the tutorial pipeline.
2. The scripted walk does not re-plan around a failed transition. A failure
   is investigated in the general Composer surface, not worked around in
   the tutorial.
3. A successful Build alone is insufficient. The canary must complete a
   real Run, inspect evidence for that run ID, and reach Graduation from
   the committed session.
4. Every transition that publishes a proposed pipeline structure requires
   a real provider attempt. The acceptance gate checks this per transition,
   not only as a total across the walk.

## Consequences

- Tutorial failures are Composer machinery investigations, not grounds for
  tutorial-specific backend treatment.
- The fixed walk complements adaptive Composer acceptance tests: an
  adaptive planner can repair around a defect that the fixed walk exposes.
- Repeated identical planner rejections call for boundary diagnosis before
  increasing a repair budget.

## Alternatives considered

- Tutorial-only normalization or a generated fallback graph: rejected
  because it would make the canary pass while the general authoring path
  remains broken.
- Replacing the fixed walk with adaptive tests alone: rejected because
  those tests can absorb failures that a first-time user cannot.
