# Phase 6b — Azure Container Apps platform facts (spike 6b-0)

Ticket `elspeth-f966f9dc2b` (6b-0), plan of record
`2026-09-04-release-0.8.0-phase6b-azure-container-apps-plan.md` §16. Measured
2026-09-05 on branch `p6b/0` at `02d10e0c1` from a host with **no Azure
subscription, no `az`, no `psql`** (Docker 29.7.2, `jq`, `curl`, buildx v0.36.1
present). Everything obtainable without a subscription is measured here; the
residue that needs one is enumerated in §10 and carried to the 6b-7 live run.

Every fact carries a provenance tag:

- `[local]` — executed on this host; the command is in the appendix.
- `[tree]` — read from the landed tree at `02d10e0c1` (`path:line`).
- `[doc]` — Microsoft Learn / man page / PostgreSQL documentation, cited by
  URL with the page's `ms.date` where it has one. Quoted text is verbatim.
- `[LIVE]` — cannot be established without a subscription; a claim, not a fact,
  until the first live run records it.

Downstream consumers cite this file **verbatim by section number**; the bundle
test (6b-1) and runbook-contract test (6b-6) pin the literals in §1, §2.5 and
§8, never the prose.

---

## 0. Corrections to the plan found by measurement

These supersede the corresponding plan text; the plan is not re-edited here.

| # | plan says | measured | consequence |
|---|---|---|---|
| C1 | epoch literals `51`/`36` (§8.3, §9.1) | `SESSION_SCHEMA_EPOCH = 52` (`src/elspeth/web/sessions/models.py:299`), `SQLITE_SCHEMA_EPOCH = 37` (`src/elspeth/core/landscape/schema.py:364`); the ECS runbook literal already reads `52`/`37` (`docs/runbooks/aws-ecs-deployment.md:1518`) `[tree]` | runbook-contract test byte-binds **52/37**; the lane brief was right, the plan text is stale |
| C2 | "add an ACR-digest == GHCR-digest assertion when both pushed; no change to the ACR push" (§8.3) | the two registries are pushed by **two independent `docker/build-push-action` builds** (`.github/workflows/build-push.yaml:253-288`, each `provenance: true`, `sbom: true`). Two independent builds of byte-identical inputs produce **different** index digests (attestation manifest differs); a registry-to-registry `docker buildx imagetools create` copy **preserves** the digest; one build pushing both tags yields **equal** digests `[local]` §6 | the assertion is only truthful if ACR publication becomes a digest-preserving copy of the GHCR image (or a second tag on the one build). 6b-1 must change the ACR push step, not only add an assertion; the existing smoke step comment at `:359` ("both independently-pushed digests") documents the current two-digest reality |
| C3 | production `stickySessions.affinity: sticky`, acceptance disables it (§3.5) | "Session affinity is only supported when your app is in single revision mode and the ingress type is HTTP" `[doc]` §2.3 | confirmed and sharpened: the acceptance's `Multiple` mode **cannot** carry `sticky`; the bundle test pins `sticky` in the production parameter set and `none` in the acceptance set |
| C4 | transport-ceiling parameter "required, no default" (§3.5) | ingress "Request time out is 240 seconds", no ingress property sets it `[doc]` §2.2; the config default is `300.0` (`src/elspeth/web/config.py:69`) with headroom `30.0` `[tree]` | the parameter is bound to **≤ 240** on this platform; the bundle test additionally asserts the example parameter file's value is `≤ 240` (the default 300 would make the wall-clock guard vacuous, the exact failure `config.py:47-66` describes) |
| C5 | startup probe "with a failure budget covering ECS's 150 s startPeriod" (§3.5) | `initialDelaySeconds` max **60**, `periodSeconds` max 240, `failureThreshold` max **10** `[doc]` §2.4; Bicep enforces the same ranges at compile time via the AVM `containerAppProbeType` `[local]` | 150 s = `periodSeconds: 15 × failureThreshold: 10`; a single probe cannot exceed 2400 s; the portal's default startup probe (`failureThreshold: 240`) is outside the documented API range and is not copied |
| C6 | `provision-storage` Job pre-creates dirs UID/GID 1654 (§3.1, §4) | the runtime image ends `USER elspeth` (UID 1654, `Dockerfile:125,185`); Container Apps exposes no `runAsUser`/`securityContext` and forbids privileged containers `[doc]` §2.8; NFS share root ownership after creation is `[LIVE]` | the `provision-storage` Job runs a **root** image (a digest-pinned distroless/busybox or the `:debug` base), not the elspeth image, with `rootSquash: NoRootSquash` on the share; the runtime containers never need root |
| C7 | P3 primary primitive: admin runs `ALTER ROLE … NOLOGIN` + `pg_terminate_backend` (§7) | the Flexible Server admin is `NOSUPERUSER`, member of `azure_pg_admin`, and **cannot grant `pg_signal_backend`** on PG16+ (Microsoft Q&A threads) `[doc]` §4.1; `pg_terminate_backend` is allowed when "the calling role is a member of the role whose backend is being terminated" `[doc]` | the deterministic sequence is **self-termination**: open a session *as* `elspeth_runtime_a`, then `ALTER ROLE elspeth_runtime_a NOLOGIN` as admin (existing sessions survive `NOLOGIN`), then from the runtime_a session terminate every other backend of `current_user`, then close it. No `pg_signal_backend`, no admin membership grant needed. §4.2 |
| C8 | Key Vault "purge or tombstone recorded" at cleanup (§5, §10) | AVM `key-vault/vault:0.14.0` defaults `enablePurgeProtection: true`; purge protection cannot be turned off once on `[local]` | the acceptance parameter set passes `enablePurgeProtection: false` (soft delete stays on; a vault with purge protection can only be tombstoned for 90 days) |
| C9 | Flexible Server "role password in Key Vault" (§3.2) | AVM `flexible-server:0.16.0` defaults `authConfig: {activeDirectoryAuth: Enabled, passwordAuth: Disabled}` and `version: 18` `[local]` | the bundle overrides `authConfig` to `passwordAuth: Enabled` (Entra excluded, D4) and pins `version` explicitly (§4.5) |
| C10 | KQL over `ContainerAppConsoleLogs_CL` / `ContainerAppSystemLogs_CL` (§3.4) | documented columns `[doc]` §5.1; with the `azure-monitor` destination the tables are the resource-specific `ContainerAppConsoleLogs` / `ContainerAppSystemLogs`, and an ingress-side `ContainerAppHTTPLogs` table with `ReplicaName`, `RevisionName`, `StatusCode` exists `[doc]` §5.2 | the bundle keeps the default `log-analytics` destination (the `_CL` shapes); §5.2 records the HTTP-log alternative as control-plane 409 evidence for P1 to be verified live |
| C11 | ACR module in the bundle (§3.1) | `build-push.yaml` pushes to an **existing** registry named by `secrets.ACR_REGISTRY` (`:188,221`) `[tree]` | the bundle references the existing registry by resource id and assigns `AcrPull` there; it creates no registry (§8) |

---

## 1. Toolchain pins `[local]`

### 1.1 Bicep CLI

| item | value |
|---|---|
| version | `0.46.1` (`bicep --version` → `Bicep CLI version 0.46.1 (545b338e2c)`) |
| asset | `https://github.com/Azure/bicep/releases/download/v0.46.1/bicep-linux-x64` (104,933,840 bytes) |
| sha256 | `3e011d629ea4311b7a7dd8f0040ab2b1a072ea4ff5d02cb75e0e55a9a6703fb9` |
| release metadata | `https://api.github.com/repos/Azure/bicep/releases/latest` → `tag_name: v0.46.1` on 2026-09-05; the release publishes no checksum asset, so the sha256 above is this spike's own measurement of the downloaded bytes — CI pins it exactly as the Terraform step pins `TF_SHA256` (`.github/workflows/ci.yaml:689-704`) |
| module cache | `bicepconfig.json` → `cacheRootDirectory` is honoured; `bicep restore` resolves every module in §1.2 from `mcr.microsoft.com` without credentials (12 `main.json` files restored, exit 0, 9.5 s) |

### 1.2 Azure Verified Modules — pinned tags

Tags are the newest on `mcr.microsoft.com/v2/bicep/avm/res/<path>/tags/list` on
2026-09-05. Every module restored and compiled (the only `bicep build` errors on
the probe file were the expected missing-required-parameter errors, proving
resolution and schema validation).

| resource | module | tag | ARM type@apiVersion inside the module |
|---|---|---|---|
| managed environment | `br/public:avm/res/app/managed-environment` | `0.16.0` | `Microsoft.App/managedEnvironments@2026-01-01` |
| container app | `br/public:avm/res/app/container-app` | `0.23.0` | `Microsoft.App/containerApps@2026-01-01` |
| job | `br/public:avm/res/app/job` | `0.7.2` | `Microsoft.App/jobs@2026-01-01` |
| Key Vault | `br/public:avm/res/key-vault/vault` | `0.14.0` | `Microsoft.KeyVault/vaults@2024-11-01` |
| PostgreSQL Flexible Server | `br/public:avm/res/db-for-postgre-sql/flexible-server` | `0.16.0` | `Microsoft.DBforPostgreSQL/flexibleServers@2026-01-01-preview` |
| storage account (+ NFS share) | `br/public:avm/res/storage/storage-account` | `0.33.0` | `Microsoft.Storage/storageAccounts/fileServices/shares` (`rootSquash`, `enabledProtocols` exposed) |
| Log Analytics | `br/public:avm/res/operational-insights/workspace` | `0.16.1` | `Microsoft.OperationalInsights/workspaces@2025-07-01` |
| user-assigned identity | `br/public:avm/res/managed-identity/user-assigned-identity` | `0.6.0` | `Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30` |
| virtual network | `br/public:avm/res/network/virtual-network` | `0.10.2` | `Microsoft.Network/virtualNetworks@2025-05-01` |
| private endpoint | `br/public:avm/res/network/private-endpoint` | `0.12.1` | `Microsoft.Network/privateEndpoints@2025-05-01` |
| private DNS zone | `br/public:avm/res/network/private-dns-zone` | `0.8.1` | `Microsoft.Network/privateDnsZones@2020-06-01` |
| container registry | `br/public:avm/res/container-registry/registry` | `0.13.0` | not used by the bundle (C11); pinned for a cold-install that must create one |

### 1.3 AVM parameter surfaces the bundle binds (from the restored `main.json`)

- `container-app:0.23.0` — `activeRevisionsMode` (`Single`\|`Multiple`, default `Single`), `stickySessionsAffinity` (`none`\|`sticky`, default `none`), `ingressExternal`, `ingressTargetPort` (default 80), `ingressTransport` (`auto`\|`http`\|`http2`\|`tcp`), `traffic` (array, resource-derived: `revisionName`/`label`/`weight`/`latestRevision`), `scaleSettings` (`{minReplicas, maxReplicas, rules?}`, **default `{minReplicas: 3, maxReplicas: 10}` — must be overridden**), `terminationGracePeriodSeconds` (int), `secrets` (`{name, keyVaultUrl, identity}` or `{name, value}`), `registries` (`{server, identity}`), `managedIdentities.userAssignedResourceIds`, `containers` (resource-derived; `probes[]` validated by `containerAppProbeType`: `initialDelaySeconds` 1–60, `periodSeconds` 1–240, `timeoutSeconds` 1–240, `failureThreshold` 1–10, `successThreshold` 1–10), `volumes`, `revisionSuffix`, `workloadProfileName`, `maxInactiveRevisions`.
- `job:0.7.2` — `triggerType` (`Manual`), `manualTriggerConfig` (`{parallelism, replicaCompletionCount}`), `replicaTimeout` (default 1800), `replicaRetryLimit` (default 0), `containers`, `secrets`, `registries`, `managedIdentities`, **`volumes`** (jobs mount the same environment storage as apps).
- `managed-environment:0.16.0` — `appLogsConfiguration: {destination: 'log-analytics', logAnalyticsWorkspaceResourceId}` (the module calls `listKeys` for `sharedKey` itself), `infrastructureSubnetResourceId`, `internal` (default false), `workloadProfiles`, `zoneRedundant` (default **true** — acceptance sets false), `publicNetworkAccess` (default `Disabled`), `storages[]: {name, kind: 'NFS'|'SMB', storageAccountName, accessMode}` — for `kind: 'NFS'` the module emits `nfsAzureFile: {server: '<account>.file.<suffix>', shareName: '/<account>/<share>', accessMode}` and **does not call `listKeys`** (only the SMB arm does), so `allowSharedKeyAccess` can be `false` on the storage account.
- `flexible-server:0.16.0` — `version` (allowed `11`–`18`, **default `18`**), `tier`/`skuName` (required), `availabilityZone` (required, `-1` = none), `highAvailability` (default `ZoneRedundant`), `geoRedundantBackup` (default `Enabled`), `backupRetentionDays` (7), `authConfig` (**default `{activeDirectoryAuth: Enabled, passwordAuth: Disabled}`**), `administratorLogin`/`administratorLoginPassword` (securestring), `databases[]: {name, charset?, collation?}`, `publicNetworkAccess` (default `Disabled`), `firewallRules[]`, `privateEndpoints[]`, `privateDnsZoneArmResourceId`, `configurations[]` (server parameters), `serverThreatProtection` (default `Enabled`).
- `storage-account:0.33.0` — `kind` (must be `FileStorage`), `skuName` (`Premium_LRS`), `supportsHttpsTrafficOnly` (default true — see §3.2), `publicNetworkAccess`, `networkAcls`, `privateEndpoints[]`, `allowSharedKeyAccess`, `fileServices.shares[]: {name, enabledProtocols: 'NFS', rootSquash: 'NoRootSquash'|'RootSquash'|'AllSquash', shareQuota, accessTier}`.
- `key-vault/vault:0.14.0` — `enableRbacAuthorization` (default true), `enableSoftDelete` (true), `softDeleteRetentionInDays` (90), `enablePurgeProtection` (**default true**, C8), `sku` (default `premium`), `secrets[]: {name, value, contentType?, attributes?}`, `roleAssignments[]`, `privateEndpoints[]`, `publicNetworkAccess`.
- `operational-insights/workspace:0.16.1` — `dataRetention` (default 365), `skuName` (`PerGB2018`), `dailyQuotaGb`.
- `private-endpoint:0.12.1` — `subnetResourceId`, `privateLinkServiceConnections[]`, `privateDnsZoneGroup`; `private-dns-zone:0.8.1` — `virtualNetworkLinks[]`.

---

## 2. Container Apps platform facts

### 2.1 Built-in environment variables `[doc]`

Source: `https://learn.microsoft.com/en-us/azure/container-apps/environment-variables` (ms.date 2026-03-31).

| variable | meaning | example |
|---|---|---|
| `CONTAINER_APP_NAME` | app name | `my-containerapp` |
| `CONTAINER_APP_REVISION` | revision name | `my-containerapp--20mh1s9` |
| `CONTAINER_APP_HOSTNAME` | revision-specific hostname | `my-containerapp--20mh1s9.<DEFAULT_HOSTNAME>.<REGION>.azurecontainerapps.io` |
| `CONTAINER_APP_ENV_DNS_SUFFIX` | environment DNS suffix; app FQDN = `$CONTAINER_APP_NAME.$CONTAINER_APP_ENV_DNS_SUFFIX` | |
| `CONTAINER_APP_PORT` | target port | `8080` |
| `CONTAINER_APP_REPLICA_NAME` | replica name | `my-containerapp--20mh1s9-86c8c4b497-zx9bq` |
| `CONTAINER_APP_JOB_NAME`, `CONTAINER_APP_JOB_EXECUTION_NAME` | jobs only | `my-job`, `my-job-iwpi4il` |
| `IDENTITY_ENDPOINT`, `IDENTITY_HEADER` | managed-identity token endpoint (`GET ${IDENTITY_ENDPOINT}?resource=…&api-version=2019-08-01`, header `X-IDENTITY-HEADER`; user-assigned via `client_id` / `mi_res_id` / `principal_id` query, one of which is **required** for a user-assigned identity) | `https://learn.microsoft.com/en-us/azure/container-apps/managed-identity` (2025-06-03) |

For 6b-3: replica identity = `CONTAINER_APP_REPLICA_NAME`, deployment
generation = `CONTAINER_APP_REVISION`. The replica-binding subject in §5 of the
plan is `sha256("<container app ARM id>/revisions/" + CONTAINER_APP_REVISION + "/replicas/" + CONTAINER_APP_REPLICA_NAME)`.
There is no metadata endpoint; the replica name is cross-checked against
`az containerapp replica list` by the driver (plan §5 compensating control).

### 2.2 Ingress `[doc]`

Source: `https://learn.microsoft.com/en-us/azure/container-apps/ingress-overview` (ms.date 2025-05-02).

- "Request time out is 240 seconds" for HTTP ingress; TLS 1.2/1.3 terminated at
  ingress; HTTP/1.1, HTTP/2, WebSocket, gRPC supported; port 80 redirects to 443.
  No ingress property changes the 240 s; it is the transport-ceiling hop (C4).
  WebSocket idle behaviour at 240 s is `[LIVE]`.
- Headers added: `X-Forwarded-Proto`, `X-Forwarded-For` (only the rightmost IP
  is Azure-provided), `X-Forwarded-Client-Cert` when `clientCertificateMode` is set.
- Ingress properties named by the platform: `external`, `targetPort`,
  `transport`, `allowInsecure`, `traffic[]`, `stickySessions.affinity`,
  `clientCertificateMode`, `corsPolicy`, `ipSecurityRestrictions`,
  `additionalPortMappings` (max five; "Only the main ingress port supports
  built-in HTTP features such as CORS and session affinity").
- "External TCP ingress is only supported for Container Apps environments that
  use a virtual network" — irrelevant to HTTP ingress; recorded for completeness.
- Front Door is **not** in the bundle. If one is placed in front, the ceiling
  becomes `min(240, <Front Door origin response timeout>)` and the operator
  re-derives the parameter; `config.py:47-66` names the CDN-in-front case as the
  hop most likely to be missed.

### 2.3 Revisions, labels, traffic, session affinity `[doc]`

Sources: `https://learn.microsoft.com/en-us/azure/container-apps/revisions` (2025-10-27),
`…/traffic-splitting` (2025-06-16), `…/sticky-sessions` (2025-05-29),
`…/blue-green-deployment` (2025-11-06).

- Modes: `Single` (default; "The existing active revision isn't deactivated
  until the new revision is ready … A new revision is considered ready when: the
  revision has provisioned successfully; the revision has scaled up to match the
  previous revisions replica count; all the replicas have passed their startup
  and readiness probes") and `Multiple` ("you can activate or deactivate
  revisions as needed"). Up to 100 inactive revisions are retained.
- Revision name = `<CONTAINER_APP_NAME>--<REVISION_SUFFIX>` (double dash);
  suffix: lower-case alphanumerics and dashes, starts alphabetic, ≤ 64 chars,
  no `--`.
- Labels: "A label provides a unique URL"; "A label can be applied to only one
  revision at a time"; "Allocation for traffic splitting isn't required for
  revisions with labels"; "Labels work independently of traffic splitting".
  Label URL = `https://<APP_NAME>---<LABEL>.<ENV_DEFAULT_DOMAIN>` (**triple**
  dash; the blue-green page: `curl -s https://$APP_NAME---blue.$APP_DOMAIN/…`,
  `APP_DOMAIN` from `az containerapp env show … --query properties.defaultDomain`).
  A revision with `weight: 0` and a label stays active and reachable on its label
  URL (the traffic-splitting "Staging microservices" example and the blue-green
  page's `weight: 0, label: green`).
- Traffic: weights must sum to 100; entries are `{revisionName | label | latestRevision, weight}`;
  CLI `az containerapp ingress traffic set --revision-weight r1=50 r2=50` or
  `--label-weight a=50 b=50`; labels via `az containerapp revision label add --label <l> --revision <r>`.
  Traffic rules, labels and revision mode are **application-scope** changes (no
  new revision); anything under `properties.template` is revision-scope.
- Session affinity: "Session stickiness is enforced using HTTP cookies. This
  feature is available in single revision mode when HTTP ingress is enabled."
  "Session affinity is only supported when your app is in single revision mode
  and the ingress type is HTTP." Property: `ingress.stickySessions.affinity: "sticky"`.
  Consequence C3.
- Replica-list evidence: `az containerapp replica list -n <app> -g <rg> --revision <rev>` `[LIVE]`
  (command exists in the `containerapp` extension; its exact output shape is
  recorded at the first live run).

### 2.4 Health probes `[doc]`

Sources: `https://learn.microsoft.com/en-us/azure/container-apps/health-probes` (2025-11-06);
ranges from the REST spec `Microsoft.App/ContainerApps/stable/2025-07-01/CommonDefinitions.json`
and enforced by the AVM `containerAppProbeType` `[local]`.

| field | range / default |
|---|---|
| `type` | `Liveness` \| `Readiness` \| `Startup`; one of each per container |
| `httpGet.path/port`, `tcpSocket.port` | port 1–65535, integer (named ports unsupported); `exec` probes unsupported; gRPC unsupported |
| `initialDelaySeconds` | min 1, **max 60** |
| `periodSeconds` | default 10, min 1, max 240 |
| `timeoutSeconds` | default 1, min 1, max 240 |
| `failureThreshold` | default 3, min 1, **max 10** |
| `successThreshold` | default 1, min 1, max 10; must be 1 for liveness and startup |
| success | HTTP status `>= 200` and `< 400` |

Platform defaults when a probe type is omitted (portal): startup TCP on the
target port, period 1 s, failure threshold 240 (outside the documented API max;
not copied); readiness TCP period 5 s, failure 48. "In single revision mode,
traffic shifts automatically once the readiness probe returns a successful
state." "A revision state appears as unhealthy if any of its replicas fails its
readiness probe check."

Bundle binding (C5): startup `httpGet /api/health` `periodSeconds: 15`,
`failureThreshold: 10`, `timeoutSeconds: 5` (150 s budget, the ECS
`startPeriod`); liveness `httpGet /api/health` period 30 / failure 3; readiness
`httpGet /api/ready` period 10 / failure 3. Routes: `/api/health` (`app.py:1985`),
`/api/ready` (`:1995`), `/api/system/status` (`:2018`) `[tree]`.

### 2.5 Termination `[doc]`

- `template.terminationGracePeriodSeconds`: "Optional duration in seconds the
  Container App Instance needs to terminate gracefully. Value must be
  non-negative integer… Defaults to 30 seconds" (REST spec 2025-07-01). The
  underlying Kubernetes semantics (`PodSpec.terminationGracePeriodSeconds`,
  `https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/pod-v1/`):
  "Value must be non-negative integer. The value zero indicates stop immediately
  via the kill signal (no opportunity to shut down)." So a grace-0
  `az containerapp revision deactivate` is a SIGKILL with no lifespan run —
  the plan's secondary P3 primitive; whether a `stopped` write can still land
  before the kill is `[LIVE]` (the receipt downgrades to `graceful_stop` if it
  does, plan §7).
- Scale-in and `Single`-mode deactivation deliver SIGTERM first and wait up to
  the grace period; the bundle sets **`terminationGracePeriodSeconds: 60`** in
  production (drain: mark draining → readiness fails → stop heartbeat → record
  `stopped`, plan §3.5) and the acceptance's `rB`-style revisions inherit it.

### 2.6 Jobs `[doc]`

Source: `https://learn.microsoft.com/en-us/azure/container-apps/jobs` (2026-03-31).

- `triggerType: Manual`, `manualTriggerConfig: {parallelism: 1, replicaCompletionCount: 1}`,
  `replicaTimeout` (seconds; default 1800 in the AVM), `replicaRetryLimit: 0`
  ("To fail a replica without retrying, set the value to `0`").
- Start: `az containerapp job start --name <job> --resource-group <rg>`;
  optionally `--yaml <template.yaml>` to override the **entire** template for
  one execution ("the job's entire template configuration is replaced").
- Status: `az containerapp job execution list --name <job> --resource-group <rg>`
  (history capped at 100 for scheduled/event jobs); detailed output comes from
  the environment's log destination.
- Jobs share the environment's network, log destination, secrets model,
  managed identity and (per the AVM `volumes` parameter) storage mounts; Dapr
  and ingress are unsupported on jobs. Starting a job needs
  `microsoft.app/jobs/start/action` (Container Apps Contributor).
- "When a job pod starts, sidecar containers (such as the Envoy proxy) are
  guaranteed to be ready before the main job container begins execution."

### 2.7 Secrets and Key Vault references `[doc]`

Source: `https://learn.microsoft.com/en-us/azure/container-apps/manage-secrets` (2026-03-31).

- Shape: `configuration.secrets[] = {name, keyVaultUrl, identity}` where
  `identity` is `"system"` or the user-assigned identity **resource id**; the
  identity needs the **Key Vault Secrets User** RBAC role. Env reference:
  `env[] = {name, secretRef}`. Secret names: lower-case alphanumerics and hyphens.
- URL forms: versioned `https://<vault>.vault.azure.net/secrets/<name>/<32-hex>`
  or unversioned `…/secrets/<name>`. Unversioned: "the app automatically
  retrieves the latest version within 30 minutes. Any active revisions that
  reference the secret in an environment variable is automatically restarted to
  pick up the new value." Versioned: "For full control of which version of a
  secret is used, specify the version in the URI."
- Secrets are application-scope ("New revisions don't get generated through
  adding, removing, or changing secrets"); "An updated or deleted secret doesn't
  automatically affect existing revisions".
- Bundle binding: **versioned** URLs (rotation = new version + new revision,
  plan §3.3; receipts record secret names and versions); the two runtime roles
  are two Key Vault secrets `elspeth-session-db-url-runtime-a` / `-b` plus the
  schema-owner URLs for the schema-init Job.

### 2.8 Containers, identity, registry pull `[doc]`

Sources: `…/containers` (2025-05-15), `…/managed-identity` (2025-06-03).

- "Any Linux-based x86-64 (`linux/amd64`) container image"; "Azure Container
  Apps doesn't allow privileged containers mode with host-level access"; no
  `securityContext`/`runAsUser` field exists — the image's `USER` is the runtime
  user. Consequence C6.
- `command` = Docker entrypoint override, `args` = arguments.
- Consumption CPU/memory pairs: `0.25/0.5Gi`, `0.5/1.0Gi`, `1.0/2.0Gi`, `2.0/4.0Gi`
  … `4.0/8.0Gi` (Consumption-only environments cap at 2 cores / 4 Gi).
- Registry pull by identity: `configuration.registries[] = {server, identity: <uami resource id>}`,
  identity holds `AcrPull` on the registry; "Don't configure a username and
  password when using managed identity". Image reference by digest:
  `<acr>.azurecr.io/<repo>@sha256:<digest>`.
- "The back-end services for managed identities maintain a cache per resource
  URI for around 24 hours" — a role assignment made after the first token
  request may not take effect for up to 24 h; the bundle assigns roles **before**
  the app/job is created (Bicep `dependsOn` ordering).
- `identitySettings[].lifecycle` (`Init`|`Main`|`All`|`None`) exists from
  `2024-02-02-preview`; not used.
- Init containers "can't access managed identity at run time" in Consumption-only
  and Dedicated environments — the `provision-storage` step is a **Job**, not an
  init container.

### 2.9 Managed OpenTelemetry agent — status `[doc]`

`https://learn.microsoft.com/en-us/azure/container-apps/opentelemetry-agents`
(ms.date 2026-03-31) is still described as "(preview)" in its page description;
destinations App Insights (no metrics), Datadog, OTLP; single replica, gRPC
only, "Secrets (such as API keys) must be specified directly in templates".
Out of scope (plan §2, D4); recorded as the post-0.8.0 `azure-otlp` input.

---

## 3. Storage: NFS 4.1 Azure Files

### 3.1 Azure Files NFS facts `[doc]`

Sources: `https://learn.microsoft.com/en-us/azure/storage/files/files-nfs-protocol` (2026-07-08),
`…/nfs-root-squash` (2026-06-17), `…/storage-files-how-to-mount-nfs-shares` (2026-06-17),
`https://learn.microsoft.com/en-us/troubleshoot/azure/azure-storage/files/security/files-troubleshoot-linux-nfs` (2026-06-03).

- "NFS Azure file shares offer a fully POSIX-compliant file system. Hard links
  and symbolic links are supported"; NFSv4.1 only; "delegations and callback of
  all kinds, Kerberos authentication, and ACLs aren't supported"; 16-GID limit
  per connection; LRS/ZRS only; SSD (Premium) media only — "NFS is only
  available on storage accounts with the following configuration: Tier:
  Premium, Account Kind: FileStorage".
- Two providers: classic `Microsoft.Storage` shares and the newer
  `Microsoft.FileShares`; **Container Apps supports only the classic
  `Microsoft.Storage/storageAccounts/fileServices/shares` type** (§3.2).
- Network: "you must set up either a private endpoint or a service endpoint";
  IP allow-lists are ignored for NFS ("The storage account firewall ignores IPs
  added to the allowlist"); private DNS zone `privatelink.file.core.windows.net`;
  port 2049.
- Root squash: property `rootSquash` on the share with values `NoRootSquash`
  (**default when creating a share**), `RootSquash` (UID/GID 0 → 65534),
  `AllSquash` (all → 65534); "root squash is the default behavior in NFS, it's
  not the default option when you create an NFS Azure file share". CLI:
  `az storage share-rm update --root-squash NoRootSquash|RootSquash|AllSquash`.
  "The client OS enforces permissions for NFS file shares, not the Azure Files
  service."
- Recommended mount options (mount how-to table): `vers=4,minorversion=1,sec=sys`
  (required), `rsize=1048576,wsize=1048576`, `noresvport` (kernels < 5.18),
  `actimeo` 30–60 ("Using a value lower than 30 seconds can cause performance
  degradation"), `nconnect=4`.
- Encryption in transit exists but needs the AZNFS mount helper; a plain NFS
  client mount fails when "Secure transfer required" or "Require encryption in
  transit for NFS" is on. Container Apps cannot use it (§3.2).
- Share-root ownership/mode after creation: not documented on any page read;
  `[LIVE]` (the `provision-storage` Job is written to succeed either way: it
  runs as root with `NoRootSquash`, `mkdir -p` + `chown 1654:1654` + `chmod 0700`).

### 3.2 Container Apps NFS mounts `[doc]`

Source: `https://learn.microsoft.com/en-us/azure/container-apps/storage-mounts` (ms.date 2026-08-21).

- "Azure Container Apps supports only *classic* Azure file shares … Container
  Apps doesn't support the newer `Microsoft.FileShares` top-level resource type."
- "If you're using NFS, your environment must be configured with a custom VNet
  and the storage account must be configured to allow access from the VNet."
- "If you're using NFS, the storage account must not require encrypted NFS
  connections. Container Apps doesn't support encryption in transit for NFS file
  shares. If the storage account's **Require Encryption in Transit for NFS**
  setting is enabled, or if that setting isn't configured and **Secure transfer
  required** is enabled, the mount fails with an error such as
  `mount.nfs: access denied by server while mounting`."
- "If your environment is configured with a custom VNet, you must allow ports
  445 and 2049 in the network security group (NSG) associated with the subnet."
- Environment storage: `az containerapp env storage set --storage-type NfsAzureFile --server <account>.file.core.windows.net --azure-file-share-name /<account>/<share> --access-mode ReadWrite`;
  ARM child `Microsoft.App/managedEnvironments/storages` with
  `properties.nfsAzureFile: {server, shareName: "/<account>/<share>", accessMode}`
  (the AVM emits exactly this, §1.3).
- Revision volume: `template.volumes[] = {name, storageType: "NfsAzureFile", storageName, mountOptions?}`,
  `mountOptions` "is a comma-separated string of mount options";
  `volumeMounts[] = {volumeName, mountPath, subPath?}` ("Don't start the sub path
  with `/`").
- "Multiple containers can mount the same file share, including ones that are in
  another replica, revision, or container app."

Bundle binding: storage account `kind: FileStorage`, `skuName: Premium_LRS`,
`supportsHttpsTrafficOnly: false` (plus the per-protocol NFS
encryption-in-transit setting left **off** — verify the AVM exposes it or that
the account-level flag suffices `[LIVE]`), `publicNetworkAccess: Disabled`,
private endpoint (`file` sub-resource) + `privatelink.file.core.windows.net`,
`allowSharedKeyAccess: false` (the NFS arm needs no key), share
`enabledProtocols: NFS`, `rootSquash: NoRootSquash`; environment storage
`kind: NFS`, `accessMode: ReadWrite`; revision volume `NfsAzureFile` mounted
at `/mnt/elspeth` with `mountOptions: "actimeo=30,nconnect=4,noresvport"`
(all three from the Azure table; whether the platform mount honours them is
`[LIVE]` — the first run records `mount | grep /mnt/elspeth` from
`az containerapp exec`).

### 3.3 NFS client caching versus ELSPETH's blob custody `[doc]` + `[tree]`

nfs(5) (`https://man7.org/linux/man-pages/man5/nfs.5.html`):

- Attribute caches: `acregmin` 3 s, `acregmax` 60 s, `acdirmin` 30 s,
  `acdirmax` 60 s; `actimeo=n` sets all four.
- `lookupcache`: default `all` — "the client assumes both types of directory
  cache entries are valid until their parent directory's cached attributes
  expire"; `positive` — "always revalidates negative entries"; `none` —
  revalidates both.
- "The Linux NFS client caches the result of all NFS LOOKUP requests … To detect
  when directory entries have been added or removed on the server, the Linux
  NFS client watches a directory's mtime. If the client detects a change in a
  directory's mtime, the client drops all cached LOOKUP results for that
  directory."
- Close-to-open: "the NFS client checks that the file exists on the server …
  When the application closes the file, the NFS client writes back any pending
  changes … so that the next opener can view the changes."

ELSPETH's publish path (`src/elspeth/web/blobs/service.py:536-560`): write a
temp file, `flush`, `os.fsync(handle)`, `os.replace(temp, storage)` (or the
`dir_fd` form), then `fsync` the parent directory. `RENAME` is atomic on the
NFS server; the data is flushed before the rename. Readers
(`read_blob_content`, `:2647-2690`) first lock the blob **row** for read and
require `status == 'ready'`, then `storage.exists()` → `read_bytes()` →
content-hash verification; a missing file raises `BlobContentMissingError`.

Consequences for replicas > 1 on one NFS share:

1. **Publish → read across replicas is safe under default caching.** The reader
   replica has never looked up the new name (blob ids are minted at publish), so
   its first `LOOKUP` goes to the server; there is no stale negative entry to
   serve. The DB row gate precedes any filesystem access.
2. **Bounded staleness exists only where a replica already cached a name.**
   Deletion stages bytes aside with `os.replace(storage, tombstone)` (`:606-609`)
   *before* the row commit; a peer holding a positive dentry for `storage` can
   open by the cached filehandle (NFSv4 handles survive rename) and read the
   bytes — the same content-addressed bytes, so the hash check passes. After the
   tombstone is unlinked the handle goes `ESTALE`, which `read_bytes` surfaces
   as `OSError`, not `FileNotFoundError` (`:2683-2686` catches only the latter).
   With `actimeo=30` the window is ≤ 30 s and closes on the next row read
   (the row is gone after commit). Recorded, not fixed here: a cross-replica
   delete racing a read on NFS can surface `ESTALE` as a 500 rather than the
   blob-lifecycle error — a narrow adjacent defect for a ticket (§10).
3. `fcntl.flock` is used only by the local-file sink
   (`src/elspeth/plugins/sinks/_local_file_effects.py:618`); NFSv4.1 carries
   byte-range locking natively (Azure lists only delegations, Kerberos and ACLs
   as unsupported). `flock` over NFSv4 is emulated as a whole-file byte-range
   lock by the Linux client; two replicas running that sink against one share
   is not a 0.8.0 configuration and is not claimed.
4. `os.replace` with `src_dir_fd`/`dst_dir_fd` and `os.fsync(directory_fd)`
   are ordinary NFS operations; `fsync` on a directory is a no-op/commit on NFS.
5. "Immediate cross-client visibility" (plan §4) is therefore exact for the
   publish→read flow and **bounded by `actimeo`** for re-lookups of names a
   replica already resolved. The runbook states it that way.

### 3.4 UID/GID contract `[tree]`

`Dockerfile:125-126` creates `elspeth` UID/GID 1654; `:142` chowns the runtime
root; `:166-167` labels `io.elspeth.runtime-uid=1654` / `runtime-gid=1654`;
`:185` `USER elspeth`. The `provision-storage` Job creates
`/mnt/elspeth/{data,data/blobs,payloads}` as `1654:1654` mode `0700` from a root
image (C6). Runtime containers mount the share read-write and never need
`chown`.

---

## 4. PostgreSQL Flexible Server

### 4.1 Roles and privileges `[doc]`

Sources: `https://learn.microsoft.com/en-us/azure/postgresql/security/security-manage-database-users` (2026-07-14),
PostgreSQL 17 docs (`functions-admin`, `role-attributes`, `runtime-config-client`),
Microsoft Q&A `https://learn.microsoft.com/en-us/answers/questions/1658966/` and `/1611010/`.

- Default roles: `azure_pg_admin`, `azuresu`, the server admin. "Your server
  admin user is a member of the azure_pg_admin role. However, the server admin
  account isn't part of the azuresu role. Since this service is a managed PaaS
  service, only Microsoft is part of the super user role." Admin privileges:
  "Sign in, NOSUPERUSER, INHERIT, CREATEDB, CREATEROLE".
- `pg_terminate_backend(pid)`: "This is also allowed if the calling role is a
  member of the role whose backend is being terminated or the calling role has
  privileges of `pg_signal_backend`, however only superusers can terminate
  superuser backends."
- On PG16+ Flexible Server the admin **cannot** `GRANT pg_signal_backend`
  ("Only roles with the Admin option on role 'pg_signal_backend' may grant this
  role" — Q&A 1658966, 1611010).
- CREATEROLE: "A role with `CREATEROLE` privilege can alter and drop roles
  which have been granted to the `CREATEROLE` user with the `ADMIN` option.
  Such a grant occurs automatically when a `CREATEROLE` user that is not a
  superuser creates a new role … Altering a role includes most changes that can
  be made using `ALTER ROLE`, including, for example, changing passwords."
  `createrole_self_grant` default is empty (no automatic `INHERIT`/`SET`).
- `NOLOGIN` is checked at connection time only ("Only roles that have the
  `LOGIN` attribute can be used as the initial role name for a database
  connection"); existing sessions are unaffected.

### 4.2 P3 primitive — the sequence the runbook uses (C7)

Roles: admin (server admin), `elspeth_schema_owner` (DDL), `elspeth_runtime_a`,
`elspeth_runtime_b` (runtime, no DDL), all created by the admin (so the admin
holds `ADMIN OPTION` on each). ELSPETH connects on **port 5432** (not the
PgBouncer 6432, §4.3).

```sql
-- session S, connected AS elspeth_runtime_a (its Key Vault password):
SELECT pg_backend_pid();                                  -- keep S open
-- admin session:
ALTER ROLE elspeth_runtime_a NOLOGIN;                     -- new logins refused; S survives
-- back in S (own role: always permitted):
SELECT pg_terminate_backend(pid)
  FROM pg_stat_activity
 WHERE usename = current_user AND pid <> pg_backend_pid();
-- close S. rA now has no live backend and cannot open one.
-- restore after the probe (admin):
ALTER ROLE elspeth_runtime_a LOGIN;
```

Why this is deterministic on the landed tree: both engines are built with
`pool_pre_ping: True, pool_size: 5, max_overflow: 5`
(`src/elspeth/web/schema_probe.py:79`) `[tree]`, so rA's pool detects the dead
sockets on the next checkout, attempts a reconnect, and is refused by
`NOLOGIN` — heartbeat, renew, release, cancel and the `stopped` write all fail
from that moment; rB's role is untouched. Whether rA's `web_instances` row is
then observed `state='active'` with an expired lease (the plan's P3 passing
evidence) depends on 6b-2's writer and is `[LIVE]`. Alternative if the admin
prefers a single session: `GRANT elspeth_runtime_a TO <admin> WITH INHERIT TRUE`
(permitted via `ADMIN OPTION`) then terminate from the admin session.

### 4.3 PgBouncer `[doc]`

`https://learn.microsoft.com/en-us/azure/postgresql/connectivity/concepts-pgbouncer` (2026-07-08):
built-in, per-server parameter `pgbouncer.enabled`, port **6432**, transaction
pooling, "doesn't support the Burstable server compute tier", restarts with
the server. Not used by the bundle: ELSPETH's URLs use 5432 so
`pg_terminate_backend` hits ELSPETH's own backends and pooled server
connections cannot outlive the role revocation.

### 4.4 TLS `[doc]` + `[local]`

`https://learn.microsoft.com/en-us/azure/postgresql/security/security-tls` (2026-07-14):
`require_secure_transport` on by default; TLS 1.2/1.3 only; root CAs
**DigiCert Global Root G2** (`https://cacerts.digicert.com/DigiCertGlobalRootG2.crt`)
and **Microsoft RSA Root CA 2017**
(`https://www.microsoft.com/pkiops/certs/Microsoft%20RSA%20Root%20Certificate%20Authority%202017.crt`);
intermediates rotate unannounced — never pin them; recommended
`sslmode=verify-full` (the page's "verify-all" is a typo for libpq's
`verify-full`), or `verify-ca` when private-endpoint DNS breaks hostname
matching; mTLS and custom server certificates unsupported.

The runtime image's distroless base
(`gcr.io/distroless/python3-debian13:debug-nonroot@sha256:6418f576…`,
`Dockerfile:154`) ships `/etc/ssl/certs/ca-certificates.crt` with 150 roots
including **both** Azure roots and DigiCert Global Root CA `[local]`. psycopg
3.3.4 bundles libpq **18.0**, psycopg2 2.9.12 bundles libpq 17.9 `[local]`; both
≥ 16, so `sslrootcert=system` is available. The ACA runbook therefore uses
`sslmode=verify-full&sslrootcert=system` with no baked bundle (the RDS bundle at
`Dockerfile:168-169` is AWS-only); `verify-ca` is the documented fallback when
the private-endpoint FQDN does not match the certificate `[LIVE]`. The doctor's
`postgres_tls_check` reports the posture either way.

### 4.5 Networking, versions, defaults `[doc]` + `[local]`

- Private Link (`https://learn.microsoft.com/en-us/azure/postgresql/network/concepts-networking-private-link`, 2026-07-13):
  private endpoints require the server to be created in **public access**
  networking mode (not VNet-integrated); "Allowing also public/internet access
  with firewall rules: Yes"; DNS zone `privatelink.postgres.database.azure.com`;
  with a private endpoint the server FQDN becomes a CNAME to
  `<server>.privatelink.postgres.database.azure.com`.
  **Acceptance shape:** private endpoint for the app + one firewall rule for the
  operator's public IP so `psql` runs from the operator host; production
  disables public access.
- Versions (`…/configure-maintain/concepts-supported-versions`, 2026-08-26): 18
  (18.6), 17 (17.11), 16 (16.15) all listed as supported without a preview
  caveat; 18 carries extension limitations and no `io_uring`. The bundle pins
  **`version: '17'`** (the AVM default 18 is the newest major; 17 is the
  conservative pin for a release the operator will not upgrade mid-window).
- AVM defaults the acceptance overrides: `authConfig` →
  `{activeDirectoryAuth: Disabled, passwordAuth: Enabled}` (C9);
  `highAvailability: Disabled`; `geoRedundantBackup: Disabled`;
  `availabilityZone: -1`; `tier: Burstable`, `skuName: Standard_B2s`
  (max_connections on B2s `[LIVE]`; Burstable has no PgBouncer, which is not
  used); `serverThreatProtection: Disabled` for the disposable RG.
- Connection budget arithmetic for the receipt: per replica per engine
  `pool_size 5 + max_overflow 5 = 10`; two engines per replica; two replicas →
  **40** possible backends plus jobs and the operator's psql. The
  `verify-connection-budget` receipt reads `az monitor metrics list --metric active_connections`
  against the server's `max_connections` `[LIVE]`.

---

## 5. Logs and evidence

### 5.1 Log Analytics destination (bundle default) `[doc]`

`https://learn.microsoft.com/en-us/azure/container-apps/log-monitoring` (2026-06-01),
`…/log-options` (2026-08-18).

- Environment `appLogsConfiguration.destination: log-analytics` with
  `logAnalyticsConfiguration.customerId/sharedKey` (the AVM fills both from the
  workspace resource id).
- Tables: `ContainerAppConsoleLogs_CL` (stdout/stderr) and
  `ContainerAppSystemLogs_CL`.
- Documented console columns: `ContainerAppName_s`, `ContainerGroupName_g`
  (documented as "Replica name"), `ContainerId_s`, `ContainerImage_s`,
  `EnvironmentName_s`, `Log_s`, `RevisionName_s`, and (from the sample
  queries) `ContainerName_s`, `LogLevel_s`, `TimeGenerated`.
  Documented system columns: `ContainerAppName_s`, `EnvironmentName_s`,
  `Log_s`, `RevisionName_s`.
  The plan's rule stands: these names are **verified live and never pinned in a
  unit test** — the doc's `_g` suffix on the replica column is exactly the kind
  of detail the first run confirms or corrects `[LIVE]`.
- System log messages worth matching: "Creating a new revision: <name>",
  "Successfully provisioned revision <name>", "Deactivating old revisions since
  'ActiveRevisionsMode=Single'", "Setting a traffic weight of <n>% for revision
  <name>", "Successfully mounted volume <name> for revision <scope>",
  "Error mounting volume <name>".
- CLI: `az monitor log-analytics query --workspace <customerId> --analytics-query "<kql>"`.
- Latency: "Expect a delay of several minutes before new `ContainerAppConsoleLogs`
  and `ContainerAppSystemLogs` records are queryable"; Azure Monitor's generic
  figures: resource logs "usually available within 3 to 10 minutes end-to-end",
  average ingest "less than 10 seconds" once at the endpoint
  (`…/azure-monitor/logs/data-ingestion-time`, 2026-07-29). The driver polls
  with a 10-minute ceiling and records `ingestion_time() - TimeGenerated`.

### 5.2 Azure Monitor destination and HTTP logs (alternative, recorded) `[doc]`

- `--logs-destination azure-monitor` + `az monitor diagnostic-settings create --logs '[{"categoryGroup":"allLogs","enabled":true}]'`
  routes to resource-specific tables named by category:
  `ContainerAppConsoleLogs`, `ContainerAppSystemLogs`; "Sending logs directly to
  a Log Analytics Workspace through Private Link isn't supported" (the
  Azure Monitor route is the workaround).
- `ContainerAppHTTPLogs` (ingress-emitted via diagnostic settings on the
  environment) carries `Method`, `Path`, `StatusCode`, `ResponseCodeDetails`
  (`via_upstream`), `RequestId`, `ContainerAppName`, `RevisionName`,
  **`ReplicaName`**, `UpstreamHost`, `RequestDuration`. This is an
  ingress-observed record of "one 2xx and one 409 on two replica names" for P1
  that does not depend on application logging. Whether the HTTP-log category is
  selectable on this environment type is `[LIVE]`; if it is, 6b-5's evidence
  projection may adopt it as a second source, never as the only one.

### 5.3 KQL files the bundle ships (6b-1)

`kql/doctor-report.kql` (the `--json` report by `ContainerAppName_s` =
`<job>` and execution name), `kql/run-sentinel-by-replica.kql`
(`RunStarted`/`RunFinished` lines by sentinel hash summarised by
`RevisionName_s`, `ContainerGroupName_g`), `kql/replica-lifecycle.kql`
(system logs for the P3 window), `kql/fence-conflict-409.kql`
(`"Session operation is already active"`, `app.py:1179` `[tree]`, joined to two
replica names). SHA-256 of each file is bound into the receipt; column names
inside them are the §5.1 documented names until the live run corrects them.

---

## 6. Image publication and the ACR == GHCR digest `[local]` + `[tree]`

### 6.1 What the workflow does today `[tree]`

`.github/workflows/build-push.yaml`: registry selection `ghcr` \| `acr` \|
`both` (`:45-46`, `:194-209`); GHCR build+push (`:253-268`) and ACR build+push
(`:273-288`) are **separate** `docker/build-push-action` invocations with the
same context, `provenance: true`, `sbom: true`, `cache-from/to: type=gha`;
outputs `ghcr_digest` / `acr_digest` (`:95-96`); both digests are cosign-signed
(`:303-315`); the smoke job binds to "both independently-pushed digests"
(`:359-363`); the release job promotes each digest to the tag with
`docker buildx imagetools create --tag … <registry>@<digest>` (`:628-636`).

### 6.2 Measured behaviour (buildx v0.36.1, two local `registry:2` instances)

| experiment | result |
|---|---|
| build A → registry 1, build B → registry 2, identical Dockerfile/context, `--provenance=true --sbom=true` | index digests **differ** (`65e33502…` vs `e5a248ad…`); the `linux/amd64` image manifest is identical (`020ce3e4…`) and only the `attestation-manifest` entry differs (`4f023d0a…` vs `6aabc48e…`) |
| `docker buildx imagetools create --tag <reg2>/x:copied <reg1>/x@<digestA>` | digest **preserved** (`65e33502…` on both registries) |
| one build, `-t <reg1>/x:both -t <reg2>/x:both` | **equal** digests (`68aab0c7…`) |

The plan's assertion "ACR digest == GHCR digest" is false by construction on
the current workflow and true under either of the two mechanisms measured. The
copy mechanism is the one the workflow already trusts for tag promotion.

### 6.3 Design 6b-1 implements

Replace the ACR build step with a **digest-preserving copy** of the GHCR
image when both registries are selected (`docker buildx imagetools create --tag <acr>/<img>:sha-<sha> ghcr.io/<owner>/<img>@<ghcr_digest>`),
keep the ACR-only build path for a run that pushes to ACR alone, set
`acr_digest` from `docker buildx imagetools inspect <acr>/<img>:sha-<sha> --format '{{.Manifest.Digest}}'`,
and add the assertion `test "$ACR_DIGEST" = "$GHCR_DIGEST"` guarded on both
being non-empty. Cosign then signs one digest in two registries, and the ACA
receipt's `cosign verify` against the GHCR-signed identity is meaningful. The
smoke comment at `:359` is reworded to match.

### 6.4 `az acr` commands the runbook cites `[doc]`

`https://learn.microsoft.com/en-us/cli/azure/acr/manifest` (2026-07-28; the
group is marked preview):
`az acr manifest show-metadata <registry>.azurecr.io/<repo>:<tag>` (or
`-r <registry> -n <repo>@sha256:<digest>`) → metadata including `digest`;
`az acr manifest show … --raw` → the raw manifest bytes;
`az acr manifest list-metadata -r <registry> -n <repo>`;
`az acr manifest list-referrers …` (cosign signatures as referrers).

---

## 7. Kill and partition primitives

### 7.1 `kill -9 1` inside the container `[local]`

Measured in a Docker PID namespace (kernel 6.8.0-137-generic):
`alpine:3.20` `sh -c 'kill -9 1; …; kill -TERM 1; …; exit 7'` → both `kill`
calls return 0, the process continues, exit code 7 is ours; `python:3.12-alpine`
`os.kill(1, 9)` then `os.kill(1, SIGTERM)` → process continues, exit 0. A signal
sent to PID 1 from inside its own PID namespace is discarded unless PID 1
installed a handler (SIGKILL/SIGSTOP are never deliverable from inside). So
`az containerapp exec … -- kill -9 1` is a no-op on ACA exactly as the plan
predicted; **recorded, not used**.

### 7.2 Platform kills

- `az containerapp revision deactivate` / scale-in: SIGTERM then SIGKILL after
  `terminationGracePeriodSeconds` (§2.5); graceful-first — reaches the lifespan
  release path and therefore **releases** fences (the opposite of a dead owner).
- Grace-0 deactivate: "stop immediately via the kill signal (no opportunity to
  shut down)" — the secondary primitive; `stopped`-write race `[LIVE]`.
- Role revocation (§4.2): the primary primitive; platform-independent.

---

## 8. Bundle parameter bindings derived here (6b-1 pins these)

| parameter / setting | production set | acceptance set |
|---|---|---|
| `activeRevisionsMode` | `Single` | `Multiple` |
| `stickySessionsAffinity` | `sticky` | `none` (C3) |
| `traffic` | `[{latestRevision: true, weight: 100}]` | `[{label: 'a', revisionName: <rA>, weight: 50}, {label: 'b', revisionName: <rB>, weight: 50}]`; second pass one revision `min=max=2` |
| `scaleSettings` | `{minReplicas: 2, maxReplicas: 4}` (≥1 / ≥2 asserted) | `{minReplicas: 1, maxReplicas: 1}` per revision |
| `terminationGracePeriodSeconds` | `60` | `60` (grace-0 only by CLI in P3-secondary) |
| `ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS` | required parameter, no default; example `210` (≤ 240, C4) | same |
| probes | startup `/api/health` 15 s × 10; liveness `/api/health` 30 s × 3; readiness `/api/ready` 10 s × 3 | same |
| env | `ELSPETH_WEB__DEPLOYMENT_TARGET=azure-container-apps`, `ELSPETH_WEB__DEPLOYMENT_STATE_MODE=external-postgresql`, `ELSPETH_WEB__HOST=0.0.0.0`, `WEB_CONCURRENCY=1`, `ELSPETH_WEB__LOG_JSON=true`, `ELSPETH_WEB__OPERATOR_TELEMETRY=prometheus` | same |
| secrets | Key Vault refs, versioned URLs, `identity: <uami id>`; no `value:` anywhere | same; two runtime URL secrets |
| image | `<acr>.azurecr.io/elspeth@sha256:<digest>` parameter, digest-only | same |
| registries | `{server: <acr>, identity: <uami id>}`; `AcrPull` on the **existing** registry (C11) | same |
| volume | `NfsAzureFile` `/mnt/elspeth`, `mountOptions: actimeo=30,nconnect=4,noresvport` | same |
| jobs | `provision-storage` (root image), `doctor-schema-init`, `doctor-runtime`, `verify-blob-managed-identity`; `Manual`, retry 0, timeout 1800 | same |
| PostgreSQL | `version '17'`, `passwordAuth: Enabled`, `publicNetworkAccess: Disabled`, private endpoint + zone | + operator-IP firewall rule; HA/geo-backup disabled; `Standard_B2s` |
| storage account | `FileStorage`/`Premium_LRS`, `supportsHttpsTrafficOnly: false`, `allowSharedKeyAccess: false`, share `NFS` + `NoRootSquash` | same |
| Key Vault | RBAC, purge protection **on** | purge protection **off** (C8) |
| Log Analytics | retention 90 | retention 30 |
| environment | custom VNet (`infrastructureSubnetResourceId`), NSG allowing 445/2049 to storage, `zoneRedundant: true`, `internal: false` | `zoneRedundant: false` |

---

## 9. What the tree already guarantees (no 6b-0 action) `[tree]`

- `azure-container-apps` is a closed `DeploymentTarget` literal (`config.py:91`),
  in `EXTERNAL_POSTGRESQL_TARGETS` (`deployment_contract.py:28`), refuses
  `sqlite-single` (`:77`) and resolves `auto` to `external-postgresql` (`:82`).
- Both engines: `pool_pre_ping`, 5 + 5 (`schema_probe.py:79`).
- 409 body `"Session operation is already active"` (`app.py:1179`).
- Routes `/api/health` `:1985`, `/api/ready` `:1995`, `/api/system/status` `:2018`.
- `WEB_CONCURRENCY` > 1 is refused as a multi-worker reason (`app.py:1666-1668`).
- The skills tree convention: canonical `.agents/skills/<name>/{SKILL.md,agents/openai.yaml,references/*.md}`
  with a tracked symlink `.claude/skills/<name> → ../../.agents/skills/<name>`;
  `tests/unit/docs/test_project_agent_guidance.py:228-233` requires the AWS skill
  text to contain `PYTHONPATH` and `.venv/bin/pytest` and never `uv sync`/`uv run`.
- `tests/unit/docs/test_deleted_ci_script_references.py` excludes `docs/plans/`
  from its active-reference scan, so this file cannot red that gate.

---

## 10. Needs the operator (one page) — `[LIVE]` residue carried to 6b-7

Everything below needs a subscription, `az login`, or a running environment.
None of it blocks 6b-1 or 6b-6; each is a receipt field or a runbook check
that the first live run fills in.

1. **Subscription + resource group scope**: a non-production subscription id,
   the region, and a service principal or `az login` context with `Contributor`
   on a disposable RG plus `Log Analytics Reader` on the workspace; the existing
   ACR's resource id (for `AcrPull`) and `secrets.ACR_REGISTRY` host name.
2. **Log Analytics column names** for `ContainerAppConsoleLogs_CL` /
   `ContainerAppSystemLogs_CL` as ingested (the replica column's exact name and
   suffix); job-execution log latency; whether `ContainerAppHTTPLogs` is
   selectable as a diagnostic category on the environment.
3. **NFS**: share-root ownership/mode after creation; that the platform honours
   `mountOptions` (`mount | grep /mnt/elspeth` via `az containerapp exec`);
   whether the AVM storage module exposes the per-protocol NFS
   encryption-in-transit flag or the account-level `supportsHttpsTrafficOnly: false`
   suffices; `os.replace` + read across two replicas timing under `actimeo=30`.
4. **P3 primitives**: (a) the §4.2 sequence leaves rA's `web_instances` row
   `active` with an expired lease and its fence unreleased while rB is
   unaffected — needs 6b-2's writer; (b) whether a grace-0
   `revision deactivate` ever lands a `stopped` write; (c) the `kill -9 1`
   no-op is already measured locally (§7.1) — re-record from `az containerapp exec`
   for the receipt.
5. **Label URLs and affinity**: two active revisions at 50/50 each reachable on
   `https://<app>---<label>.<defaultDomain>` with `X-Elspeth-Instance` distinct
   (needs 6b-3); that the platform rejects `stickySessions: sticky` under
   `Multiple` mode (expected per §2.3) rather than silently ignoring it.
6. **Ingress**: WebSocket idle behaviour at the 240 s request timeout; the
   `replica list` output shape.
7. **PostgreSQL**: `max_connections` on the chosen SKU for the connection-budget
   receipt; `verify-full` hostname match through the private endpoint
   (fallback `verify-ca`); confirm `ALTER ROLE … NOLOGIN` by the admin on a
   role it created succeeds on Flexible Server (PG17).
8. **Probes**: that the RP accepts `failureThreshold: 10, periodSeconds: 15`
   for the startup probe (Bicep already validates the ranges) and that a cold
   start of the epoch-52 image completes inside 150 s on `0.5 vCPU / 1 Gi`
   (raise to `1.0 / 2 Gi` if not).
9. **Digest**: `az acr manifest show-metadata` on the copied image returns the
   GHCR digest; `cosign verify` of the ACR reference against the GHCR-signed
   identity succeeds.
10. Adjacent defect to ticket (not fixed in this lane): a cross-replica blob
   delete racing a read on NFS can surface `ESTALE` as an `OSError` 500 instead
   of `BlobContentMissingError` (§3.3 item 2; `blobs/service.py:2683-2686`
   catches only `FileNotFoundError`).

---

## Appendix — commands behind the facts (all on 2026-09-05)

- Bicep: `curl -sSL -o bin/bicep https://github.com/Azure/bicep/releases/download/v0.46.1/bicep-linux-x64 && sha256sum bin/bicep && ./bin/bicep --version`; `bicep restore restore.bicep` with a `bicepconfig.json` setting `cacheRootDirectory`; `find <cache> -name main.json` → 12 files; `bicep build restore.bicep --stdout` → only BCP035/BCP333 parameter errors.
- AVM tags: `curl -sS https://mcr.microsoft.com/v2/bicep/avm/res/<path>/tags/list | jq '.tags | sort_by(split(".")|map(tonumber)) | .[-4:]'` for each of the 12 paths.
- AVM parameters/definitions: `jq '.parameters | to_entries[] | …'`, `jq '.definitions.<type>'`, `jq '.resources | … | select(.type=="Microsoft.App/managedEnvironments/storages")'` over each cached `main.json`; API versions via `jq '[.resources[] | "\(.type)@\(.apiVersion)"] | unique'`.
- `kill -9 1`: `docker run --rm alpine:3.20 sh -c 'echo pid=$$; kill -9 1; echo rc=$?; kill -TERM 1; echo rc=$?; exit 7'` → exit 7; `docker run --rm python:3.12-alpine python -c 'import os,signal; os.kill(1,9); print("alive"); os.kill(1,signal.SIGTERM); print("alive")'` → exit 0. (`unshare --user --pid` is refused on this host: `write failed /proc/self/uid_map: Operation not permitted`.)
- Digest experiment: two `registry:2` containers on `127.0.0.1:5311/5312`, a `docker-container` builder with `network=host` and an insecure-registry `buildkitd.toml`; `docker buildx build --push --provenance=true --sbom=true -t … --metadata-file mN.json .` twice; `docker buildx imagetools create --tag … <ref>@<digest>`; `docker buildx imagetools inspect <ref> --format '{{.Manifest.Digest}}'`; `… --raw | jq '.manifests[] | {mediaType, digest, platform, annotations}'`.
- Image CA store: `docker run --rm --entrypoint /busybox/sh gcr.io/distroless/python3-debian13:debug-nonroot@sha256:6418f576… -c 'python3 -c "import ssl; …get_ca_certs()…"'` → 150 roots, both Azure roots present.
- libpq: `.venv/bin/python -c 'import psycopg, psycopg2; print(psycopg.pq.version(), psycopg2.__libpq_version__)'` → `180000`, `170009`.
- Tree: `grep -n` for every `path:line` cited; `git rev-parse HEAD` → `02d10e0c1530cbf8aea4d5c76f0e5b30f147efb2`.
- Docs: each `[doc]` URL fetched 2026-09-05; `ms.date` quoted from the page front matter.
