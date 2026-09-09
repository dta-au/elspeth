# Azure Container Apps command cheat sheet

These commands operate an **existing** ELSPETH container app. Run them from
the repository/worktree containing the code to deploy. They deliberately do
not bootstrap or destroy Azure infrastructure. Every literal comes from the
platform facts (`docs/plans/2026-09-05-phase6b-azure-container-apps-platform-facts.md`).

## 0. Shell and authentication

```bash
set -Eeuo pipefail
umask 077
export AZURE_CORE_OUTPUT=json
: "${AZURE_SUBSCRIPTION_ID:?export the subscription id}"
: "${RESOURCE_GROUP:?export the resource group}"
: "${CONTAINER_APP:?export the container app name}"
: "${DEPLOY_REF:?export the user-selected branch, tag, or commit to deploy}"

DEPLOY_SHA=$(git rev-parse "${DEPLOY_REF}^{commit}")
test "$(git rev-parse HEAD)" = "$DEPLOY_SHA"
test -z "$(git status --porcelain)"
az account set --subscription "$AZURE_SUBSCRIPTION_ID"
az account show --query '{subscription:id,tenant:tenantId,user:user.name}'
```

If `az account show` reports an expired session, the human runs
`az login`. Do not edit `~/.azure` around the failure.

## 1. Discover the live app

```bash
az containerapp show --name "$CONTAINER_APP" --resource-group "$RESOURCE_GROUP" >live-app.json
jq '{revision: .properties.latestReadyRevisionName,
     image: (.properties.template.containers[] | select(.name=="elspeth-web") | .image),
     mode: .properties.configuration.activeRevisionsMode,
     affinity: .properties.configuration.ingress.stickySessions.affinity,
     scale: .properties.template.scale,
     registry: .properties.configuration.registries[0].server,
     identity: (.identity.userAssignedIdentities | keys[0])}' live-app.json
az containerapp revision list --name "$CONTAINER_APP" --resource-group "$RESOURCE_GROUP" \
  --query "[?properties.active].{name:name,traffic:properties.trafficWeight,state:properties.runningState}"
az containerapp replica list --name "$CONTAINER_APP" --resource-group "$RESOURCE_GROUP" \
  --revision "$(jq -r '.properties.latestReadyRevisionName' live-app.json)" --query '[].name'
```

## 2. Run targeted pre-deploy tests

```bash
cd "$(git rev-parse --show-toplevel)"
unset VIRTUAL_ENV
export PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src"
.venv/bin/pytest -n 4 -o "pythonpath=src elspeth-lints/src" \
  tests/unit/web/test_deployment_contract.py \
  tests/unit/web/test_azure_container_apps_runbook_contract.py \
  tests/unit/deployment/test_azure_container_apps_bundle.py
```

## 3. Publish by digest (copy, never rebuild)

```bash
ACR_LOGIN_SERVER=$(jq -r '.properties.configuration.registries[0].server' live-app.json)
GHCR_DIGEST=$(docker buildx imagetools inspect "ghcr.io/dta-au/elspeth:sha-${DEPLOY_SHA}" --format '{{.Manifest.Digest}}')
az acr login --name "${ACR_LOGIN_SERVER%%.*}"
docker buildx imagetools create --tag "${ACR_LOGIN_SERVER}/elspeth:sha-${DEPLOY_SHA}" "ghcr.io/dta-au/elspeth@${GHCR_DIGEST}"
ACR_DIGEST=$(az acr manifest show-metadata "${ACR_LOGIN_SERVER}/elspeth:sha-${DEPLOY_SHA}" --query digest --output tsv)
test "$ACR_DIGEST" = "$GHCR_DIGEST"
CANDIDATE_IMAGE="${ACR_LOGIN_SERVER}/elspeth@${ACR_DIGEST}"
```

## 4. Doctor Job with the candidate digest

```bash
az containerapp job update --name doctor-runtime --resource-group "$RESOURCE_GROUP" --image "$CANDIDATE_IMAGE"
EXECUTION=$(az containerapp job start --name doctor-runtime --resource-group "$RESOURCE_GROUP" --query name --output tsv)
az containerapp job execution show --name doctor-runtime --resource-group "$RESOURCE_GROUP" \
  --job-execution-name "$EXECUTION" --query properties.status --output tsv
```

Require `Succeeded`. The Job runs `elspeth doctor deployment --json`.

## 5. Roll the revision

```bash
az containerapp update --name "$CONTAINER_APP" --resource-group "$RESOURCE_GROUP" \
  --image "$CANDIDATE_IMAGE" --revision-suffix "${DEPLOY_SHA:0:12}"
```

## 6. Prove the rollout

```bash
az containerapp revision list --name "$CONTAINER_APP" --resource-group "$RESOURCE_GROUP" \
  --query "[?properties.active].{name:name,traffic:properties.trafficWeight,state:properties.runningState,image:properties.template.containers[0].image}"
az containerapp replica list --name "$CONTAINER_APP" --resource-group "$RESOURCE_GROUP" \
  --revision "${CONTAINER_APP}--${DEPLOY_SHA:0:12}" --query '[].{name:name,state:properties.runningState}'
```

## 7. Public verification

```bash
FQDN=$(jq -r '.properties.configuration.ingress.fqdn' live-app.json)
curl --silent --fail-with-body "https://${FQDN}/api/health"
curl --silent --fail-with-body "https://${FQDN}/api/ready" | jq -e '.ready == true'
curl --silent --fail-with-body --dump-header - "https://${FQDN}/api/system/status" | grep -i '^X-Elspeth-Instance:'
```

## 8. Logs

```bash
WORKSPACE_ID=$(az containerapp env show --name "$(jq -r '.properties.environmentId | split("/") | last' live-app.json)" \
  --resource-group "$RESOURCE_GROUP" --query properties.appLogsConfiguration.logAnalyticsConfiguration.customerId --output tsv)
az monitor log-analytics query --workspace "$WORKSPACE_ID" --analytics-query \
  "ContainerAppConsoleLogs_CL | where ContainerAppName_s == '${CONTAINER_APP}' and RevisionName_s == '${CONTAINER_APP}--${DEPLOY_SHA:0:12}' | project TimeGenerated, Log_s | order by TimeGenerated desc | take 100"
az monitor log-analytics query --workspace "$WORKSPACE_ID" --analytics-query \
  "ContainerAppSystemLogs_CL | where ContainerAppName_s == '${CONTAINER_APP}' | project TimeGenerated, RevisionName_s, Log_s | order by TimeGenerated desc | take 50"
```

Ingestion lags by minutes. `az containerapp logs show --follow` streams
live console output when the destination lag matters.

## 9. Stop, resume, rollback

```bash
az containerapp update --name "$CONTAINER_APP" --resource-group "$RESOURCE_GROUP" --min-replicas 0 --max-replicas 0
az containerapp update --name "$CONTAINER_APP" --resource-group "$RESOURCE_GROUP" --min-replicas 2 --max-replicas 4
```

Rollback only when the compatibility record permits it (a Scenario A install
does not): activate the previous revision, set its weight to 100, deactivate
the candidate, then repeat sections 6 and 7.
