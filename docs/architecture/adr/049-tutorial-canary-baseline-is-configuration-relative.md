# ADR-049: The Tutorial Canary's Baseline Is Configuration-Relative, Not Byte-Fixed

**Date:** 2026-09-10
**Status:** Proposed
**Deciders:** ELSPETH maintainer
**Tags:** composer, tutorial, guided, backend-parity, testing-doctrine,
          preferences, skillpacks

## Context

[ADR-031](031-tutorial-is-a-fixed-script-canary.md) makes the first-run
tutorial a deliberately fragile canary over the general guided surface, and
grants it exactly one privilege:

> Its only privilege is a frozen prompt.

Two designs dated 2026-09-10 add inputs that vary that prompt:

- **User standing preferences** (`2026-09-10-user-standing-preferences-design.md`)
  — a per-user block of personal and stylistic preferences rendered into the
  cacheable prefix.
- **Plugin skillpacks** (`2026-09-10-plugin-skillpacks-design.md`) — a
  manifest line per available pack in the catalog block, with bodies pulled
  on demand.

Both apply to the guided surface, so both reach the tutorial. That produces a
direct collision with the two rules ADR-031 depends on:

- If the tutorial's prompt varies with user preferences and installed
  plugins, it is no longer *frozen* in the byte sense ADR-031's phrasing
  implies.
- If we suppress those inputs for the tutorial, we have built a tutorial-only
  prompt branch — banned by the second composer invariant in AGENTS.md, in
  the same sentence that bans tutorial-only normalisation and short-circuits,
  and rejected by ADR-031 itself as converting the canary into a liar.

There is no third option that leaves both rules literally intact. The
question is which reading of "frozen" the canary's value actually rests on.

Re-reading ADR-031's own reasoning settles it. The defects the tutorial
caught — the silently-accepted empty auto-proposal, missing
interpretation-review events, orphaned review cards, the binder severing
legal wiring — were all caught because the walk had **no adaptive shock
absorber**: it could not rephrase, could not re-plan around a broken
affordance, and had no repair budget quietly compensating for machinery bugs.
ADR-031 names this directly as "no adaptive slack". Byte-identity of the
prompt across all deployments and users was never the mechanism; the absence
of *adaptive* variation was.

A user preference block and a pack manifest are static, declared, and
hash-recorded before the walk begins. They are configuration, not slack.

## Decision

1. **Both inputs apply during the tutorial, uniformly.** No tutorial-only
   suppression, normalisation, or branch. ADR-031's backend-parity rule is
   preserved exactly as written; the tutorial continues to run the same
   guided machinery every user exercises, now including the same
   configuration inputs.

2. **"Frozen" means free of adaptive variation, not byte-identical.** The
   tutorial's privilege is restated: *its script cannot adapt*. The frozen
   prompt cannot be rephrased, the walk cannot re-plan around a broken
   affordance, the driver cannot improvise, and no repair budget compensates.
   A static, declared, hash-recorded configuration input does not violate
   that and does not weaken the signal.

3. **The canary baseline is configuration-relative.** The tutorial's
   `PlannerCapabilityManifest` records `user_instructions_hash` and
   `skillpack_manifest_hash` (both required by the companion specs for
   independent reasons). A tutorial result is compared against the baseline
   *for that configuration*, not against one fixed string.

4. **Triage rule — an input-induced red is never filed as a machinery
   defect.** A tutorial red whose manifest carries a non-null
   `user_instructions_hash` or a non-null `skillpack_manifest_hash` is first
   re-run as a **diagnostic** with those inputs absent:
   - still red → machinery defect; investigate per ADR-031, which is
     unchanged.
   - green → input-induced; the finding is about that configuration, and if
     the input is a shipped skillpack it is a pack defect to fix.

   The diagnostic re-run is a debugging action taken by a human or a test
   harness. It is **never** a product code path, never automatic in the
   serving path, and never a fallback the running tutorial takes on its own —
   that would be precisely the tutorial-only branch this ADR refuses.

5. **The clean-configuration walk remains the release-boundary signal.** The
   tutorial run weighted at a release boundary is the one with no user
   preferences and only shipped packs available. ADR-031 point 2
   ("tutorial-green is a machinery signal") applies to that walk.

## Consequences

- ADR-031 stands in full. Only its parenthetical characterisation of the
  privilege — "a frozen prompt" — is amended, to "a script that cannot
  adapt". Its four decision points are untouched.
- A new failure class exists: a tutorial red caused by a user's own
  preferences or by a defective shipped pack. Decision 4 exists solely to
  keep that class from being mis-filed as a machinery investigation, which
  would waste exactly the attention ADR-031 was written to direct well.
- Practical exposure is small and should not be mistaken for zero. The
  first-run tutorial usually runs before a user has set any preferences, so
  the common case is an empty block and a manifest of only shipped packs —
  which is the clean configuration of decision 5. The rule matters for the
  retake path and for deployments shipping their own packs.
- The audit cost is two hash fields on the manifest, both already required by
  the companion specs. This ADR adds no new instrumentation.
- The pattern generalises: any future declared, static, hash-recorded input
  to the guided surface is admissible under the same reasoning. Any input
  that lets the walk *adapt* is not, and would need its own ADR arguing
  against ADR-031 directly.

## Alternatives considered

- **Suppress user preferences and packs during the tutorial.** Rejected.
  It is a tutorial-only prompt branch, banned by composer invariant 2, and it
  would make the tutorial stop proving the path real users take — ADR-031's
  own stated failure mode for tutorial-only patches, which it notes are most
  tempting under schedule pressure.
- **Keep the prompt byte-frozen by withholding both features from the guided
  surface, freeform only.** Rejected: a surface-special path in the same
  family as a tutorial-special one, and it would deny the features to the
  surface where a new user most needs them.
- **Treat any tutorial red under non-clean configuration as non-signal.**
  Rejected: it discards genuine machinery reds that happen to occur on a
  configured account, and it creates an incentive to dismiss reds by pointing
  at configuration. Decision 4 classifies instead of discarding.
- **Give the tutorial a byte-fixed prompt via a pinned fixture account.**
  Rejected: it is a tutorial-only path wearing a fixture costume, and the
  pinned account's configuration would drift from any real user's.
