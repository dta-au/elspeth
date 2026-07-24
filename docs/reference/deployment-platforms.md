# Deployment Platforms

ELSPETH ships one container image and a small set of maintained deployment
artifacts. The image contains PostgreSQL clients, not a PostgreSQL server:
both `postgresql+psycopg://` (psycopg v3) and
`postgresql+psycopg2://` (psycopg2) URLs work. Compose is the only shipped
bundle that provisions PostgreSQL. Every other production topology supplies
its own external PostgreSQL service.

Use an immutable, release-specific image tag or digest. ELSPETH web currently
supports one web process or replica. Payload persistence is separate from
database persistence: preserve both stores across every replacement.

## Support matrix

| Profile | Database | Payload storage | Deployment entry point | Status |
| --- | --- | --- | --- | --- |
| Docker Compose | Bundled PostgreSQL sidecar, or operator external PostgreSQL; SQLite remains suitable for CLI/local work | Named `elspeth_state` volume | [Docker guide](../guides/docker.md) and [`deploy/compose`](../../deploy/compose) three-file bundle | Maintained |
| AWS ECS | External Aurora PostgreSQL or PostgreSQL | EFS or another external filesystem | [AWS ECS live-task clone runbook](../runbooks/aws-ecs-deployment.md) | Maintained |
| Azure Ubuntu VM | External Azure Database for PostgreSQL in production; SQLite only for an explicitly single-host installation | Persistent host storage | [Native Linux/Azure VM runbook](../runbooks/ansible-ubuntu-deployment.md) using [`deploy/linux-systemd/elspeth-web.service`](../../deploy/linux-systemd/elspeth-web.service) | Maintained as exactly one Azure Ubuntu VM |
| Kubernetes (BYO manifests) | External PostgreSQL | Operator-provided persistent payload storage | BYO manifests only | Runtime contract only; no maintained bundle in this release |
| Native Linux | SQLite on one single host, or external PostgreSQL | Persistent host directory | [Native Linux/Azure VM runbook](../runbooks/ansible-ubuntu-deployment.md) and [portable systemd unit](../../deploy/linux-systemd/elspeth-web.service) | Maintained |

There is no generated deployment-profile schema in this release. The tracked
deployment artifacts are the Compose and portable systemd bundles plus the AWS
live-task clone implementation described by its runbook.

## Shared production contract

- Pin an immutable, release-specific image or source revision. Never deploy
  `latest`.
- Run one web process or replica (`WEB_CONCURRENCY=1`). Stop the old process
  before starting its replacement.
- Create persistent `data`, `data/blobs`, and `payloads` paths writable by UID
  and GID 1000. Payload persistence is separate from database persistence.
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
paths. The [AWS runbook](../runbooks/aws-ecs-deployment.md) clones the live task
definition and enforces `minimumHealthyPercent=0`, `maximumPercent=100`, and
`desiredCount=1` so replacement is deliberately zero-overlap.

## Azure

The maintained Azure path is exactly one Azure Ubuntu VM using the portable
systemd bundle. Set `WEB_CONCURRENCY=1`, retain persistent host storage, and use
a true stop-before-start rollout. If Azure Front Door is present, drain or
disable the origin, stop ELSPETH, prove no process remains, deploy and validate
the replacement, then restore the origin. This causes a deliberate availability
interruption.

Use Azure Database for PostgreSQL for production. SQLite is permitted only
when the Azure VM is the one persistent host and its database is backed up with
the payload store.

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
- persistent payload storage writable by UID/GID 1000.

ELSPETH does not ship or claim a PVC bundle in this release. The operator owns
storage-class behavior, backups, database availability, rollout verification,
and manifest testing.
