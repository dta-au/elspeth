# Deployment Platforms

ELSPETH ships one container image and a small set of maintained deployment
artifacts. The image contains PostgreSQL clients, not a PostgreSQL server or
`psql`. These clients are the psycopg v3 and psycopg2 Python drivers; both
`postgresql+psycopg://` and `postgresql+psycopg2://` URLs work. The final
runtime is a pinned non-root distroless image with no package manager. Compose
provisions a PostgreSQL container; the tracked AWS ECS Terraform package
provisions Aurora PostgreSQL outside the application task. The ACA Bicep bundle
provisions Azure Database for PostgreSQL. Azure VM production and Kubernetes
BYO deployments require operator-provided external PostgreSQL.
Native Linux may instead use SQLite on one persistent host or external
PostgreSQL.

Use an immutable, release-specific image tag or digest. ELSPETH web currently
supports one web process or replica. Payload persistence is separate from
database persistence: preserve both stores across every replacement.

## Support matrix

| Profile | Database | Payload storage | Deployment entry point | Status |
| --- | --- | --- | --- | --- |
| Docker Compose | Bundled PostgreSQL sidecar, or operator external PostgreSQL; SQLite remains suitable for CLI/local work | Named `elspeth_state` volume | [Docker guide](../guides/docker.md) and [`deploy/compose`](../../deploy/compose) three-file bundle | Maintained |
| AWS ECS | Terraform-provisioned Aurora PostgreSQL for a cold install, or the existing service's external PostgreSQL | Terraform-provisioned EFS for a cold install, or the existing service's persistent filesystem | [Cold install](../runbooks/aws-ecs-cold-install.md) with [`deploy/aws-ecs/terraform`](../../deploy/aws-ecs/terraform); [existing-service redeploy](../runbooks/aws-ecs-existing-service-redeploy.md); [full disposable acceptance](../runbooks/aws-ecs-deployment.md) | Maintained disposable single-replica package |
| Azure Container Apps | Separate external Azure Database for PostgreSQL databases | Shared NFS 4.1 Azure Files share | [Bicep bundle](../../deploy/azure-container-apps/README.md), [cold install](../runbooks/azure-container-apps-cold-install.md), [redeploy](../runbooks/azure-container-apps-existing-service-redeploy.md) | Implemented; desktop acceptance for Single/sticky configuration with the limitations below |
| Azure Ubuntu VM | External Azure Database for PostgreSQL in production; SQLite only for explicitly non-production use on one persistent host | Persistent host storage | [Native Linux/Azure VM runbook](../runbooks/ansible-ubuntu-deployment.md) using [`deploy/linux-systemd/elspeth-web.service`](../../deploy/linux-systemd/elspeth-web.service) | Maintained as exactly one Azure Ubuntu VM |
| Kubernetes (BYO manifests) | External PostgreSQL | Operator-provided persistent payload storage | BYO manifests only | Runtime contract only; no maintained bundle in this release |
| Native Linux | SQLite on one single host, or external PostgreSQL | Persistent host directory | [Native Linux/Azure VM runbook](../runbooks/ansible-ubuntu-deployment.md) and [portable systemd unit](../../deploy/linux-systemd/elspeth-web.service) | Maintained |

There is no generated deployment-profile schema in this release. The tracked
deployment artifacts are the Compose and portable systemd bundles, the AWS ECS
Terraform package, the ACA Bicep bundle, and their acceptance controllers.

## Shared production contract

- Pin an immutable, release-specific image or source revision. Never deploy
  `latest`.
- Run one web process per replica (`WEB_CONCURRENCY=1`). Except for ACA's
  Single/sticky configuration below, keep one replica and stop the old process
  before starting its replacement.
- Create persistent `data`, `data/blobs`, and `payloads` paths writable by UID
  and GID 1654. Payload persistence is separate from database persistence.
- For an external database, configure distinct session and Landscape URLs with
  `ELSPETH_WEB__DEPLOYMENT_STATE_MODE=external-postgresql`.
- Grant the runtime role read access to every session table plus `INSERT` and
  `UPDATE` on `web_instances`. A read-only runtime role is no longer sufficient:
  a PostgreSQL-backed replica registers itself in that table at boot, so a role
  without those two verbs fails startup with `permission denied for table
  web_instances`. This is provisioning-time work wherever you create the role
  yourself; the AWS ECS Terraform path already grants it.
- Initialize empty external schemas once with
  `elspeth doctor deployment --init-schema`, then run the same command without
  `--init-schema` before admitting traffic. AWS keeps the compatible operator
  entry point `elspeth doctor aws-ecs --init-schema`.
- Treat `/api/health` as liveness and `/api/ready` as the traffic gate.

Web startup validates existing schemas; it does not create or repair them.

## Docker Compose

The maintained Compose bundle starts a local PostgreSQL container. It uses
three files and a repository-root `.env`; follow the
[Docker guide](../guides/docker.md) exactly. The named PostgreSQL and ELSPETH
state volumes have independent lifecycles.

## AWS ECS

For a new stack, follow the
[AWS ECS cold-install runbook](../runbooks/aws-ecs-cold-install.md). Its tracked
[Terraform package](../../deploy/aws-ecs/terraform) creates a VPC, Aurora
PostgreSQL, separate session and Landscape databases and roles, EFS/S3
storage, the ECS/Fargate service and ALB, CloudWatch/X-Ray monitoring, Bedrock
guardrails, and bounded task roles. The service remains disabled until a
schema-owner doctor and then the least-privilege runtime doctor both pass.

For an everyday image/config replacement on an existing service, follow the
[existing-service redeploy runbook](../runbooks/aws-ecs-existing-service-redeploy.md).
It discovers the current service, publishes an immutable ECR image, requires
the registry scan, clones the selected task definition narrowly, runs a
one-shot doctor, and proves the candidate task and both probes.

The exhaustive [full acceptance runbook](../runbooks/aws-ecs-deployment.md)
provisions, exercises, and destroys a disposable two-scenario release
qualification environment. It is neither the cold-install path nor the
everyday redeploy procedure. Its acceptance controller consumes explicitly
selected candidate, doctor, and previous task-definition ARNs and enforces
`minimumHealthyPercent=0`, `maximumPercent=100`, and `desiredCount=1` so
replacement is deliberately zero-overlap.

## Azure

The maintained Azure VM path is exactly one Azure Ubuntu VM using the portable
systemd bundle. Set `WEB_CONCURRENCY=1`, retain persistent host storage, and use
a true stop-before-start rollout. If Azure Front Door is present, drain or
disable the origin, stop ELSPETH, prove no process remains, deploy and validate
the replacement, then restore the origin. This causes a deliberate availability
interruption.

Azure production requires external Azure Database for PostgreSQL. Azure VM
SQLite is supported only for explicitly non-production use on one persistent
host. Back up its database with the payload store.

The `azure-container-apps` runtime contract and
[Bicep bundle](../../deploy/azure-container-apps/README.md) are implemented,
including external PostgreSQL, shared NFS storage, membership and session
fencing. The [cold-install](../runbooks/azure-container-apps-cold-install.md),
[redeploy](../runbooks/azure-container-apps-existing-service-redeploy.md) and
[acceptance](../runbooks/azure-container-apps-deployment.md) procedures are
executable operator procedures. Task `elspeth-5ec3befc1a` closed on 2026-09-10
by operator-directed desktop acceptance. No live cloud acceptance is claimed;
a sanitized live receipt may be produced by a future operator run, but is no
longer a condition of that task's closure or this documentation status.

The supported ACA operating configuration uses `Single` revision mode,
`sticky` session affinity, 2 to 4 replicas, and one web process per replica.
PostgreSQL holds both databases; shared NFS holds files, never SQLite.
Membership and session fences protect concurrent operations and dead-owner
recovery. Automatic handoff is implemented for the bounded transitions below;
integrated verification is recorded in the
[ACA plan](../plans/2026-09-10-aca-pivot-and-replica-residuals.md#final-verification). This is not
an unrestricted transparent run handoff claim.

External PostgreSQL now stores single-use WebSocket tickets and durable ordered
run events, allowing an authorized peer to consume a ticket and replay progress
after reconnect. It also stores renewable Composer request leases, bounded
progress snapshots and current inflight accounting, plus shared rate-limit
budgets for auth, writes and Composer/execution work. Adding replicas does not
multiply those shared budgets. An interrupted provider request is not
automatically resumed; visibility of saved progress does not restart its work.

Verification is limited to local PostgreSQL mechanism and integration evidence;
it is not a cloud receipt or a no-affinity deployment qualification. Keep the
Single/sticky configuration. The legacy v2 P4b acceptance receipt remains
conservative: its `owner_affine` mechanism is `cannot_pass` and does not measure
these new runtime capabilities. Receipt evolution is explicitly deferred.

### Durable run handoff

Durable admission binds a run UUID and permit to an immutable execution
envelope. It retains file, `blob_rows` and inline-content input bytes, pinned
secret versions, and admitted policy evidence so later session edits or source
file changes cannot silently change the run. Recovery rechecks runtime,
schema, protocol, source and distribution compatibility before execution.

Central plugin version, source and determinism checks apply to both CLI and
web resume. The full engine/runtime source and distribution fingerprint is
bound to the web handoff envelope. Direct CLI resume across engine or
interpreter drift without a version change remains open in
`elspeth-f321e3ff21` (closure review, comment 10110); this handoff contract does
not establish universal CLI resume identity protection.

The implemented automatic transitions cover admission before dispatch,
permit-bound `PREPARED` initialization with proof that no effects occurred, and
eligible executing runs with a durable checkpoint. A successor retains the
same run UUID, obtains fresh web and Landscape authority, and selects the
latest checkpoint after acquiring leadership. At most one active Landscape
scheduler leader may own a run; a still-live Landscape seat defers takeover.
The header becomes `EXECUTING` before plugin initialization or effects, so
pure-initialization replay applies only to a still-`PREPARED` header.
Continuous web-ownership checks fence execution after custody loss.

Authenticated peer cancellation is durable. Terminal recovery reconciles
status, counters, one terminal progress event and output artifacts across
process crashes. FAILED/INTERRUPTED reconciliation preserves the Landscape
status through fresh Landscape authority and holds its row lock through web
and output finalization to exclude concurrent CLI takeover.

Unsafe or ambiguous external effects, incomplete source ingestion, and failed
identity or compatibility checks require explicit `recovery_required`
disposition; they do not silently replay work or invent a terminal result.
Cancellation before a baseline verifies the envelope's digest and identity but
does not invoke plugins or require current secret, runtime or policy state.
Retained input objects are content-addressed and fsynced; they currently
have no automatic pruning policy. These mechanisms have local PostgreSQL
process-crash evidence and completed default/serial PostgreSQL verification.
They neither
resume an interrupted Composer provider request nor promote the frozen v3
state-engine catalog or legacy acceptance receipts.

## Kubernetes

The provider-neutral `kubernetes` runtime/config contract exists, but this
release ships no manifests. Use BYO manifests only and enforce all of these
conditions:

- one replica and one process;
- `strategy: Recreate` (stop-before-start);
- external PostgreSQL for session and Landscape state; and
- persistent payload storage writable by UID/GID 1654.

ELSPETH does not ship or claim a PVC bundle in this release. The operator owns
storage-class behavior, backups, database availability, rollout verification,
and manifest testing.
