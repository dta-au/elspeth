# Deployment Platforms

ELSPETH ships one container image and a small set of maintained deployment
artifacts. The image contains PostgreSQL clients, not a PostgreSQL server or
`psql`. These clients are the psycopg v3 and psycopg2 Python drivers; both
`postgresql+psycopg://` and `postgresql+psycopg2://` URLs work. The final
runtime is a pinned non-root distroless image with no package manager. Compose
is the only shipped bundle that provisions PostgreSQL. AWS ECS, Azure
production, and Kubernetes BYO deployments require operator-provided external
PostgreSQL. Native Linux may instead use SQLite on one persistent host or
external PostgreSQL.

Use an immutable, release-specific image tag or digest. ELSPETH web currently
supports one web process or replica. Payload persistence is separate from
database persistence: preserve both stores across every replacement.

## Support matrix

| Profile | Database | Payload storage | Deployment entry point | Status |
| --- | --- | --- | --- | --- |
| Docker Compose | Bundled PostgreSQL sidecar, or operator external PostgreSQL; SQLite remains suitable for CLI/local work | Named `elspeth_state` volume | [Docker guide](../guides/docker.md) and [`deploy/compose`](../../deploy/compose) three-file bundle | Maintained |
| AWS ECS | External Aurora PostgreSQL or PostgreSQL | EFS or another external filesystem | [Existing-service redeploy](../runbooks/aws-ecs-existing-service-redeploy.md); [full disposable acceptance](../runbooks/aws-ecs-deployment.md) | Maintained |
| Azure Ubuntu VM | External Azure Database for PostgreSQL in production; SQLite only for explicitly non-production use on one persistent host | Persistent host storage | [Native Linux/Azure VM runbook](../runbooks/ansible-ubuntu-deployment.md) using [`deploy/linux-systemd/elspeth-web.service`](../../deploy/linux-systemd/elspeth-web.service) | Maintained as exactly one Azure Ubuntu VM |
| Kubernetes (BYO manifests) | External PostgreSQL | Operator-provided persistent payload storage | BYO manifests only | Runtime contract only; no maintained bundle in this release |
| Native Linux | SQLite on one single host, or external PostgreSQL | Persistent host directory | [Native Linux/Azure VM runbook](../runbooks/ansible-ubuntu-deployment.md) and [portable systemd unit](../../deploy/linux-systemd/elspeth-web.service) | Maintained |

There is no generated deployment-profile schema in this release. The tracked
deployment artifacts are the Compose and portable systemd bundles plus the AWS
acceptance/deployment controller described by its runbook.

## Shared production contract

- Pin an immutable, release-specific image or source revision. Never deploy
  `latest`.
- Run one web process or replica (`WEB_CONCURRENCY=1`). Stop the old process
  before starting its replacement.
- Create persistent `data`, `data/blobs`, and `payloads` paths writable by UID
  and GID 1654. Payload persistence is separate from database persistence.
- For an external database, configure distinct session and Landscape URLs with
  `ELSPETH_WEB__DEPLOYMENT_STATE_MODE=external-postgresql`.
- Initialize empty external schemas once with
  `elspeth doctor deployment --init-schema`, then run the same command without
  `--init-schema` before admitting traffic. AWS keeps the compatible operator
  entry point `elspeth doctor aws-ecs --init-schema`.
- Treat `/api/health` as liveness and `/api/ready` as the traffic gate.

Web startup validates existing schemas; it does not create or repair them.

## Docker Compose

The maintained Compose bundle is the only shipped deployment that can start a
PostgreSQL server. It uses three files and a repository-root `.env`; follow the
[Docker guide](../guides/docker.md) exactly. The named PostgreSQL and ELSPETH
state volumes have independent lifecycles.

## AWS ECS

AWS uses one ECS task, external Aurora PostgreSQL/PostgreSQL, and durable EFS
paths. For an everyday image/config replacement, follow the
[existing-service redeploy runbook](../runbooks/aws-ecs-existing-service-redeploy.md).
It discovers the current service, publishes an immutable ECR image, requires
the registry scan, clones the selected task definition narrowly, runs a
one-shot doctor, and proves the candidate task and both probes.

The exhaustive [full acceptance runbook](../runbooks/aws-ecs-deployment.md)
provisions and destroys a disposable two-scenario environment using an
external Terraform package. It is not the everyday redeploy procedure. That
program's operator supplies candidate, doctor, and previous task-definition
ARNs; the controller validates them and enforces `minimumHealthyPercent=0`,
`maximumPercent=100`, and `desiredCount=1` so replacement is deliberately
zero-overlap.

## Azure

The maintained Azure path is exactly one Azure Ubuntu VM using the portable
systemd bundle. Set `WEB_CONCURRENCY=1`, retain persistent host storage, and use
a true stop-before-start rollout. If Azure Front Door is present, drain or
disable the origin, stop ELSPETH, prove no process remains, deploy and validate
the replacement, then restore the origin. This causes a deliberate availability
interruption.

Azure production requires external Azure Database for PostgreSQL. Azure VM
SQLite is supported only for explicitly non-production use on one persistent
host. Back up its database with the payload store.

The `azure-container-apps` runtime target value is reserved for a future
deployment contract. Azure Container Apps is unsupported and its bundle is
deferred until cross-instance admission and fencing lands under
`elspeth-b5d7aa5655`; a one-replica setting does not prove that platform
replacements never overlap.

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
