# Azure 0.8.1 deployment handoff review

This review covers the deployment handoff dated 2026-09-21 and the repairs
intended for `release/0.8.1`. The live deployment observations came from the
operator's handoff; local source review and regression checks do not constitute
a new deployment or independent live acceptance.

## Cost accounting and the pricing catalog

Missing response cost is recovered only when both supported fields are absent.
The calculator receives the requested model and admitted usage, including cache
and service-tier details. Explicit malformed costs and unsupported models remain
fail-closed. The audit source is `litellm.cost_per_token`, and calculated cost is
included in the planner's cumulative cap.

Cache-write recovery also requires an explicit valid catalog rate for each
reported cache duration. A missing one-hour rate must remain unavailable;
the pricing library's zero-cost default would otherwise understate the cap.

LiteLLM 1.85.0's bundled catalog did not contain the reported model. The updated
lock selects 1.102.0, whose bundled catalog contains the exact OpenAI and Azure
Terra entries. This removes the reported model's dependency on a successful
remote fetch. The package update must be included in the image: copying only
ELSPETH source files into an old image does not update its pricing catalog.

## Temperature and profile ownership

Azure source and transform configs omit temperature by default. Explicit
numeric values remain supported for deployments that accept them. For
profile-bound nodes, the operator's profile owns temperature; authored node
options cannot override it. Profile lowering distinguishes omission from
explicit null and retains explicit zero.

Keep configured aliases while retained pipelines reference them. Introducing a
new default alias does not rewrite append-only session history. No global
`standard` to `terra` substitution, historical-state migration, credential
reset, or account-state change is part of this repair.

## Deployment deadlines and WebSockets

Tracked Compose and systemd examples carry a 180-second Composer deadline and
240-second transport ceiling. The nginx template forwards HTTP/1.1 WebSocket
upgrade headers and uses the matching proxy timeout. Turn limits remain upper
bounds, not a guarantee that every allowed turn fits the wall-clock deadline.
Slow provider calls can still exhaust a correctly configured deadline.

The WebSocket ingress probe exercises one-use tickets, progress delivery and
terminal replay through the configured public endpoint. HTTP readiness alone
does not prove the proxy can upgrade a connection. The tracked template must
be installed and the probe run against the deployment to establish live
acceptance; adding the template to Git does not change an existing host.

## Run settlement and database polling

Normal execution previously recorded terminal run status and emitted terminal
progress without closing the run saga. Settlement now requires successful
output finalization, terminal durable status and terminal-event persistence
under the live owner's fence. Failed finalization or event persistence remains
recoverable instead of being marked complete.

The reported progress-polling `OperationalError` has no underlying driver code
in the handoff, so its production cause is not established. Source review found
scoped database transactions, not a demonstrated connection leak. HTTP database
failure diagnostics now retain a validated SQLSTATE, bounded driver class,
connection-invalidated flag and explicitly labeled session-pool occupancy.
They omit raw exception messages, SQL and parameters. These diagnostics enable
the remaining deployment investigation; they do not prove the original
database incident is fixed.

## Release boundary

The dedicated Composer `cost_unavailable` failure vocabulary requires Sessions
schema epoch 64. The existing pre-1.0 recreation policy applies; no production
store is recreated as part of this code merge. The core cost and temperature
repairs were originally separated from that diagnostic schema change.

See [Azure troubleshooting](../runbooks/azure-llm-composer-troubleshooting.md)
for cost admission and deployment guidance. Publishing an image, installing
host configuration, changing deployed profiles, and validating live execution
are separate from merging this source repair.
