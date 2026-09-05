# Runbook: Redeploy an existing Azure Container Apps service

Publish one immutable ELSPETH image and roll it onto an existing container
app as a new revision. This is the everyday image/config replacement path. It
does not create or destroy Azure infrastructure.

For a first installation use
[Azure Container Apps cold install](azure-container-apps-cold-install.md); for
the release-specific replica > 1 acceptance program use
[Full disposable Azure Container Apps acceptance](azure-container-apps-deployment.md).
Every platform literal below is measured in the
[platform facts](../plans/2026-09-05-phase6b-azure-container-apps-platform-facts.md).

> **Status.** Skeleton prepared by Phase 6b before the first live run; steps
> marked **LIVE** are completed from the 6b-7 acceptance. Until the sanitized
> receipt at `docs/operator/evidence/azure-container-apps/0.8.0.json` exists,
> this is not a support claim.

## Safety contract

- Discover the subscription, resource group, environment, app, active
  revision, image digest, identity and registry from live Azure state; never
  from memory.
- Build or copy a clean, exact Git commit and deploy the registry
  `@sha256:` digest, never a tag.
- Change only the image and the release identity values. Preserve the
  storage mount, every Key Vault secret reference and version, probes, scale
  settings, identity and ingress settings.
- Run the `doctor` Job with the candidate digest before mutating the app.
- Keep `activeRevisionsMode: Single`: the platform activates the candidate
  revision, waits for its replicas to pass startup and readiness, shifts
  traffic and deprovisions the previous revision. The overlap is an
  equal-key overlap the session fences serialise; the epoch sentinel refuses
  an unequal-epoch candidate before it is ready. Both databases stay on Azure
  Database for PostgreSQL Flexible Server; **Azure Files carries no database.**
- Treat `/api/health` as liveness and `/api/ready` as the traffic gate.
- Never print secret values or the resolved secret references.

Rollback is a compatibility decision, not a reflex: only when the
compatibility record says `rollback_permitted: true`, which is never the case
for a Scenario A install. Otherwise keep the candidate and repair forward.

## Prerequisites

- Azure CLI with the `containerapp` extension, `jq`, `curl`, Docker Buildx,
  `cosign`, and an authenticated `az login` context with `Contributor` on the
  resource group and `AcrPush` (or an existing copy) on the registry.
- A clean source checkout; Python imports bound to it with `PYTHONPATH`.
- The current app is stable: one active revision at 100 % with all replicas
  `Running`.
- The schemas are already current; `--init-schema` is only for an explicitly
  approved fresh database reported `MISSING`, and `STALE` is a stop.

```bash
set -Eeuo pipefail
umask 077
export AZURE_CORE_OUTPUT=json
: "${AZURE_SUBSCRIPTION_ID:?set the subscription id}"
: "${RESOURCE_GROUP:?set the resource group}"
: "${CONTAINER_APP:?set the container app name}"
: "${DEPLOY_REF:?set the exact branch, tag, or commit to deploy}"
: "${ELSPETH_BASE_URL:?set the exact public HTTPS origin without a trailing slash}"

CANDIDATE_SHA=$(git rev-parse "${DEPLOY_REF}^{commit}")
test "$(git rev-parse HEAD)" = "$CANDIDATE_SHA"
test -z "$(git status --porcelain)"
az account set --subscription "$AZURE_SUBSCRIPTION_ID"
```

## 1. Capture the live deployment

```bash
az containerapp show --name "$CONTAINER_APP" --resource-group "$RESOURCE_GROUP" >live-app.json
PREVIOUS_REVISION=$(jq -r '.properties.latestReadyRevisionName' live-app.json)
PREVIOUS_IMAGE=$(jq -r '.properties.template.containers[] | select(.name=="elspeth-web") | .image' live-app.json)
ACR_LOGIN_SERVER=$(jq -r '.properties.configuration.registries[0].server' live-app.json)
jq '{mode: .properties.configuration.activeRevisionsMode,
     affinity: .properties.configuration.ingress.stickySessions.affinity,
     scale: .properties.template.scale,
     grace: .properties.template.terminationGracePeriodSeconds}' live-app.json
az containerapp revision list --name "$CONTAINER_APP" --resource-group "$RESOURCE_GROUP" \
  --query "[?properties.active].{name:name,traffic:properties.trafficWeight,state:properties.runningState}"
```

Require `activeRevisionsMode == "Single"`, exactly one active revision at
100 %, and a `@sha256:` image reference. Stop on anything else.

## 2. Verify and publish the exact source

```bash
GHCR_DIGEST=$(docker buildx imagetools inspect "ghcr.io/dta-au/elspeth:sha-${CANDIDATE_SHA}" \
  --format '{{.Manifest.Digest}}')
az acr login --name "${ACR_LOGIN_SERVER%%.*}"
docker buildx imagetools create --tag "${ACR_LOGIN_SERVER}/elspeth:sha-${CANDIDATE_SHA}" \
  "ghcr.io/dta-au/elspeth@${GHCR_DIGEST}"
ACR_DIGEST=$(az acr manifest show-metadata "${ACR_LOGIN_SERVER}/elspeth:sha-${CANDIDATE_SHA}" \
  --query digest --output tsv)
test "$ACR_DIGEST" = "$GHCR_DIGEST"
cosign verify "${ACR_LOGIN_SERVER}/elspeth@${ACR_DIGEST}" \
  --certificate-identity-regexp '^https://github.com/dta-au/elspeth/' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com >/dev/null
CANDIDATE_IMAGE="${ACR_LOGIN_SERVER}/elspeth@${ACR_DIGEST}"
```

## 3. Review the change with what-if

```bash
az deployment group what-if --resource-group "$RESOURCE_GROUP" \
  --template-file deploy/azure-container-apps/workload.bicep \
  --parameters deploy/azure-container-apps/workload.production.bicepparam \
  --parameters image="$CANDIDATE_IMAGE" revisionSuffix="${CANDIDATE_SHA:0:12}"
```

The only expected change is the container image and the revision suffix.
Any change to secrets, volumes, probes, scale or ingress is a stop.

## 4. Run the doctor Job with the candidate digest

```bash
az containerapp job update --name doctor-runtime --resource-group "$RESOURCE_GROUP" --image "$CANDIDATE_IMAGE"
EXECUTION=$(az containerapp job start --name doctor-runtime --resource-group "$RESOURCE_GROUP" --query name --output tsv)
az containerapp job execution show --name doctor-runtime --resource-group "$RESOURCE_GROUP" \
  --job-execution-name "$EXECUTION" --query properties.status --output tsv
```

The Job runs `elspeth doctor deployment --json`; require `Succeeded`. A
schema check of `STALE` means the candidate needs a compatibility decision
first; do not proceed to step 5.

## 5. Deploy the candidate revision

```bash
az containerapp update --name "$CONTAINER_APP" --resource-group "$RESOURCE_GROUP" \
  --image "$CANDIDATE_IMAGE" --revision-suffix "${CANDIDATE_SHA:0:12}" \
  --set-env-vars "ELSPETH_WEB__RELEASE_SOURCE_SHA=${CANDIDATE_SHA}"
```

## 6. Prove the rollout

```bash
az containerapp revision list --name "$CONTAINER_APP" --resource-group "$RESOURCE_GROUP" \
  --query "[?properties.active].{name:name,traffic:properties.trafficWeight,state:properties.runningState,image:properties.template.containers[0].image}"
az containerapp replica list --name "$CONTAINER_APP" --resource-group "$RESOURCE_GROUP" \
  --revision "${CONTAINER_APP}--${CANDIDATE_SHA:0:12}" --query '[].{name:name,state:properties.runningState}'
```

Require exactly one active revision at 100 % whose image is
`CANDIDATE_IMAGE`, `N` replicas `Running`, and the previous revision
inactive. The platform's own readiness wait is the rollout primitive; the
checks above are the proof.

## 7. Prove public behaviour and identity

```bash
curl --silent --fail-with-body "$ELSPETH_BASE_URL/api/health"
curl --silent --fail-with-body "$ELSPETH_BASE_URL/api/ready" | jq -e '.ready == true'
curl --silent --fail-with-body --dump-header - "$ELSPETH_BASE_URL/api/system/status" \
  | grep -i '^X-Elspeth-Instance:'
curl --silent --fail-with-body "$ELSPETH_BASE_URL/api/system/status" \
  | jq '{deployment_target, frontend_build, instance_id}'
```

Require HTTP 200 on both probes, an `X-Elspeth-Instance` header (6b-3) and
`deployment_target: azure-container-apps`. Then run the authenticated flow
appropriate to the change.

> **LIVE:** the console-log query by revision name that shows no new
> unhandled startup or runtime failure.

## Rollback

Rollback is permitted only when the compatibility record for this candidate
says `rollback_permitted: true`, meaning the previous image understands the
current schemas. For a Scenario A install that is `false` by the record's
own rule: keep the candidate and repair forward. When it is permitted:

```bash
az containerapp revision activate --name "$CONTAINER_APP" --resource-group "$RESOURCE_GROUP" --revision "$PREVIOUS_REVISION"
az containerapp ingress traffic set --name "$CONTAINER_APP" --resource-group "$RESOURCE_GROUP" \
  --revision-weight "${PREVIOUS_REVISION}=100"
az containerapp revision deactivate --name "$CONTAINER_APP" --resource-group "$RESOURCE_GROUP" \
  --revision "${CONTAINER_APP}--${CANDIDATE_SHA:0:12}"
```

Then repeat step 6 and step 7 against the previous revision.
