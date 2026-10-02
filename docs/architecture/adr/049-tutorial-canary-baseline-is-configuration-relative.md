# ADR-049: The Tutorial Canary's Baseline Is Configuration-Relative, Not Byte-Fixed

**Date:** 2026-09-28
**Status:** Proposed
**Deciders:** ELSPETH maintainer
**Tags:** composer, tutorial, freeform, backend-parity, testing-doctrine,
          preferences, skillpacks

## Context

[ADR-031](031-tutorial-is-a-fixed-script-canary.md) keeps the first-run
tutorial on the ordinary freeform Composer path with a fixed, non-adaptive
script. Proposed standing user preferences and plugin skillpacks would add
declared inputs to that path. Suppressing either input only for the tutorial
would violate backend parity; requiring byte-identical planner prompts across
different configured accounts would make the canary unusable.

The canary detects defects because its script cannot adapt around a failed
transition, not because every deployment sends identical prompt bytes.

## Proposed decision

1. Declared user preferences and installed skillpacks apply to the tutorial
   exactly as they apply to any other freeform session. No tutorial-only
   prompt branch or fallback is allowed.
2. The tutorial brief, driver actions, and success criteria remain fixed
   within a walk. The driver cannot rephrase, re-plan, or improvise to rescue
   a failed transition.
3. Compare results against the baseline for the declared configuration,
   recording its input hashes when these features are implemented. The
   clean-configuration walk remains the release-boundary signal.
4. When a configured walk fails, repeat it without optional inputs as a
   human-directed diagnostic. A remaining failure points to general
   Composer machinery; a clean-only pass points to that configuration.
   The serving path never performs this diagnostic as a fallback.

## Consequences

The configuration must be visible to acceptance and audit tooling when the
proposed features land. This ADR does not authorize adaptive tutorial
behavior, weaken the provider-call requirement, or change the explicit
Run, Audit, and Graduation capstone.

## Alternatives considered

- Suppress configuration during the tutorial: rejected as a tutorial-only
  authoring path.
- Treat every configured failure as non-signal: rejected because a real
  machinery defect can also appear on a configured account.
