# Runbook: Deploy a new ELSPETH stack on Azure Container Apps

Use this procedure to install a complete ELSPETH stack on Azure Container Apps
from an empty resource group with the tracked Bicep bundle at
[`deploy/azure-container-apps/`](../../deploy/azure-container-apps/README.md).
The bundle composes Azure Verified Modules (versions pinned in the
[platform facts](../plans/2026-09-05-phase6b-azure-container-apps-platform-facts.md) §1.2)
into a virtual network, a Container Apps environment, Azure Database for
PostgreSQL Flexible Server, an NFS 4.1 Azure Files share, Key Vault, Log
Analytics, a user-assigned managed identity, the web app and its Jobs.

> **Status.** Skeleton prepared by Phase 6b before the first live run; steps
> marked **LIVE** are completed from the 6b-7 acceptance. Until the sanitized
> receipt at `docs/operator/evidence/azure-container-apps/0.8.0.json` exists,
> this is not a support claim.

## Choose the correct Azure procedure

| Need | Procedure |
| --- | --- |
| Create a new, complete stack in an empty resource group | This runbook |
| Replace the image or configuration of an existing container app | [Existing-service redeploy](azure-container-apps-existing-service-redeploy.md) |
| Run the release qualification program with the replica > 1 probes | [Full disposable acceptance](azure-container-apps-deployment.md) |
| Run exactly one Azure Ubuntu VM instead | [Native Linux/Azure VM runbook](ansible-ubuntu-deployment.md) |

Do not use the full acceptance runbook for an ordinary cold install; it is a
release-evidence controller.

## Fast path

1. prove the subscription, region and identity;
2. `what-if` and deploy `environment.bicep` (network, environment, storage,
   database, Key Vault, workspace, identity);
3. publish the image to the registry as a digest-preserving copy and pin the
   digest;
4. put every secret in Key Vault as a versioned secret;
5. run the `provision-storage` Job;
6. run the `doctor-schema-init` Job with the schema-owner URLs;
7. run the `doctor-runtime` Job with the runtime URLs;
8. deploy `workload.bicep` in the production shape and prove the rollout; and
9. verify public behaviour and record the operator-local notes.

Every step has a stop condition. Do not skip forward after a failed identity,
image, doctor or readiness check.

## Result and limits

A successful install has:

- one container app `elspeth-web` in `Single` revision mode with
  `minReplicas ≥ 1` and `maxReplicas ≥ 2`, session affinity on, an external
  HTTPS ingress, and startup, liveness and readiness probes on `/api/health`,
  `/api/health` and `/api/ready`;
- both databases (`elspeth_sessions`, `elspeth_landscape`) on one Flexible
  Server behind a private endpoint, one schema-owner role and one runtime role,
  session epoch 53 and Landscape epoch 38 initialized;
- one NFS 4.1 Azure Files share mounted at `/mnt/elspeth` on the app and every
  Job with `data`, `data/blobs` and `payloads` owned `1654:1654`. SMB Azure
  Files is not supported for this target; **Azure Files carries no database**
  and there is no SQLite mode at replicas > 1 (`sqlite-single` is refused);
- Key Vault references for every secret, no `value:` anywhere in the bundle;
- Log Analytics as the log destination; and
- no static credential in any container: image pull and Key Vault use the
  user-assigned identity, PostgreSQL uses a role password held in Key Vault.

The bundle is a **disposable-by-default** package for a dedicated resource
group. It is not a multi-region design and it does not manage the registry
(the existing registry that CI publishes to is referenced by resource id).

## Prerequisites

- Azure CLI with the `containerapp` extension, the pinned Bicep CLI
  (facts §1.1), `jq`, `curl`, Docker Buildx and `cosign`.
- `az login` with `Contributor` on the target resource group and the right to
  assign `AcrPull` on the existing registry.
- The candidate digest already published to GitHub Container Registry by
  `build-push.yaml`.
- A value for `ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS`. The
  ingress request timeout is a fixed 240 seconds (facts §2.2); if a Front Door
  or other hop fronts the ingress, take the minimum across hops. The bundle
  refuses to compile without this parameter.

Set only operator-selected, non-secret inputs:

```bash
set -Eeuo pipefail
umask 077
export AZURE_CORE_OUTPUT=json
: "${AZURE_SUBSCRIPTION_ID:?set the subscription id}"
: "${AZURE_LOCATION:?set the region}"
: "${RESOURCE_GROUP:?set the empty resource group name}"
: "${ACR_RESOURCE_ID:?set the existing registry's resource id}"
: "${ACR_LOGIN_SERVER:?set the existing registry's login server}"
: "${DEPLOY_REF:?set the exact branch, tag, or commit to deploy}"
: "${ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS:?set the transport ceiling; at most 240 on this platform}"
test "$ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS" -le 240

CANDIDATE_SHA=$(git rev-parse "${DEPLOY_REF}^{commit}")
test "$(git rev-parse HEAD)" = "$CANDIDATE_SHA"
test -z "$(git status --porcelain)"
az account set --subscription "$AZURE_SUBSCRIPTION_ID"
az account show --query '{subscription:id,tenant:tenantId,user:user.name}'
```

## 1. Select and prove the identity

```bash
az group create --name "$RESOURCE_GROUP" --location "$AZURE_LOCATION" --tags elspeth.stack=cold-install
az acr show --ids "$ACR_RESOURCE_ID" --query '{name:name,loginServer:loginServer}'
```

Stop if the registry's login server differs from `ACR_LOGIN_SERVER`.

## 2. Deploy the environment

```bash
az deployment group what-if --resource-group "$RESOURCE_GROUP" \
  --template-file deploy/azure-container-apps/environment.bicep \
  --parameters deploy/azure-container-apps/environment.example.bicepparam \
  --parameters containerRegistryResourceId="$ACR_RESOURCE_ID"
az deployment group create --name elspeth-environment --resource-group "$RESOURCE_GROUP" \
  --template-file deploy/azure-container-apps/environment.bicep \
  --parameters deploy/azure-container-apps/environment.example.bicepparam \
  --parameters containerRegistryResourceId="$ACR_RESOURCE_ID" \
  --query properties.outputs >environment-outputs.json
```

The environment deployment creates the custom virtual network the NFS mount
requires, with an NSG allowing 445 and 2049 to the storage private endpoint
(facts §3.2); the Premium `FileStorage` account with `supportsHttpsTrafficOnly:
false` (Container Apps cannot mount an NFS share that requires encryption in
transit), the NFS share with `rootSquash: NoRootSquash`, and the
`privatelink.file.core.windows.net` zone; the Flexible Server (`version 17`,
password authentication enabled, public network access disabled, private
endpoint plus `privatelink.postgres.database.azure.com`); the Key Vault (RBAC);
the Log Analytics workspace; and the identity with `AcrPull` on the existing
registry, `Key Vault Secrets User` on the vault and the blob role on the
payload container.

## 3. Publish the image by digest

```bash
GHCR_DIGEST=$(docker buildx imagetools inspect "ghcr.io/dta-au/elspeth:sha-${CANDIDATE_SHA}" \
  --format '{{.Manifest.Digest}}')
az acr login --name "${ACR_LOGIN_SERVER%%.*}"
docker buildx imagetools create --tag "${ACR_LOGIN_SERVER}/elspeth:sha-${CANDIDATE_SHA}" \
  "ghcr.io/dta-au/elspeth@${GHCR_DIGEST}"
ACR_DIGEST=$(az acr manifest show-metadata "${ACR_LOGIN_SERVER}/elspeth:sha-${CANDIDATE_SHA}" \
  --query digest --output tsv)
test "$ACR_DIGEST" = "$GHCR_DIGEST"
export CANDIDATE_IMAGE="${ACR_LOGIN_SERVER}/elspeth@${ACR_DIGEST}"
```

A registry-to-registry copy preserves the index digest; a second build never
does (facts §6.2). Deploy the `@sha256:` reference, never a tag.

## 4. Store secrets in Key Vault

Create each secret as a versioned Key Vault secret and record the version id;
the workload parameters reference `https://<vault>.vault.azure.net/secrets/<name>/<version>`.
Required names: `elspeth-session-db-url-runtime`, `elspeth-landscape-url-runtime`,
`elspeth-session-db-url-schema-owner`, `elspeth-landscape-url-schema-owner`,
`elspeth-secret-key`, `elspeth-shareable-link-signing-key`,
`elspeth-fingerprint-key`, the composer endpoint key(s) and
`elspeth-operator-metrics-bearer-token`. PostgreSQL URLs use
`sslmode=verify-full&sslrootcert=system`: the runtime image's CA store carries
both Azure roots (facts §4.4). Never print a secret value.

## 5. Provision storage

```bash
az containerapp job start --name provision-storage --resource-group "$RESOURCE_GROUP"
```

The Job runs a digest-pinned root image (the runtime image is `USER 1654` and
the platform offers no `runAsUser`) and creates `/mnt/elspeth/data`,
`/mnt/elspeth/data/blobs` and `/mnt/elspeth/payloads` as `1654:1654`, mode
`0700`. Wait for `properties.status == Succeeded` with
`az containerapp job execution list` before continuing.

> **LIVE:** the share root's ownership and mode after creation.

## 6. Initialize schemas

```bash
az containerapp job start --name doctor-schema-init --resource-group "$RESOURCE_GROUP"
```

`doctor-schema-init` runs `elspeth doctor deployment --init-schema --json`
with the schema-owner URLs. `--init-schema` initializes only `MISSING` or
repairable schemas; `STALE` is a stop, not a migration.

## 7. Prove runtime credentials

```bash
az containerapp job start --name doctor-runtime --resource-group "$RESOURCE_GROUP"
```

`doctor-runtime` runs `elspeth doctor deployment --json` with the runtime
URLs; every check must be OK, including `session_tls`, `landscape_tls`,
`payload_store_writable` and `blob_writable`. Retrieve the report from Log
Analytics by execution name (ingestion lags by minutes, facts §5.1).

## 8. Deploy the workload

```bash
az deployment group create --name "elspeth-workload-${CANDIDATE_SHA:0:12}" --resource-group "$RESOURCE_GROUP" \
  --template-file deploy/azure-container-apps/workload.bicep \
  --parameters deploy/azure-container-apps/workload.production.bicepparam \
  --parameters image="$CANDIDATE_IMAGE" revisionSuffix="${CANDIDATE_SHA:0:12}" \
    composerTransportIdleCeilingSeconds="$ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS"
```

Production shape: `activeRevisionsMode: Single`, `stickySessions.affinity:
sticky`, `minReplicas: 2`, `maxReplicas: 4`, `terminationGracePeriodSeconds:
60`, startup probe `/api/health` (15 s × 10), liveness `/api/health`
(30 s × 3), readiness `/api/ready` (10 s × 3), environment
`ELSPETH_WEB__DEPLOYMENT_TARGET=azure-container-apps`,
`ELSPETH_WEB__DEPLOYMENT_STATE_MODE=external-postgresql`,
`ELSPETH_WEB__HOST=0.0.0.0`, `WEB_CONCURRENCY=1`, `ELSPETH_WEB__LOG_JSON=true`.

## 9. Verify

```bash
FQDN=$(az containerapp show --name elspeth-web --resource-group "$RESOURCE_GROUP" \
  --query properties.configuration.ingress.fqdn --output tsv)
az containerapp revision list --name elspeth-web --resource-group "$RESOURCE_GROUP" \
  --query "[?properties.active].{name:name,traffic:properties.trafficWeight,state:properties.runningState}"
az containerapp replica list --name elspeth-web --resource-group "$RESOURCE_GROUP" \
  --revision "elspeth-web--${CANDIDATE_SHA:0:12}" --query '[].name'
curl --silent --fail-with-body "https://${FQDN}/api/health"
curl --silent --fail-with-body "https://${FQDN}/api/ready" | jq -e '.ready == true'
curl --silent --fail-with-body "https://${FQDN}/api/system/status" | jq '{deployment_target, frontend_build, instance_id}'
```

Require exactly one active revision at 100 % with the candidate digest, `N`
running replicas, HTTP 200 on both probes and the expected
`/api/system/status` facts. Record the resource group, revision name and
digest in operator-local notes under
`~/.local/state/elspeth/azure-container-apps/`, not in a tracked file.

## Troubleshooting

### The runtime doctor fails TLS or authentication

`verify-full` fails when the private-endpoint FQDN does not match the server
certificate; use `sslmode=verify-ca` (facts §4.4) and record the posture. A
`NOLOGIN` or password failure on the runtime role is a Key Vault version
mismatch: the app reads the version the parameters name, not "latest".

### The Job cannot mount the share

`mount.nfs: access denied by server while mounting` means encryption in
transit is still required on the storage account, or the NSG does not allow
2049 to the private endpoint (facts §3.2).

### The revision never becomes ready

The startup probe budget is 150 s (15 s × 10; the platform caps
`failureThreshold` at 10). Raise CPU/memory to `1.0 / 2Gi` before raising the
period; read the system log messages in `ContainerAppSystemLogs_CL`.

## Teardown

```bash
az group delete --name "$RESOURCE_GROUP" --yes
az graph query -q "Resources | where resourceGroup =~ '${RESOURCE_GROUP}' | count"
```

A production Key Vault has purge protection on and cannot be purged; record
its soft-delete tombstone. Registry images are not owned by the resource
group and are left in place.
