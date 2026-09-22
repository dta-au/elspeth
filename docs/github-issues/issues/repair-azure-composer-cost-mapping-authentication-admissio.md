---
title: Repair Azure Composer cost mapping, authentication admission and LLM response handling
labels: [area/composer, area/deployment, type/task, needs-triage]
---

A deployment on Azure reported invalid LLM cost metadata from the Composer. This task covers
reproducing and repairing the cost mapping, and reviewing the Azure Composer authentication
admission and LLM response handling alongside it.

## What happens

Composer runs on an Azure deployment recorded invalid cost metadata. LiteLLM 1.85.0 — the
release pinned when this was reported — ships a bundled pricing catalogue that does not
contain the model in use, so cost recovery for that model depends on a successful remote
catalogue fetch. A pricing catalogue only changes when the package update is in the image:
copying source files into an older image does not update the catalogue it already carries.

## Scope

`docs/diagnostics/azure-081-deployment-handoff-review.md` records the boundaries in more
detail. The pieces of behaviour this task covers:

- **Cost admission.** Missing response cost should be recovered only when both supported
  fields are absent; explicitly malformed costs and unsupported models stay fail-closed; the
  audit source is `litellm.cost_per_token`, and any calculated cost counts towards the
  planner's cumulative cap. Cache-write recovery needs an explicit valid catalogue rate for
  each reported cache duration — a missing one-hour rate must remain unavailable, because the
  pricing library's zero-cost default would otherwise understate the cap.
- **Temperature and profile ownership.** Azure source and transform configurations omit
  temperature by default, with explicit numeric values still supported. For profile-bound
  nodes the operator's profile owns temperature and authored node options must not override
  it, and profile lowering must distinguish omission from an explicit null while retaining an
  explicit zero.
- **Deadlines and transport.** The tracked Compose and systemd examples carry a 180-second
  Composer deadline and a 240-second transport ceiling, and the nginx template must forward
  HTTP/1.1 WebSocket upgrade headers with a matching proxy timeout. Turn limits remain upper
  bounds, not a guarantee that every allowed turn fits inside the wall-clock deadline.
- **Run settlement.** Normal execution recorded terminal run status and emitted terminal
  progress without closing the run saga. Settlement should require successful output
  finalisation, terminal durable status and terminal-event persistence under the live owner's
  fence, so a failed finalisation or event persistence stays recoverable instead of being
  marked complete.
- **Database failure diagnostics.** HTTP database failure diagnostics should carry a
  validated SQLSTATE, a bounded driver class, a connection-invalidated flag and explicitly
  labelled session-pool occupancy, and omit raw exception messages, SQL and parameters.

Configured model aliases stay in place while retained pipelines reference them; introducing a
new default alias does not rewrite append-only session history. No global alias substitution,
historical-state migration, credential reset or account-state change is in scope.

## Limits

The reported progress-polling `OperationalError` has no driver code in the report, so its
cause is not established. Source review found scoped database transactions, not a demonstrated
connection leak. Better diagnostics enable the remaining investigation; they do not establish
the cause and do not show that the original database incident is resolved.

HTTP readiness alone does not prove the proxy can upgrade a connection: establishing that
needs the tracked template installed on the host and the WebSocket ingress probe run against
the deployment. Local checks do not constitute live Azure acceptance — publishing an image,
installing host configuration, changing deployed profiles and validating live execution are
separate from any source-level repair.

## Note

The source for this ticket records a work programme rather than a diagnosed mechanism,
particularly for the authentication-admission half, which is scoped but not characterised.
The cost-admission, temperature-ownership and run-settlement items are the specified parts.
