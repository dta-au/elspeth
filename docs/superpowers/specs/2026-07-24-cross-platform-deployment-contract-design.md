# Cross-Platform Deployment Contract Design

**Date:** 2026-07-24
**Status:** Approved direction
**Release target:** After 0.7.2 unless the release owner explicitly pulls the
work into 0.7.2

## Objective

ELSPETH must ship an honest, testable deployment contract for five supported
deployment profiles:

- Docker Compose;
- AWS ECS;
- Azure Container Apps;
- Kubernetes; and
- native Linux, with or without Docker.

The application image remains an application image. It includes both supported
PostgreSQL client drivers, but it does not run a PostgreSQL server. Docker
Compose may provision a separate PostgreSQL service for a self-contained
single-host installation. AWS, Azure, and Kubernetes use externally managed
PostgreSQL. Native Linux may use SQLite for a single-host installation or an
external PostgreSQL service.

This design turns those claims into tracked configuration, runtime validation,
deployment artifacts, and CI checks. A runbook alone is not evidence of support.

## Current Gap

The image dependency problem itself is repaired on the current release branch:
the `postgres` extra and the official full image include both `psycopg2` for
bare `postgresql://` URLs and psycopg v3 for explicit
`postgresql+psycopg://` URLs. The remaining deployment contract is uneven:

- the root Compose file advertises an optional PostgreSQL service without
  wiring ELSPETH to it, and defaults to a registry/tag combination that the
  release workflow does not publish;
- runtime configuration recognizes only `default` and `aws-ecs`;
- AWS has a strict external-state validator and operational runbook;
- Azure is represented by a specification with an inlined Container Apps
  example, not a tracked deployable bundle;
- Kubernetes has probe examples but no tracked manifests; and
- the only systemd unit is a machine-specific staging unit hidden by a blanket
  `deploy/` ignore rule.

The result is that the repository can claim a platform while failing to provide
the database, storage, and runtime wiring necessary to operate it.

## Design Decision

Choose an app-only image plus explicit deployment profiles and provider
artifacts.

Rejected alternatives:

1. **Install PostgreSQL server in the ELSPETH image.** This couples unrelated
   process lifecycles, complicates upgrades and backups, creates a second
   ungoverned database deployment, and cannot serve managed container or
   multi-node environments safely.
2. **Keep the external-database requirement in prose only.** This is the current
   failure mode: image and platform configuration can drift independently from
   the docs.
3. **Ship one universal cloud manifest.** AWS, Azure, and Kubernetes have
   materially different identity, secret, network, and persistent-storage
   primitives. A universal template would hide rather than remove those
   differences.

The selected design has one provider-neutral runtime contract, one
machine-readable profile per supported platform, and a small provider-specific
artifact or runbook for the parts that genuinely differ.

## Runtime Configuration Model

`WebSettings` gains two explicit type aliases:

```python
DeploymentTarget = Literal[
    "default",
    "docker-compose",
    "linux-systemd",
    "aws-ecs",
    "azure-container-apps",
    "kubernetes",
]
DeploymentStateMode = Literal["auto", "sqlite-single", "external-postgresql"]
```

The corresponding settings are:

```python
deployment_target: DeploymentTarget = "default"
deployment_state_mode: DeploymentStateMode = "auto"
```

`auto` is a compatibility bridge, not a value used by a shipped production
profile. Resolution is deterministic:

- `aws-ecs`, `azure-container-apps`, and `kubernetes` resolve to
  `external-postgresql`;
- two explicit PostgreSQL session/Landscape URLs resolve to
  `external-postgresql`;
- local targets resolve missing URLs through the existing SQLite getters, so
  zero, one, or two explicit SQLite URLs resolve to `sqlite-single`; and
- a local PostgreSQL/SQLite mix, one explicit PostgreSQL URL, or an unsupported
  SQLAlchemy dialect fails validation rather than guessing.

Before 1.0, existing non-AWS web installations using PostgreSQL must set
`ELSPETH_WEB__DEPLOYMENT_STATE_MODE=external-postgresql`. The compatibility
`auto` value may then be deprecated without changing the explicit profiles.

### Allowed combinations

| Deployment target | `sqlite-single` | `external-postgresql` |
|---|---:|---:|
| `default` | yes | yes |
| `docker-compose` | yes | yes |
| `linux-systemd` | yes | yes |
| `aws-ecs` | no | required |
| `azure-container-apps` | no | required |
| `kubernetes` | no | required |

`sqlite-single` means one host and one web process. It is not a distributed
database mode. `external-postgresql` also remains one ELSPETH web process or
replica in this change. Multi-replica web correctness is outside this scope and
must not be implied by Kubernetes or managed-container syntax.

## External-State Contract

The provider-neutral validator checks every `external-postgresql` deployment:

- both `session_db_url` and `landscape_url` are explicitly set;
- both URLs use bare `postgresql://` or `postgresql+psycopg2://` (psycopg2),
  or `postgresql+psycopg://` (psycopg v3); other PostgreSQL drivers fail even
  though SQLAlchemy can parse their scheme;
- the two URLs select different logical databases;
- `data_dir` and `payload_store_path` are explicitly set;
- the server binds to `0.0.0.0` for container targets;
- production secret and signing keys pass the existing shape guards; and
- application startup does not lazily create database schemas.

The validator compares parsed URL components and never places raw URLs,
credentials, secrets, or paths into `ContractCheck.detail`. Invalid or
unsupported target/state combinations fail closed.

AWS validation builds on this shared contract and retains its AWS-specific OTLP
and ECS identity checks. Azure and Kubernetes add only platform-specific checks
that can be evaluated from local settings. Cloud-control-plane checks remain in
operator acceptance procedures because the application cannot prove them from
inside its process.

Landscape schema creation and readiness policy depend on the resolved state
mode, not on a hard-coded `aws-ecs` comparison. SQLite installations retain
their current lazy local initialization. External PostgreSQL installations
require the documented migration/bootstrap step before the web service starts.

## Machine-Readable Deployment Profiles

Tracked files under `deploy/platforms/` describe what the project supports:

```text
deploy/platforms/schema.json
deploy/platforms/docker-compose.yaml
deploy/platforms/linux-systemd.yaml
deploy/platforms/aws-ecs.yaml
deploy/platforms/azure-container-apps.yaml
deploy/platforms/kubernetes.yaml
```

Each profile declares:

- schema version and deployment target;
- supported state modes and the production default;
- database ownership (`compose-sidecar`, `host-or-managed`, or
  `external-managed`);
- required image extras and the platform's image-delivery channel;
- workload and maximum supported process/replica count;
- payload-storage ownership;
- tracked artifact paths; and
- the authoritative runbook.

`tests/unit/deployment/test_platform_profiles.py` validates the schema and
repository cross-links in the ordinary unit-test gate. It also enforces that:

- every advertised platform has a profile and tracked artifacts;
- every external-PostgreSQL profile requires the `postgres` image extra;
- cloud and Kubernetes profiles cannot advertise SQLite or bundled PostgreSQL;
- no production profile uses the compatibility `auto` state mode;
- every workload declares exactly one process/replica;
- image examples use the actual GHCR namespace and require an explicit release
  or commit tag rather than mutable `latest`; and
- referenced artifacts and runbooks exist.

The profiles are build/deployment authority and CI input. The web runtime does
not read repository YAML at startup; runtime settings remain the deployer's
explicit environment contract.

## Platform Bundles

### Docker Compose

The root `docker-compose.yaml` remains the low-friction CLI/SQLite path. It uses
`ghcr.io/johnm-dta/elspeth` and requires `IMAGE_TAG` instead of silently falling
back to `latest`.

Tracked overlays under `deploy/compose/` provide:

- a healthy PostgreSQL 16 service and persistent volume;
- initialization of separate `elspeth_sessions` and `elspeth_landscape`
  databases;
- a one-shot state-volume initializer that creates the data, blob, and payload
  directories with the permissions required by startup validation;
- CLI `DATABASE_URL` wiring for operators who select PostgreSQL; and
- a one-process web service with both web database URLs, persistent payload
  storage, explicit `docker-compose` target/state settings, health checks, and
  required secret environment variables.

`.env.example` documents names only. Real values remain ignored. Because the
same Compose PostgreSQL password is also interpolated into connection URLs, the
example requires a URL-unreserved 48-character hexadecimal value generated by
`openssl rand -hex 24`; it does not imply that arbitrary free-form passwords
can be embedded without encoding. The Compose database is a
development/single-host convenience, not the production topology for AWS,
Azure, or Kubernetes.

### AWS ECS

AWS continues to use the existing clone-the-live-task-definition workflow and
strict acceptance controller. The repository does not add a static full task
definition, because doing so would create a second source of truth for task
roles, network policy, secret ARNs, and deployed revisions.

The AWS profile records the required `webui`, `llm`, `aws`, and `postgres`
extras, external Aurora/PostgreSQL ownership, persistent payload storage, one
task, and the existing runbook/acceptance commands.

### Azure Container Apps

`deploy/azure-container-apps/main.bicep` becomes the tracked workload module
extracted from the current Ubuntu/Azure specification. It declares:

- single-revision mode and one active replica;
- the official image with `webui`, `azure`, `llm`, and `postgres` extras;
- secret references for both PostgreSQL URLs and application keys;
- explicit `azure-container-apps` and `external-postgresql` settings;
- an NFS Azure Files mount for payload/data persistence;
- a required operator-prepared NFS subdirectory whose data, blob, and payload
  paths are owned by UID/GID 1654 with restrictive permissions; and
- liveness and readiness probes matching the web service.

The Azure runbook remains responsible for resource provisioning, managed
identity, Key Vault, database bootstrap, and substituting release-specific
image/storage values. Its current multiple-revision traffic-shift procedure is
not compatible with ELSPETH's one-replica contract. It is replaced by a
zero-overlap update: drain/deactivate the old revision, prove zero running
replicas, deploy and validate the new revision, then restore traffic. The
availability interruption is explicit; the plan does not trade data correctness
for a blue/green claim the application cannot yet support.

### Kubernetes

`deploy/kubernetes/base/` provides a Kustomize base containing a Deployment,
Service, ConfigMap, persistent-volume claim, and a non-secret example of the
required Secret keys. It fixes replicas and the uvicorn worker count to one,
uses the zero-overlap `Recreate` strategy, initializes restrictive state
subdirectories on the PVC, uses explicit external PostgreSQL settings, mounts
persistent payload/data storage, and exposes the existing liveness/readiness
endpoints.

The base intentionally does not install PostgreSQL. Operators provide managed
PostgreSQL and a storage class appropriate to their cluster. No ingress or
cloud-specific identity controller is selected at this layer.

### Native Linux

`deploy/linux-systemd/` provides a portable systemd unit and environment
example using `/opt/elspeth`, `/etc/elspeth`, and `/var/lib/elspeth`. It supports
the explicit `sqlite-single` and `external-postgresql` state modes and runs one
web process.

The existing `deploy/elspeth-web.service` is staging-specific. It remains in
place until the portable unit has a tested migration path; the implementation
must not silently replace a live machine configuration.

## Ignore and Secret Policy

The blanket `deploy/` ignore rule is removed so support artifacts are visible to
Git and CI. Narrow rules continue to ignore:

- real environment files while retaining `*.env.example`;
- local deployment overrides;
- generated backup files; and
- the machine-local Caddy configuration already used by staging.

No real API key, database password, cloud secret, connection URL, or secret ARN
is committed. Tests inject inert values or blank sensitive variables. Diagnostic
output identifies missing variable names without echoing values.

## Verification Strategy

The change is accepted only when all five deployment profiles pass the same
static contract and their provider artifacts pass focused tests:

- JSON Schema and cross-file profile validation;
- Pydantic target/state resolution and redaction tests;
- shared external-PostgreSQL validator tests;
- startup, readiness, schema-policy, and doctor regressions;
- `docker compose config` for the base and every overlay;
- a Docker-backed Compose smoke test that initializes both logical databases
  and reaches web readiness;
- systemd unit verification with `systemd-analyze verify`;
- YAML/schema assertions for Azure Container Apps and Kubernetes;
- current AWS unit/testcontainer acceptance suites;
- image tests proving both PostgreSQL drivers are installed; and
- repository lint, type, unit, and focused testcontainer gates.

Provider-side creation of AWS, Azure, or Kubernetes resources is an explicit
operator acceptance lane, not an implicit CI side effect. The static bundles
must still fail locally when required wiring, storage, immutable image identity,
or external database declarations are absent.

## Non-Goals

- Running PostgreSQL inside the ELSPETH application container.
- Claiming multi-replica or distributed web support.
- Provisioning Aurora, Azure Database for PostgreSQL, or a Kubernetes database
  operator.
- Choosing ingress, DNS, TLS, cloud network, or backup policy for an operator.
- Replacing AWS's live-task clone and acceptance workflow with a static task
  definition.
- Publishing or exercising real operator credentials from CI.
- Reworking CLI pipeline database semantics beyond correctly wiring the
  supported Compose and Linux installation paths.
