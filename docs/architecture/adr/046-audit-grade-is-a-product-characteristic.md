# ADR-046: Audit Grade Is a Product Characteristic, Not a Project One

**Date:** 2026-08-29
**Status:** Accepted
**Deciders:** ELSPETH maintainers
**Tags:** delivery-posture, tooling, process, related-adr-043

## Context

ELSPETH's thesis is that validation and audit belong inside the workflow:
fail-closed gates, honest statuses, signed allowlists, an append-only
Landscape trail, and a two-actor judge-signature seam. Those properties are
what the *product* guarantees to the people who run pipelines with it.

The project also carries a great deal of development tooling: the Filigree
issue tracker and its SQLite store, the Loomweave code map, session and git
hooks, scan caches, uv-installed helpers, and — until the tooling decision in
[ADR-043](043-project-tooling.md) — three Weft-suite tools that arrived by
installer rather than by decision. While retiring them, the same
audit-grade instincts that are correct for the product began attaching to
the tooling: weighing whether marking 79,000 orphaned scanner rows "fixed"
was an honest status, taking a backup as if it were evidence, reasoning
about a cache purge at ADR weight. None of that protects a user, a run, or an
audit trail. It is the organisational ceremony the delivery posture in
`AGENTS.md` already forbids for documents, applied to databases and caches
instead.

## Decision

**Audit grade is a characteristic of the product, not of the project's own
tooling.** The project's development tools receive ordinary engineering
hygiene and nothing more.

Concretely:

- **Product surfaces keep audit grade.** Source code, tests, runtime data,
  the Landscape audit trail, releases and exports, signed allowlists and
  judge signatures, the trust-tier and honesty gates, and the [O1] key
  custody rule are unchanged by this ADR.
- **Project tooling gets hygiene.** Purging a tracker's dead rows, deleting a
  scan store, stripping a hook, uninstalling a leaked tool, or resetting an
  index are ordinary operations: do them, use the tool's own verbs when they
  exist and direct SQL or `rm` when they do not, and report what was done.
  Do not debate status semantics for tool-internal rows, do not write an ADR
  for a cache, do not stage backups or sidecars as evidence.
- **Confirmation is not ceremony.** Genuinely destructive shared-state
  actions still get an operator's go-ahead (the existing gate on destructive
  actions stands); what changes is the weight of reasoning and record-keeping
  around them, not the need to ask.
- **Tooling decisions still get a record when they change what agents are
  told to do.** Adding or removing a tool that carries standing instructions
  in `AGENTS.md` is a project decision and amends [ADR-043](043-project-tooling.md),
  as the three retirements did — because agents act on those instructions. The state
  *inside* such a tool does not.

## Consequences

- Retirement and cleanup of tooling becomes cheap: a lane can purge, delete,
  or reset without manufacturing justification, and the operator reviews the
  outcome rather than the deliberation.
- The boundary is the artefact's audience. If a user of ELSPETH could ever
  rely on it — code, data, evidence, release — it is product. If only the
  people building ELSPETH consume it, it is tooling.
- This does not weaken any gate that protects product surfaces, and it does
  not license skipping confirmation on destructive actions; it removes the
  ceremony, not the check.
