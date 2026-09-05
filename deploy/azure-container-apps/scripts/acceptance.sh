#!/usr/bin/env bash
# Thin acceptance driver for the disposable Azure Container Apps run.
#
# Stage order (plan §10): group + environment -> image copy -> Jobs -> production
# rollout -> replica probes -> KQL / Resource Graph evidence -> receipts -> group
# delete. Bash + az + jq + psql only; no second scenario engine. Every platform
# call goes through the protected capture wrappers, exactly as the runbook
# docs/runbooks/azure-container-apps-deployment.md defines them.
#
# Status: skeleton prepared before the first live run. The facade commands
# (elspeth.web.azure_container_apps_acceptance) land with 6b-5; until then
# `stage_probes` and `stage_receipts` stop with a static class.
set -Eeuo pipefail
umask 077

export AZURE_CORE_OUTPUT=json
export AZURE_CORE_ONLY_SHOW_ERRORS=true
export AZURE_CORE_NO_COLOR=true
export ELSPETH_COMMAND_OUTPUT_LIMIT_BYTES="${ELSPETH_COMMAND_OUTPUT_LIMIT_BYTES:-2097152}"
export ELSPETH_AZ_CALL_CEILING_SECONDS="${ELSPETH_AZ_CALL_CEILING_SECONDS:-120}"
export ELSPETH_AZ_DEPLOY_CEILING_SECONDS="${ELSPETH_AZ_DEPLOY_CEILING_SECONDS:-3600}"
export ELSPETH_AZ_EXEC_CEILING_SECONDS="${ELSPETH_AZ_EXEC_CEILING_SECONDS:-300}"
export ELSPETH_PSQL_CALL_CEILING_SECONDS="${ELSPETH_PSQL_CALL_CEILING_SECONDS:-60}"
export ELSPETH_BICEP_CALL_CEILING_SECONDS="${ELSPETH_BICEP_CALL_CEILING_SECONDS:-300}"
export ELSPETH_HTTP_CALL_CEILING_SECONDS="${ELSPETH_HTTP_CALL_CEILING_SECONDS:-60}"
export ELSPETH_LOG_QUERY_CEILING_SECONDS="${ELSPETH_LOG_QUERY_CEILING_SECONDS:-600}"

protected_timeout_seconds() {
  local kind="$1" ceiling
  case "$kind" in
    az) ceiling="${ELSPETH_AZ_CALL_CEILING_SECONDS:?set az call ceiling}" ;;
    az-deploy) ceiling="${ELSPETH_AZ_DEPLOY_CEILING_SECONDS:?set az deployment ceiling}" ;;
    az-exec) ceiling="${ELSPETH_AZ_EXEC_CEILING_SECONDS:?set az exec ceiling}" ;;
    psql) ceiling="${ELSPETH_PSQL_CALL_CEILING_SECONDS:?set psql call ceiling}" ;;
    bicep) ceiling="${ELSPETH_BICEP_CALL_CEILING_SECONDS:?set bicep call ceiling}" ;;
    http) ceiling="${ELSPETH_HTTP_CALL_CEILING_SECONDS:?set http call ceiling}" ;;
    *) printf '%s\n' 'command_kind_invalid' >&2; return 1 ;;
  esac
  test "$ceiling" -gt 0 2>/dev/null || {
    printf '%s\n' 'command_timeout_invalid' >&2
    return 1
  }
  printf '%s\n' "$ceiling"
}

protected_capture() {
  local kind="$1" failure_class="$2"
  shift 2
  local seconds stderr_file status
  seconds=$(protected_timeout_seconds "$kind") || return 1
  stderr_file=$(mktemp -p /tmp elspeth-capture.XXXXXX) || return 1
  chmod 600 "$stderr_file" || { rm -f -- "$stderr_file"; return 1; }
  trap 'rm -f -- "$stderr_file"' RETURN
  set +e
  ( ulimit -f 4096; timeout --signal=TERM --kill-after=5s "$seconds" "$@" 2>"$stderr_file" ) \
    | head -c "$ELSPETH_COMMAND_OUTPUT_LIMIT_BYTES"
  status=${PIPESTATUS[0]}
  set -e
  if test "$status" -ne 0; then
    printf '%s\n' "$failure_class" >&2
    return "$status"
  fi
}

az_capture() { protected_capture az az_command_failed az "$@"; }
az_deploy_capture() { protected_capture az-deploy az_deployment_failed az "$@"; }
az_exec_capture() { protected_capture az-exec az_exec_failed az containerapp exec "$@"; }
bicep_capture() { protected_capture bicep bicep_command_failed bicep "$@"; }
curl_capture() { protected_capture http http_request_failed curl --silent --show-error --fail-with-body "$@"; }
psql_capture() { protected_capture psql psql_command_failed psql --no-psqlrc --quiet --tuples-only --no-align --set=ON_ERROR_STOP=1 "$@"; }

require_inputs() {
  : "${ACCEPTANCE_RUN_ID:?set a fresh run id}"
  : "${AZURE_SUBSCRIPTION_ID:?set the non-production subscription id}"
  : "${AZURE_LOCATION:?set the region}"
  : "${ACR_RESOURCE_ID:?set the existing registry resource id}"
  : "${ACR_LOGIN_SERVER:?set the existing registry login server}"
  : "${CANDIDATE_SHA:?set the 40-hex candidate sha}"
  : "${CANDIDATE_IMAGE_DIGEST:?set the sha256 index digest published to GHCR}"
  : "${OPERATOR_PUBLIC_IP:?set the operator host public IPv4}"
  : "${ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS:?set the transport ceiling (<= 240)}"
  test "$ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS" -le 240
  test "${#CANDIDATE_SHA}" -eq 40
  case "$CANDIDATE_IMAGE_DIGEST" in
    sha256:*) ;;
    *) printf '%s\n' 'candidate_digest_invalid' >&2; return 1 ;;
  esac
  export RESOURCE_GROUP="elspeth-acc-${ACCEPTANCE_RUN_ID}"
  export BUNDLE_DIR
  BUNDLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  export EVIDENCE_DIR="${EVIDENCE_DIR:-${HOME}/.local/state/elspeth/azure-container-apps/${ACCEPTANCE_RUN_ID}}"
  mkdir -p -m 0700 "$EVIDENCE_DIR"
}

stage_environment() {
  az_capture account set --subscription "$AZURE_SUBSCRIPTION_ID"
  az_deploy_capture deployment sub what-if \
    --location "$AZURE_LOCATION" \
    --template-file "$BUNDLE_DIR/main.bicep" \
    --parameters "$BUNDLE_DIR/main.acceptance.bicepparam" \
    --parameters resourceGroupName="$RESOURCE_GROUP" acceptanceRunId="$ACCEPTANCE_RUN_ID" \
      containerRegistryResourceId="$ACR_RESOURCE_ID" location="$AZURE_LOCATION" \
      keyVaultAllowedIpRules="[\"$OPERATOR_PUBLIC_IP\"]" \
    >"$EVIDENCE_DIR/what-if.json"
  az_deploy_capture deployment sub create \
    --name "elspeth-acc-${ACCEPTANCE_RUN_ID}" \
    --location "$AZURE_LOCATION" \
    --template-file "$BUNDLE_DIR/main.bicep" \
    --parameters "$BUNDLE_DIR/main.acceptance.bicepparam" \
    --parameters resourceGroupName="$RESOURCE_GROUP" acceptanceRunId="$ACCEPTANCE_RUN_ID" \
      containerRegistryResourceId="$ACR_RESOURCE_ID" location="$AZURE_LOCATION" \
      keyVaultAllowedIpRules="[\"$OPERATOR_PUBLIC_IP\"]" \
    >"$EVIDENCE_DIR/deployment.json"
  jq -S '.properties.outputs' "$EVIDENCE_DIR/deployment.json" >"$EVIDENCE_DIR/inventory.json"
  sha256sum "$EVIDENCE_DIR/inventory.json" >"$EVIDENCE_DIR/inventory.sha256"
}

stage_image() {
  az_capture acr login --name "${ACR_LOGIN_SERVER%%.*}"
  docker buildx imagetools create \
    --tag "${ACR_LOGIN_SERVER}/elspeth:sha-${CANDIDATE_SHA}" \
    "ghcr.io/dta-au/elspeth@${CANDIDATE_IMAGE_DIGEST}"
  local acr_digest
  acr_digest=$(az_capture acr manifest show-metadata \
    "${ACR_LOGIN_SERVER}/elspeth:sha-${CANDIDATE_SHA}" --query digest --output tsv)
  test "$acr_digest" = "$CANDIDATE_IMAGE_DIGEST"
  export CANDIDATE_IMAGE="${ACR_LOGIN_SERVER}/elspeth@${acr_digest}"
}

run_job_to_completion() {
  local job="$1" execution status
  execution=$(az_capture containerapp job start --name "$job" --resource-group "$RESOURCE_GROUP" \
    --query name --output tsv)
  while :; do
    status=$(az_capture containerapp job execution show --name "$job" --resource-group "$RESOURCE_GROUP" \
      --job-execution-name "$execution" --query properties.status --output tsv)
    case "$status" in
      Succeeded) break ;;
      Failed|Stopped|Degraded) printf '%s\n' "job_execution_${status,,}" >&2; return 1 ;;
      *) sleep 10 ;;
    esac
  done
  printf '%s\n' "$execution"
}

stage_jobs() {
  run_job_to_completion provision-storage >"$EVIDENCE_DIR/execution-provision-storage.txt"
  run_job_to_completion doctor-schema-init >"$EVIDENCE_DIR/execution-doctor-schema-init.txt"
  run_job_to_completion doctor-runtime-a >"$EVIDENCE_DIR/execution-doctor-runtime-a.txt"
  run_job_to_completion doctor-runtime-b >"$EVIDENCE_DIR/execution-doctor-runtime-b.txt"
}

stage_workload() {
  local shape="$1" label="$2" suffix="$3"
  local extra=()
  if test -n "$label"; then
    extra=(runtimeRoleLabel="$label")
  fi
  az_deploy_capture deployment group create \
    --name "elspeth-workload-${suffix}" \
    --resource-group "$RESOURCE_GROUP" \
    --template-file "$BUNDLE_DIR/workload.bicep" \
    --parameters "$BUNDLE_DIR/workload.${shape}.bicepparam" \
    --parameters image="$CANDIDATE_IMAGE" revisionSuffix="$suffix" \
      composerTransportIdleCeilingSeconds="$ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS" \
      "${extra[@]}" \
    >"$EVIDENCE_DIR/workload-${suffix}.json"
}

stage_probes() {
  # 6b-5 delivers the facade (replica-probes, verify-*, receipt-store).
  printf '%s\n' 'facade_not_landed_6b5' >&2
  return 1
}

kql_capture() {
  local query_file="$1" out="$2" query
  query=$(sed \
    -e "s/__WINDOW_START__/${PROBE_WINDOW_START:?}/g" \
    -e "s/__WINDOW_END__/${PROBE_WINDOW_END:?}/g" \
    -e "s/__APP_NAME__/elspeth-web/g" \
    -e "s/__JOB_NAME__/${KQL_JOB_NAME:-doctor-runtime-a}/g" \
    -e "s/__SENTINEL_HASH__/${SENTINEL_HASH:-none}/g" \
    "$query_file")
  ELSPETH_AZ_CALL_CEILING_SECONDS="$ELSPETH_LOG_QUERY_CEILING_SECONDS" az_capture monitor log-analytics query \
    --workspace "${LOG_ANALYTICS_CUSTOMER_ID:?}" --analytics-query "$query" >"$out"
  sha256sum "$query_file" >>"$EVIDENCE_DIR/kql.sha256"
}

stage_evidence() {
  kql_capture "$BUNDLE_DIR/kql/doctor-report.kql" "$EVIDENCE_DIR/doctor-report.json"
  kql_capture "$BUNDLE_DIR/kql/run-sentinel-by-replica.kql" "$EVIDENCE_DIR/run-sentinel.json"
  kql_capture "$BUNDLE_DIR/kql/replica-lifecycle.kql" "$EVIDENCE_DIR/replica-lifecycle.json"
  kql_capture "$BUNDLE_DIR/kql/fence-conflict-409.kql" "$EVIDENCE_DIR/fence-409.json"
}

stage_cleanup() {
  export ELSPETH_CLEANUP_MODE=1
  az_deploy_capture group delete --name "$RESOURCE_GROUP" --yes
  local remaining
  remaining=$(az_capture graph query \
    -q "Resources | where resourceGroup =~ '${RESOURCE_GROUP}' | count" \
    --query 'data[0].Count' --output tsv)
  test "$remaining" = 0
  if ! az_capture keyvault purge --name "${KEY_VAULT_NAME:?}" --location "$AZURE_LOCATION"; then
    printf '%s\n' 'key_vault_tombstoned' >>"$EVIDENCE_DIR/cleanup-notes.txt"
  fi
}

main() {
  local stage="${1:-all}"
  require_inputs
  case "$stage" in
    environment) stage_environment ;;
    image) stage_image ;;
    jobs) stage_jobs ;;
    workload-production) stage_workload production "" "${CANDIDATE_SHA:0:12}" ;;
    workload-probes)
      stage_workload acceptance a "${CANDIDATE_SHA:0:12}-a"
      stage_workload acceptance b "${CANDIDATE_SHA:0:12}-b"
      ;;
    probes) stage_probes ;;
    evidence) stage_evidence ;;
    cleanup) stage_cleanup ;;
    all)
      stage_environment
      stage_image
      stage_workload production "" "${CANDIDATE_SHA:0:12}"
      stage_jobs
      stage_workload acceptance a "${CANDIDATE_SHA:0:12}-a"
      stage_workload acceptance b "${CANDIDATE_SHA:0:12}-b"
      stage_probes
      stage_evidence
      stage_cleanup
      ;;
    *) printf '%s\n' 'stage_invalid' >&2; return 64 ;;
  esac
}

main "$@"
