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

> **Status.** The implemented ACA slice received desktop acceptance:
> `elspeth-5ec3befc1a` closed on 2026-09-10 by operator ruling. No live cloud
> acceptance is claimed. This is an executable operator procedure; steps marked
> **LIVE** require measurements during execution. A future acceptance receipt at
> `docs/operator/evidence/azure-container-apps/0.8.0.json` is no longer a tracker
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
: "${WORKLOAD_PARAMETERS:?absolute path to the concrete workload ARM JSON retained from cold install}"
OPERATOR_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/elspeth/azure-container-apps/${RESOURCE_GROUP}"
mkdir -p "$OPERATOR_DIR"
chmod 700 "$OPERATOR_DIR"
az account set --subscription "$AZURE_SUBSCRIPTION_ID"
```

## 1. Capture the live deployment

```bash
az containerapp show --name "$CONTAINER_APP" --resource-group "$RESOURCE_GROUP" >"$OPERATOR_DIR/live-app.json"
PREVIOUS_REVISION=$(jq -r '.properties.latestReadyRevisionName' "$OPERATOR_DIR/live-app.json")
PREVIOUS_IMAGE=$(jq -r '.properties.template.containers[] | select(.name=="elspeth-web") | .image' "$OPERATOR_DIR/live-app.json")
ACR_LOGIN_SERVER=$(jq -r '.properties.configuration.registries[0].server' "$OPERATOR_DIR/live-app.json")
jq '{mode: .properties.configuration.activeRevisionsMode,
     affinity: .properties.configuration.ingress.stickySessions.affinity,
     scale: .properties.template.scale,
     grace: .properties.template.terminationGracePeriodSeconds}' "$OPERATOR_DIR/live-app.json"
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

Use the operator-local ARM JSON retained from installation, never the tracked
`workload.production.bicepparam` compilation example. If that file is missing,
recover the concrete parameter set from the last successful workload deployment
(`az deployment group show --query properties.parameters`) into a new local ARM
parameter envelope before continuing. Compare it to the captured app and Jobs;
do not regenerate secret IDs from the latest Key Vault versions during redeploy.
The local source parameter file remains the rollback reference. Prepare a new
candidate file with only the image and release identity changed:

```bash
NEXT_WORKLOAD_PARAMETERS="$OPERATOR_DIR/workload-${CANDIDATE_SHA}.parameters.json"
test "$WORKLOAD_PARAMETERS" != "$NEXT_WORKLOAD_PARAMETERS"
test ! -e "$NEXT_WORKLOAD_PARAMETERS"
jq --arg image "$CANDIDATE_IMAGE" --arg sha "$CANDIDATE_SHA" '
  .parameters.image.value = $image |
  .parameters.candidateSourceSha.value = $sha |
  .parameters.revisionSuffix.value = ("r" + $sha[0:12])
' "$WORKLOAD_PARAMETERS" >"$NEXT_WORKLOAD_PARAMETERS"
jq -e -f deploy/azure-container-apps/scripts/validate-workload-parameters.jq "$NEXT_WORKLOAD_PARAMETERS" >/dev/null
jq -e --arg name "$CONTAINER_APP" '.parameters.containerAppName.value == $name' "$NEXT_WORKLOAD_PARAMETERS" >/dev/null
az deployment group what-if --resource-group "$RESOURCE_GROUP" \
  --template-file deploy/azure-container-apps/workload.bicep \
  --parameters "@$NEXT_WORKLOAD_PARAMETERS"
```

The expected changes are the container image, revision suffix and candidate
SHA in `ELSPETH_WEB__OPERATOR_TELEMETRY_RELEASE` and
`ELSPETH_ACCEPTANCE_CANDIDATE_SHA` on the app and doctor Jobs.
Any change to secrets, volumes, probes, scale or ingress is a stop.

## 4. Run the doctor Job with the candidate digest

```bash
az deployment group create --name "elspeth-jobs-${CANDIDATE_SHA:0:12}" --resource-group "$RESOURCE_GROUP" \
  --template-file deploy/azure-container-apps/workload.bicep \
  --parameters "@$NEXT_WORKLOAD_PARAMETERS" --parameters deployWebApp=false --mode Incremental
bash deploy/azure-container-apps/scripts/run-job.sh "$RESOURCE_GROUP" doctor-runtime
```

The Job runs `elspeth doctor deployment --json`; require `Succeeded`. A
schema check of `STALE` means the candidate needs a compatibility decision
first; do not proceed to step 5.

## 5. Deploy the candidate revision

```bash
az containerapp update --name "$CONTAINER_APP" --resource-group "$RESOURCE_GROUP" \
  --image "$CANDIDATE_IMAGE" --revision-suffix "r${CANDIDATE_SHA:0:12}" \
  --set-env-vars "ELSPETH_ACCEPTANCE_CANDIDATE_SHA=${CANDIDATE_SHA}" \
    "ELSPETH_WEB__OPERATOR_TELEMETRY_RELEASE=${CANDIDATE_SHA}"
```

## 6. Prove the rollout

```bash
az containerapp revision list --name "$CONTAINER_APP" --resource-group "$RESOURCE_GROUP" \
  --query "[?properties.active].{name:name,traffic:properties.trafficWeight,state:properties.runningState,image:properties.template.containers[0].image}"
az containerapp replica list --name "$CONTAINER_APP" --resource-group "$RESOURCE_GROUP" \
  --revision "${CONTAINER_APP}--r${CANDIDATE_SHA:0:12}" --query '[].{name:name,state:properties.runningState}'
```

Require exactly one active revision at 100 % whose image is
`CANDIDATE_IMAGE`, `N` replicas `Running`, and the previous revision
inactive. The platform's own readiness wait is the rollout primitive; the
checks above are the proof.

After successful verification, retain `NEXT_WORKLOAD_PARAMETERS` as the
`WORKLOAD_PARAMETERS` input for the next redeploy. A configuration change uses
the same reviewed concrete parameters with `deployWebApp=true`; the direct
`az containerapp update` above is the image-only path.

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
  --revision "${CONTAINER_APP}--r${CANDIDATE_SHA:0:12}"
```

Then repeat step 6 and step 7 against the previous revision.
