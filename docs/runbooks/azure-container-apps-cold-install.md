# Runbook: Deploy a new ELSPETH stack on Azure Container Apps

Use this procedure to install a complete ELSPETH stack on Azure Container Apps
from an empty resource group with the tracked Bicep bundle at
[`deploy/azure-container-apps/`](../../deploy/azure-container-apps/README.md).
The bundle composes Azure Verified Modules (versions pinned in the
[platform facts](../plans/2026-09-05-phase6b-azure-container-apps-platform-facts.md) §1.2)
into a virtual network, a Container Apps environment, Azure Database for
PostgreSQL Flexible Server, an NFS 4.1 Azure Files share, Key Vault, Log
Analytics, separate runtime and schema-owner managed identities and vaults,
the web app and its Jobs.

> **Status.** The implemented ACA slice received desktop acceptance:
> `elspeth-5ec3befc1a` closed on 2026-09-10 by operator ruling. No live cloud
> acceptance is claimed. This is an executable operator procedure; steps marked
> **LIVE** require measurements during execution. A future acceptance receipt at
> `docs/operator/evidence/azure-container-apps/0.8.1.json` is no longer a tracker
> closure or documentation-promotion condition.

The supported configuration retains `Single` revision mode, `sticky` session
affinity and 2–4 replicas with one web process per replica. External PostgreSQL
provides single-use tickets and durable run-event replay on authorized peer
reconnect, renewable Composer request leases with saved progress and current
inflight accounting, and shared budgets for auth, writes and Composer/execution
work. An interrupted provider request is not automatically resumed. Automatic
run handoff covers durable admission, permit-bound PREPARED initialization and
eligible checkpoint resume, using fresh web and Landscape authority. Unsafe
effects, incomplete sources and identity/compatibility failures remain
`recovery_required`; see the [handoff contract](../reference/deployment-platforms.md#durable-run-handoff).
Integrated verification is recorded in the
[ACA plan](../plans/2026-09-10-aca-pivot-and-replica-residuals.md#final-verification).
Evidence remains limited to
local PostgreSQL mechanism and integration evidence, with no cloud receipt or
no-affinity deployment qualification. The legacy v2 P4b receipt remains
conservative `cannot_pass`; it does not measure the new runtime capabilities.
Receipt evolution is deferred.

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

An unsigned release candidate is supported for the soft launch. Use its exact
GHCR digest and verify the image's source label and CLI before copying it to
ACR. A CI signature and the disposable acceptance receipt are not prerequisites
for this install. The image source commit and the checkout containing these
deployment fixes may differ; keep both identities explicit.

1. prove the subscription, region and identity;
2. `what-if` and deploy `environment.bicep` (network, environment, storage,
   database, Key Vault, workspace, identity);
3. publish the image to the registry as a digest-preserving copy and pin the
   digest;
4. put every secret in Key Vault as a versioned secret;
5. resolve an operator-local workload parameter file, create Jobs with
   `deployWebApp=false`, then run the `provision-storage` Job;
6. run the `doctor-schema-init` Job with the schema-owner URLs;
7. run the `doctor-runtime` Job with the runtime URLs;
8. deploy `workload.bicep` in the production shape and prove the rollout;
9. verify public behaviour and record the operator-local notes; and
10. optionally, grant the web identity read access to an Azure AI Search index
    and declare it as an operator profile for the `azure_ai_search` transform.

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
  session epoch 66 and Landscape epoch 45 initialized;
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
  (facts §1.1), `jq`, `curl`, Docker Buildx, and PostgreSQL `psql`.
  `cosign` is needed only when selecting a signed release.
- Resource deployment rights on the target resource group **and the existing
  registry's resource group** (the registry role assignment uses a nested
  deployment there). Also role-assignment rights at the target group and
  registry, plus image push rights on the registry. Contributor alone cannot
  create role assignments; Contributor plus appropriately scoped RBAC
  Administrator is one arrangement. Creating the empty resource group needs
  permission at the subscription scope, or have it created beforehand.
- The selected image already published to GHCR, whether a locally built RC
  or a signed CI release, and its exact source SHA and digest.
- A host that can reach PostgreSQL and both Key Vaults. The production example
  uses private PostgreSQL access: an operator firewall allowlist alone does
  not enable it. Arrange VNet routing and private DNS (for example a connected
  operator host) before bootstrap. The bundle creates no workstation VPN.
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
: "${DEPLOY_REF:?set the source commit used to build the selected image}"
: "${GHCR_DIGEST:?set the published image sha256 digest}"
: "${ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS:?set the transport ceiling; at most 240 on this platform}"
test "$ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS" -le 240

CANDIDATE_SHA=$(git rev-parse "${DEPLOY_REF}^{commit}")
DEPLOYMENT_CONFIG_SHA=$(git rev-parse HEAD)
test -z "$(git status --porcelain)"
OPERATOR_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/elspeth/azure-container-apps/${RESOURCE_GROUP}"
mkdir -p "$OPERATOR_DIR"
chmod 700 "$OPERATOR_DIR"
export WORKLOAD_PARAMETERS="$OPERATOR_DIR/workload-${CANDIDATE_SHA}.parameters.json"
export APPLICATION_PARAMETERS="$OPERATOR_DIR/application.json"
export SECRET_VERSION_DIR="$OPERATOR_DIR"
az account set --subscription "$AZURE_SUBSCRIPTION_ID"
az account show --query '{subscription:id,tenant:tenantId,user:user.name}'
```

## 1. Select and prove the identity

```bash
az group create --name "$RESOURCE_GROUP" --location "$AZURE_LOCATION" --tags elspeth.stack=cold-install
az acr show --ids "$ACR_RESOURCE_ID" \
  --query '{name:name,loginServer:loginServer,roleAssignmentMode:roleAssignmentMode,publicNetworkAccess:publicNetworkAccess,networkRuleSet:networkRuleSet}'
az acr config authentication-as-arm show --registry "${ACR_LOGIN_SERVER%%.*}"
```

Require the selected login server, role assignment mode `LegacyRegistryPermissions`
(the portal's RBAC Registry Permissions), and ARM audience authentication
`enabled`. The bundle grants `AcrPull`; an ABAC registry does not honor that
role. Arrange the right repository-reader role before using an ABAC registry.
The fresh ACA VNet also needs access to the registry's login and data endpoints;
the bundle does not connect it to an existing ACR private endpoint or private
DNS. Inspect these settings before changing any shared registry configuration.
Microsoft documents the [ABAC role behavior](https://learn.microsoft.com/en-us/azure/container-registry/container-registry-rbac-abac-repository-permissions)
and the [ARM audience requirement for managed-identity pulls](https://learn.microsoft.com/en-us/azure/container-apps/managed-identity-image-pull).

## 2. Deploy the environment

The PostgreSQL administrator password is required; the tracked empty fallback
is only a compilation fixture. Copy the environment parameter example outside
the checkout, change its `using` directive to the absolute path of this
checkout's `environment.bicep`, and replace `containerRegistryResourceId` with
the verified `ACR_RESOURCE_ID`. Set the actual administrator login and network
configuration in that local file. Both the operator's SQL client and Key Vault
client need access: run from a host on the private network (with private DNS).
For a deliberate public bootstrap, public access and matching firewall rules
must both be configured; an IP allowlist does not override disabled public access.
Grant the operator Key Vault Secrets Officer on both vaults separately from
the identities' Secrets User roles. Never grant the runtime identity access
to the schema-owner vault or secret-write permission. Keep public database
access disabled after bootstrap.

```bash
: "${ELSPETH_POSTGRES_ADMIN_PASSWORD:?export the administrator password without printing it}"
: "${ENVIRONMENT_PARAMETERS:?absolute path to the completed local environment bicepparam file}"
bicep build-params "$ENVIRONMENT_PARAMETERS" --outfile "$OPERATOR_DIR/environment.parameters.json"
jq -e --arg registry "$ACR_RESOURCE_ID" '
  .parameters.containerRegistryResourceId.value == $registry and
  (.parameters.containerRegistryResourceId.value | contains("00000000") | not) and
  (.parameters.postgresAdministratorPassword.value | length > 0)
' "$OPERATOR_DIR/environment.parameters.json" >/dev/null
az deployment group what-if --resource-group "$RESOURCE_GROUP" \
  --template-file deploy/azure-container-apps/environment.bicep \
  --parameters "@$OPERATOR_DIR/environment.parameters.json"
az deployment group create --name elspeth-environment --resource-group "$RESOURCE_GROUP" \
  --template-file deploy/azure-container-apps/environment.bicep \
  --parameters "@$OPERATOR_DIR/environment.parameters.json" \
  --query properties.outputs >"$OPERATOR_DIR/environment-outputs.json"
rm -- "$OPERATOR_DIR/environment.parameters.json"
unset ELSPETH_POSTGRES_ADMIN_PASSWORD
```

The environment deployment creates the custom virtual network the NFS mount
requires, with an NSG allowing 445 and 2049 to the storage private endpoint
(facts §3.2); the Premium `FileStorage` account with `supportsHttpsTrafficOnly:
false` (Container Apps cannot mount an NFS share that requires encryption in
transit), the NFS share with `rootSquash: NoRootSquash`, and the
`privatelink.file.core.windows.net` zone; the Flexible Server (`version 17`,
password authentication enabled, public network access disabled, private
endpoint plus `privatelink.postgres.database.azure.com`); two Key Vaults (RBAC);
the Log Analytics workspace; and separate runtime and schema-owner identities
with `AcrPull` on the existing registry. Both identities read application keys
from the runtime vault; only the schema-owner identity reads owner database
URLs from the schema-owner vault. The runtime identity has the blob role on
the payload container.

Before uploading secrets, grant the selected operator access to both vaults:

```bash
: "${OPERATOR_OBJECT_ID:?object id of the signed-in operator principal}"
for output_name in keyVaultName schemaOwnerKeyVaultName; do
  vault_name=$(jq -er --arg key "$output_name" '.[$key].value' "$OPERATOR_DIR/environment-outputs.json")
  vault_id=$(az keyvault show --name "$vault_name" --query id --output tsv)
  az role assignment create --assignee-object-id "$OPERATOR_OBJECT_ID" \
    --role 'Key Vault Secrets Officer' --scope "$vault_id" --output none
done
```

## 3. Publish the image by digest

```bash
test "$(docker buildx imagetools inspect "ghcr.io/dta-au/elspeth@${GHCR_DIGEST}" \
  --format '{{.Manifest.Digest}}')" = "$GHCR_DIGEST"
docker pull --platform linux/amd64 "ghcr.io/dta-au/elspeth@${GHCR_DIGEST}"
test "$(docker image inspect "ghcr.io/dta-au/elspeth@${GHCR_DIGEST}" \
  --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" = "$CANDIDATE_SHA"
docker run --rm --platform linux/amd64 "ghcr.io/dta-au/elspeth@${GHCR_DIGEST}" --version
docker run --rm --platform linux/amd64 "ghcr.io/dta-au/elspeth@${GHCR_DIGEST}" health
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
The source label is an identity check, not a signature. For a signed release,
verify the GHCR signature using the release workflow's identity before the
copy. Separate Cosign artifacts are not automatically copied with the image
index; verify an ACR signature only if the release process signed that ACR
reference. An unsigned RC proceeds with the digest and smoke checks above.

## 4. Store secrets in Key Vault

First create the application database roles on the empty Flexible Server.
The environment template creates databases and the administrator, not these
roles. From the private-network operator host, configure `PGHOST` from
`postgresFqdn`, `PGUSER` to the administrator, `PGDATABASE=postgres`, and
`PGSSLMODE=verify-full` with the operator's `PGSSLROOTCERT` trust bundle.
Supply the administrator and new role passwords through the environment:

```bash
: "${PGHOST:?set the provisioned postgresFqdn}"
: "${PGUSER:?set the Flexible Server administrator}"
: "${PGPASSWORD:?export the administrator password}"
: "${PGSSLROOTCERT:?set the operator PostgreSQL CA bundle path}"
: "${ELSPETH_SCHEMA_OWNER_PASSWORD:?export a fresh schema-owner password}"
: "${ELSPETH_RUNTIME_PASSWORD:?export a different fresh runtime password}"
export PGDATABASE=postgres PGSSLMODE=verify-full
psql --no-psqlrc --set=ON_ERROR_STOP=1 \
  --file deploy/azure-container-apps/scripts/bootstrap-roles.sql \
  >"$OPERATOR_DIR/bootstrap-roles.log" 2>&1
```

This cold-only script fails on existing roles. It makes the schema owner the
database owner and grants the runtime only connection, schema usage and
default data/sequence/function privileges on objects created by that owner.
Run the schema-init doctor as that owner. Acceptance creates separate a/b
runtime roles with the same grants before its role-specific Jobs.

Create each secret as a versioned Key Vault secret and record the version id;
the workload parameters reference `https://<vault>.vault.azure.net/secrets/<name>/<version>`.
Required names: `elspeth-session-db-url-runtime`, `elspeth-landscape-url-runtime`,
`elspeth-session-db-url-schema-owner`, `elspeth-landscape-url-schema-owner`,
`elspeth-secret-key`, `elspeth-shareable-link-signing-key`,
`elspeth-fingerprint-key`, the composer endpoint key(s) and
`elspeth-operator-metrics-bearer-token`. PostgreSQL URLs use
`sslmode=verify-full&sslrootcert=system`: the runtime image's CA store carries
both Azure roots (facts §4.4). Never print a secret value.
Store URL-escaped role passwords in those URLs; never put raw passwords into
the shell command line. Clear `PGPASSWORD`, `ELSPETH_SCHEMA_OWNER_PASSWORD`
and `ELSPETH_RUNTIME_PASSWORD` after storing their Key Vault versions.

One executable handoff is a mode-0700 operator-local `SECRET_VALUE_DIR` with
one mode-0600 UTF-8 file per required secret, named exactly as above. Populate
these through the operator's secret manager; the following command uploads
each file and captures only its version ID. Keep the directory outside Git:

```bash
: "${SECRET_VALUE_DIR:?absolute directory containing the selected secret value files}"
KEY_VAULT_NAME=$(jq -er '.keyVaultName.value' "$OPERATOR_DIR/environment-outputs.json")
SCHEMA_OWNER_KEY_VAULT_NAME=$(jq -er '.schemaOwnerKeyVaultName.value' "$OPERATOR_DIR/environment-outputs.json")
for secret_name in elspeth-session-db-url-runtime elspeth-landscape-url-runtime \
  elspeth-session-db-url-schema-owner elspeth-landscape-url-schema-owner \
  elspeth-secret-key elspeth-shareable-link-signing-key elspeth-fingerprint-key \
  elspeth-operator-metrics-bearer-token; do
  test -s "$SECRET_VALUE_DIR/$secret_name"
  case "$secret_name" in
    *-schema-owner) secret_vault=$SCHEMA_OWNER_KEY_VAULT_NAME ;;
    *) secret_vault=$KEY_VAULT_NAME ;;
  esac
  az keyvault secret set --vault-name "$secret_vault" --name "$secret_name" \
    --file "$SECRET_VALUE_DIR/$secret_name" --encoding utf-8 --query id --output tsv \
    >"$OPERATOR_DIR/$secret_name.version"
done
unset PGPASSWORD ELSPETH_SCHEMA_OWNER_PASSWORD ELSPETH_RUNTIME_PASSWORD
```

Only the two schema-owner database URLs belong in the schema-owner vault.
Upload the primary/advisor endpoint keys and SSO/provider secrets the same way
to the runtime vault, recording each returned ID in
`$SECRET_VERSION_DIR/<secret-name>.version`. The tracked application example
uses native Azure OpenAI authentication; upload its three private files:

```bash
for secret_name in elspeth-azure-api-key elspeth-sso-client-secret elspeth-sso-transaction-secret; do
  test -s "$SECRET_VALUE_DIR/$secret_name"
  az keyvault secret set --vault-name "$KEY_VAULT_NAME" --name "$secret_name" \
    --file "$SECRET_VALUE_DIR/$secret_name" --encoding utf-8 --query id --output tsv \
    >"$SECRET_VERSION_DIR/$secret_name.version"
done
```

Only if choosing custom OpenAI-compatible endpoints instead, upload both keys
and set the matching URLs in the application file:

```bash
export COMPOSER_ENDPOINT_SECRET_NAME=elspeth-composer-endpoint-key
export COMPOSER_ADVISOR_ENDPOINT_SECRET_NAME=elspeth-composer-advisor-endpoint-key
for secret_name in "$COMPOSER_ENDPOINT_SECRET_NAME" "$COMPOSER_ADVISOR_ENDPOINT_SECRET_NAME"; do
  test -s "$SECRET_VALUE_DIR/$secret_name"
  az keyvault secret set --vault-name "$KEY_VAULT_NAME" --name "$secret_name" \
    --file "$SECRET_VALUE_DIR/$secret_name" --encoding utf-8 --query id --output tsv \
    >"$SECRET_VERSION_DIR/$secret_name.version"
done
```

### Complete application settings before creating Jobs

Prepare `$APPLICATION_PARAMETERS` outside Git and replace every placeholder.
The example uses Entra login and native Azure OpenAI for both Composer roles
and pipeline execution. Bind its secret URLs to the versions just uploaded:

```bash
test ! -e "$APPLICATION_PARAMETERS"
ENVIRONMENT_DOMAIN=$(jq -er '.environmentDefaultDomain.value' "$OPERATOR_DIR/environment-outputs.json")
FQDN="elspeth-web.${ENVIRONMENT_DOMAIN}"
PUBLIC_BASE_URL="https://${FQDN}"
jq --rawfile azure "$SECRET_VERSION_DIR/elspeth-azure-api-key.version" \
  --rawfile client "$SECRET_VERSION_DIR/elspeth-sso-client-secret.version" \
  --rawfile transaction "$SECRET_VERSION_DIR/elspeth-sso-transaction-secret.version" \
  --arg public_base_url "$PUBLIC_BASE_URL" '
  .extraSecrets |= map(
    if .name == "azure-api-key" then .keyVaultUrl = ($azure | rtrimstr("\n"))
    elif .name == "sso-client-secret" then .keyVaultUrl = ($client | rtrimstr("\n"))
    elif .name == "sso-transaction-secret" then .keyVaultUrl = ($transaction | rtrimstr("\n"))
    else . end) |
  .extraEnvironment |= map(
    if .name == "ELSPETH_WEB__PUBLIC_BASE_URL" then .value = $public_base_url
    else . end)
' deploy/azure-container-apps/application.example.json >"$APPLICATION_PARAMETERS"
chmod 600 "$APPLICATION_PARAMETERS"
```

This derives the public origin before creating the web app, using the
environment output and the bundle's `elspeth-web` app name. If changing the
app name, use that same name here and in the workload parameters. The
[external ACA FQDN format](https://learn.microsoft.com/en-us/azure/container-apps/connect-apps#container-app-location-fqdn)
is `<app-name>.<environment-default-domain>`. Register
`https://<that-FQDN>/api/auth/sso/callback` as the Entra application's **Web**
redirect URI. The backend route is defined by `/api/auth` plus `/sso/callback`
in `src/elspeth/web/auth/routes.py`; the SPA's `/#/auth/callback` is a separate
post-login redirect and is not the IdP callback. After deployment, compare
the actual ingress FQDN with this configured origin.

Edit the remaining nonsecret values in that local file. It is a
flat JSON object mapping Bicep parameter names to values, without an ARM
`parameters` envelope. Complete these inputs:

- `composerMaxCompositionTurns`, `composerMaxDiscoveryTurns`,
  `composerTimeoutSeconds`, `composerRateLimitPerMinute`: select actual limits.
  The timeout must fit below the transport ceiling with the configured headroom.
  These parameters flow to web **and both doctor Jobs**; web-only
  `extraEnvironment` cannot fix doctor startup.
- `composerModel` and `composerAdvisorModel`, plus paired
  `composerEndpointBaseUrl` / primary key and `composerAdvisorEndpointBaseUrl` /
  advisor key. Both roles need valid model access. Never set a key without its
  corresponding URL. The native Azure example leaves both custom URLs empty
  and uses `AZURE_API_BASE`, `AZURE_API_VERSION`, `AZURE_API_KEY`, and
  `azure/<deployment-name>` Composer models instead; leave both endpoint
  secret-name variables unset for that setup.
- `authProvider`: choose the actual nonlocal production provider and complete
  its SSO client, issuer and transaction-secret settings in `extraEnvironment`.
  Set `registrationMode=closed`, configure the exact public HTTPS origin and
  register the matching callback with the IdP. Supply per-user token/storage
  quota defaults. Local authentication stores SQLite `auth.db`, which must
  not be put on the shared NFS mount.
- `extraSecrets`: define each SSO/provider secret as `{name, keyVaultUrl}`
  using its captured versioned URL. Reference that ACA name from
  `extraEnvironment` with `secretRef`; an environment reference alone does not
  create a secret. Keep secret values out of both parameter files.
- `ELSPETH_WEB__LLM_PROFILES` in `extraEnvironment`: define at least the
  standard pipeline profile, including its actual provider/model and
  credential reference. Set `ELSPETH_WEB__DEFAULT_LLM_PROFILE` to that alias.
  Its credential environment variable must have a matching `secretRef` and
  be in `ELSPETH_WEB__SERVER_SECRET_ALLOWLIST` for server credentials. Composer
  endpoint keys alone do not configure pipeline transforms. The
  [LLM profile reference](../reference/environment-variables.md#operator-llm-profiles-elspeth_web__llm_profiles)
  defines provider-specific fields.

The first administrator can be selected with
`ELSPETH_WEB__SSO_ADMIN_SUBJECTS` (a JSON array of exact IdP subject IDs). It
bootstraps at first login only while no active human administrator exists.
Alternatively run `elspeth composer users bootstrap-admin PROVIDER SUBJECT
--username USERNAME --note TEXT` from a configured runtime container after
schema initialization. Neither mechanism grants an author/run workload role:
use the administrator UI/API to grant the intended user's explicit workload
role before testing Composer and execution.

## 5. Provision storage

Select and verify a root provisioner image digest independently of the runtime
image, then materialise the workload parameters from environment outputs and
the secret version IDs created in step 4. The resolver reads IDs only and
rejects unresolved sample values. Retain this file for redeployment; it pins
secret versions, identity, storage and production settings outside Git. Review
any site-specific `extraEnvironment`, scale or sizing changes in that local
file, then validate again before deploying.

```bash
: "${PROVISION_STORAGE_IMAGE:?export the verified digest-pinned root provisioner image}"
: "${APPLICATION_PARAMETERS:?absolute path to completed application settings JSON}"
: "${SECRET_VERSION_DIR:?directory containing captured secret version files}"
export CANDIDATE_SHA CANDIDATE_IMAGE PROVISION_STORAGE_IMAGE
export APPLICATION_PARAMETERS SECRET_VERSION_DIR
bash deploy/azure-container-apps/scripts/resolve-workload-parameters.sh \
  "$OPERATOR_DIR/environment-outputs.json" "$WORKLOAD_PARAMETERS"
jq -e -f deploy/azure-container-apps/scripts/validate-workload-parameters.jq "$WORKLOAD_PARAMETERS" >/dev/null
az deployment group what-if --resource-group "$RESOURCE_GROUP" \
  --template-file deploy/azure-container-apps/workload.bicep \
  --parameters "@$WORKLOAD_PARAMETERS" --parameters deployWebApp=false
az deployment group create --name elspeth-jobs --resource-group "$RESOURCE_GROUP" --mode Incremental \
  --template-file deploy/azure-container-apps/workload.bicep \
  --parameters "@$WORKLOAD_PARAMETERS" --parameters deployWebApp=false
bash deploy/azure-container-apps/scripts/run-job.sh "$RESOURCE_GROUP" provision-storage
```

The Job has no managed identity and runs a digest-pinned public root image
(the runtime image is `USER 1654` and
the platform offers no `runAsUser`) and creates `/mnt/elspeth/data`,
`/mnt/elspeth/data/blobs` and `/mnt/elspeth/payloads` as `1654:1654`, mode
`0700`. The helper waits for `Succeeded` on the exact started execution and
stops on failure or timeout. Jobs exist before this first execution; no app
resource is deployed in the Jobs-only stage. Use Incremental mode throughout:
Complete mode could delete an existing app when `deployWebApp=false`.

> **LIVE:** the share root's ownership and mode after creation.

## 6. Initialize schemas

```bash
bash deploy/azure-container-apps/scripts/run-job.sh "$RESOURCE_GROUP" doctor-schema-init
```

`doctor-schema-init` runs `elspeth doctor deployment --init-schema --json`
with the schema-owner identity and URLs from its dedicated vault. The web app
and runtime doctor never attach that identity. `--init-schema` initializes only `MISSING` or
repairable schemas; `STALE` is a stop, not a migration.

## 7. Prove runtime credentials

```bash
bash deploy/azure-container-apps/scripts/run-job.sh "$RESOURCE_GROUP" doctor-runtime
```

`doctor-runtime` runs `elspeth doctor deployment --json` with the runtime
URLs; every reported check must be OK, including schema and storage checks.
The ACA collector does not emit `session_tls` or `landscape_tls`; a successful
Job does not establish those named checks ran. The configured runtime URLs
must retain `sslmode=verify-full&sslrootcert=system`. From the connected
operator host, test each runtime database with `psql`, using separate `PGHOST`,
`PGUSER`, `PGPASSWORD`, `PGDATABASE`, `PGSSLMODE=verify-full` and a trusted
`PGSSLROOTCERT` bundle. This query must return `t` and the negotiated protocol:
`SELECT ssl, version FROM pg_stat_ssl WHERE pid = pg_backend_pid();`.
That proves the operator's connection; runtime Jobs prove connectivity using
their configured URLs. Retrieve the report from Log
Analytics by execution name (ingestion lags by minutes, facts §5.1).

## 8. Deploy the workload

```bash
az deployment group create --name "elspeth-workload-${CANDIDATE_SHA:0:12}" --resource-group "$RESOURCE_GROUP" \
  --template-file deploy/azure-container-apps/workload.bicep \
  --parameters "@$WORKLOAD_PARAMETERS" --parameters deployWebApp=true --mode Incremental
```

Production shape: `activeRevisionsMode: Single`, `stickySessions.affinity:
sticky`, `minReplicas: 2`, `maxReplicas: 4`, `terminationGracePeriodSeconds:
60`, startup probe `/api/health` (15 s × 10), liveness `/api/health`
(30 s × 3), readiness `/api/ready` (10 s × 3), environment
`ELSPETH_WEB__DEPLOYMENT_TARGET=azure-container-apps`,
`ELSPETH_WEB__DEPLOYMENT_STATE_MODE=external-postgresql`,
`ELSPETH_WEB__HOST=0.0.0.0`, `WEB_CONCURRENCY=1`, `ELSPETH_WEB__LOG_JSON=true`.
`AZURE_CLIENT_ID` is set from the required `identityClientId` parameter to
select the attached runtime identity for Azure plugins.

## 9. Verify

```bash
FQDN=$(az containerapp show --name elspeth-web --resource-group "$RESOURCE_GROUP" \
  --query properties.configuration.ingress.fqdn --output tsv)
az containerapp revision list --name elspeth-web --resource-group "$RESOURCE_GROUP" \
  --query "[?properties.active].{name:name,traffic:properties.trafficWeight,state:properties.runningState}"
az containerapp replica list --name elspeth-web --resource-group "$RESOURCE_GROUP" \
  --revision "elspeth-web--r${CANDIDATE_SHA:0:12}" --query '[].name'
curl --silent --fail-with-body "https://${FQDN}/api/health"
curl --silent --fail-with-body "https://${FQDN}/api/ready" | jq -e '.ready == true'
curl --silent --fail-with-body "https://${FQDN}/api/system/status" \
  | jq '{deployment_target, frontend_build, instance_id, composer_available, composer_missing_keys, tutorial_ready}'
```

Require exactly one active revision at 100 % with the candidate digest, `N`
running replicas, HTTP 200 on both probes and the expected
`/api/system/status` facts. Record the resource group, revision name and
digest in operator-local notes under
`~/.local/state/elspeth/azure-container-apps/`, not in a tracked file.
Also record `DEPLOYMENT_CONFIG_SHA` separately from the image source SHA.
Sign in as the chosen first administrator and verify `/api/auth/me`; admit
the intended user and grant the required workload role. As that user, make a
real Composer request, save the pipeline, execute a small input through the
configured standard LLM profile, and inspect output and audit history.
Require `composer_available=true`, no missing keys and `tutorial_ready=true`
when offering the tutorial. Public health probes alone do not prove this flow.

## 10. Optional: grant Azure AI Search access for RAG retrieval

The `azure_ai_search` transform queries an existing Azure AI Search index; the default image already carries it
(`INSTALL_EXTRAS=all` includes `azure-identity`). The bundle does not create a
search service or an index. Keeping to "no static credential in any
container", the recommended credential on this target is the web app's
user-assigned identity, not a Search API key.

Enable role-based access on the search service and grant the identity read
access to index data:

```bash
: "${SEARCH_SERVICE_NAME:?set the existing search service name}"
: "${SEARCH_RESOURCE_GROUP:?set the resource group of the search service}"
IDENTITY_PRINCIPAL_ID=$(jq -er '.identityPrincipalId.value' "$OPERATOR_DIR/environment-outputs.json")
IDENTITY_CLIENT_ID=$(jq -er '.identityClientId.value' "$OPERATOR_DIR/environment-outputs.json")
SEARCH_RESOURCE_ID=$(az search service show --name "$SEARCH_SERVICE_NAME" \
  --resource-group "$SEARCH_RESOURCE_GROUP" --query id --output tsv)
az search service update --name "$SEARCH_SERVICE_NAME" --resource-group "$SEARCH_RESOURCE_GROUP" \
  --auth-options aadOrApiKey --aad-auth-failure-mode http401WithBearerChallenge --output none
az role assignment create --assignee-object-id "$IDENTITY_PRINCIPAL_ID" \
  --assignee-principal-type ServicePrincipal \
  --role 'Search Index Data Reader' --scope "$SEARCH_RESOURCE_ID" --output none
printf 'client_id: %s\n' "$IDENTITY_CLIENT_ID"
```

Declare the service to the web app as an operator profile. It is not a secret,
so it travels as a plain `extraEnvironment` entry in the operator-local
application parameter file (`$APPLICATION_PARAMETERS`); `indexes` is mandatory, and `"any"` is the written decision to open every
index on the service to web authors. `client_id` is required here: the identity
is user-assigned, and the transform uses `ManagedIdentityCredential`, which does
not read `AZURE_CLIENT_ID` on Container Apps.

```json
{"name": "ELSPETH_WEB__AZURE_SEARCH_PROFILES",
 "value": "[{\"alias\":\"policies\",\"endpoint\":\"https://<service>.search.windows.net\",\"auth\":\"managed_identity\",\"client_id\":\"<identityClientId>\",\"indexes\":[\"<index>\"]}]"}
```

Add `transform:azure_ai_search` to `ELSPETH_WEB__PLUGIN_ALLOWLIST`, which is
another `extraEnvironment` entry on this target. A web author then selects
`profile: policies` and an index the profile lists; the endpoint and identity
never appear in an authored pipeline, and `endpoint`, `api_key`,
`use_managed_identity`, `client_id` and `api_version` are refused there.
Several services are several entries in the array. A managed-identity profile
needs no `ELSPETH_WEB__SECRET_WIRING_ALLOWLIST` rule; neither does an `api_key`
profile, whose server secret the profile injects rather than the author wiring
it. If a query key is unavoidable, hold it in Key Vault as an `extraSecrets`
entry, expose it through `extraEnvironment` with `secretRef`, name that
variable as the profile's `credential_ref`, and list it in
`ELSPETH_WEB__SERVER_SECRET_ALLOWLIST`; a profile whose secret does not resolve
reads as unavailable instead of failing start-up.

Index field mapping, search modes and score ranges are in
[`examples/azure_search_rag`](../../examples/azure_search_rag/README.md).

Limits on this target:

- **No private endpoint for the search service.** The transform resolves the
  endpoint and refuses private addresses before every request and before the
  first row, so a `privatelink.search.windows.net` answer is blocked as SSRF.
  Leave the service on its public endpoint and restrict it with the Search IP
  firewall. The environment's outbound address is not static without a NAT
  gateway, which the bundle does not create.
- A run that stops before the first row with `pre_flight_failed`
  (`RuntimePreflightFailedError`) and `Authentication failed ... HTTP 403`
  means the role assignment has not propagated or role-based access is off; a
  missing index reports `not found`, an empty one `is empty`.

## Troubleshooting

### The runtime doctor fails TLS or authentication

For a `verify-full` hostname failure, use the Flexible Server's normal
`<server>.postgres.database.azure.com` hostname and fix its private DNS route
and trusted CA bundle. Keep `sslmode=verify-full&sslrootcert=system` in runtime
URLs; weakening the mode with system roots is rejected by libpq. Check role
login state, database privileges and the pinned Key Vault version for an
authentication failure. The app reads the named version, not "latest".
[libpq documents the system-root requirement](https://www.postgresql.org/docs/16/libpq-connect.html#LIBPQ-CONNECT-SSLROOTCERT):
`sslrootcert=system` requires `verify-full` and rejects weaker modes.

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

Both production Key Vaults have purge protection on and cannot be purged;
record both soft-delete tombstones. Registry images are not owned by the resource
group and are left in place.
