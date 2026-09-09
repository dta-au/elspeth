#!/usr/bin/env bash
# Disposable ACA acceptance. Operator-local parameter JSON and prepared probe
# sessions are inputs; observations and receipts are collected, never invented.
set -Eeuo pipefail
umask 077

export AZURE_CORE_OUTPUT=json AZURE_CORE_ONLY_SHOW_ERRORS=true AZURE_CORE_NO_COLOR=true
export ELSPETH_COMMAND_OUTPUT_LIMIT_BYTES="${ELSPETH_COMMAND_OUTPUT_LIMIT_BYTES:-2097152}"
export ELSPETH_AZ_CALL_CEILING_SECONDS="${ELSPETH_AZ_CALL_CEILING_SECONDS:-120}"
export ELSPETH_AZ_DEPLOY_CEILING_SECONDS="${ELSPETH_AZ_DEPLOY_CEILING_SECONDS:-3600}"
export ELSPETH_AZ_EXEC_CEILING_SECONDS="${ELSPETH_AZ_EXEC_CEILING_SECONDS:-300}"
export ELSPETH_PSQL_CALL_CEILING_SECONDS="${ELSPETH_PSQL_CALL_CEILING_SECONDS:-60}"
export ELSPETH_BICEP_CALL_CEILING_SECONDS="${ELSPETH_BICEP_CALL_CEILING_SECONDS:-300}"
export ELSPETH_HTTP_CALL_CEILING_SECONDS="${ELSPETH_HTTP_CALL_CEILING_SECONDS:-60}"
export ELSPETH_LOG_QUERY_CEILING_SECONDS="${ELSPETH_LOG_QUERY_CEILING_SECONDS:-600}"
export ELSPETH_ACCEPTANCE_PROBE_CEILING_SECONDS="${ELSPETH_ACCEPTANCE_PROBE_CEILING_SECONDS:-3600}"
export ELSPETH_JOB_WAIT_SECONDS="${ELSPETH_JOB_WAIT_SECONDS:-1800}"
export ELSPETH_POLL_SECONDS="${ELSPETH_POLL_SECONDS:-10}"
PYTHON="${ELSPETH_ACCEPTANCE_PYTHON:-python}"
APP_NAME=elspeth-web
GROUP_MAY_EXIST=0
RESTORE_ROLES=0
CLEANED_UP=0
PROBE_FAILED=0

fail() { printf '%s\n' "$1" >&2; return 1; }
positive_integer() { [[ "$1" =~ ^[1-9][0-9]*$ ]] || fail integer_input_invalid; }
protected_timeout_seconds() {
  case "$1" in
    az) printf '%s\n' "$ELSPETH_AZ_CALL_CEILING_SECONDS" ;;
    az-deploy) printf '%s\n' "$ELSPETH_AZ_DEPLOY_CEILING_SECONDS" ;;
    az-exec) printf '%s\n' "$ELSPETH_AZ_EXEC_CEILING_SECONDS" ;;
    psql) printf '%s\n' "$ELSPETH_PSQL_CALL_CEILING_SECONDS" ;;
    bicep) printf '%s\n' "$ELSPETH_BICEP_CALL_CEILING_SECONDS" ;;
    http) printf '%s\n' "$ELSPETH_HTTP_CALL_CEILING_SECONDS" ;;
    probe) printf '%s\n' "$ELSPETH_ACCEPTANCE_PROBE_CEILING_SECONDS" ;;
    *) fail command_kind_invalid ;;
  esac
}

protected_capture() (
  local kind="$1" failure_class="$2" seconds scratch cleanup stderr_reader status=0
  shift 2
  seconds=$(protected_timeout_seconds "$kind") || exit 1
  positive_integer "$seconds" || exit 1
  scratch=$(mktemp -d -p /tmp elspeth-capture.XXXXXX) || exit 1
  printf -v cleanup 'rm -rf -- %q' "$scratch"
  trap "$cleanup" EXIT
  # A process-wide RLIMIT_FSIZE breaks Bicep's .NET runtime before compilation.
  # Bound the two captured streams instead; timeout still bounds blocked writers.
  mkfifo "$scratch/stderr-pipe"
  head -c "$((ELSPETH_COMMAND_OUTPUT_LIMIT_BYTES + 1))" <"$scratch/stderr-pipe" >"$scratch/stderr" &
  stderr_reader=$!
  set +e
  timeout --signal=TERM --kill-after=5s "$seconds" "$@" 2>"$scratch/stderr-pipe" \
    | head -c "$((ELSPETH_COMMAND_OUTPUT_LIMIT_BYTES + 1))" >"$scratch/stdout"
  status=${PIPESTATUS[0]}
  wait "$stderr_reader"
  set -e
  if test "$(wc -c <"$scratch/stdout")" -gt "$ELSPETH_COMMAND_OUTPUT_LIMIT_BYTES" \
    || test "$(wc -c <"$scratch/stderr")" -gt "$ELSPETH_COMMAND_OUTPUT_LIMIT_BYTES"; then
    fail command_output_limit_exceeded
    exit 1
  fi
  cat "$scratch/stdout"
  if test "$status" -ne 0; then
    printf '%s\n' "$failure_class" >&2
    exit "$status"
  fi
)
az_capture() { protected_capture az az_command_failed az "$@"; }
az_deploy_capture() { protected_capture az-deploy az_deployment_failed az "$@"; }
az_exec_capture() { protected_capture az-exec az_exec_failed az containerapp exec "$@"; }
bicep_capture() { protected_capture bicep bicep_command_failed bicep "$@"; }
curl_capture() { protected_capture http http_request_failed curl --silent --show-error --fail-with-body --max-filesize "$ELSPETH_COMMAND_OUTPUT_LIMIT_BYTES" "$@"; }
psql_capture() { protected_capture psql psql_command_failed psql --no-psqlrc --quiet --tuples-only --no-align --set=ON_ERROR_STOP=1 "$@"; }
facade() { protected_capture probe acceptance_command_failed "$PYTHON" -m elspeth.web.azure_container_apps_acceptance "$@"; }

require_file() { test -f "$1" && test -r "$1" || fail required_file_unreadable; }
require_parameters() {
  require_file "$1"
  # The concrete resolved files, not checked-in examples, reach what-if/create.
  jq -e '.parameters | type == "object" and length > 0' "$1" >/dev/null
  jq -e '[.. | strings | select(test("00000000-0000-0000-0000-000000000000|sha256:0{64}|elspeth-kv-example|RUN-ID"))]
    | length == 0' "$1" >/dev/null || fail unresolved_parameter_placeholder
}
require_inputs() {
  : "${ACCEPTANCE_RUN_ID:?set a fresh run id}" "${AZURE_SUBSCRIPTION_ID:?set subscription id}"
  : "${AZURE_LOCATION:?set region}" "${CANDIDATE_SHA:?set candidate sha}"
  : "${CANDIDATE_IMAGE_DIGEST:?set published index digest}"
  [[ "$ACCEPTANCE_RUN_ID" =~ ^[a-z0-9][a-z0-9-]{0,39}$ ]] || fail acceptance_run_id_invalid
  [[ "$CANDIDATE_SHA" =~ ^[0-9a-f]{40}$ ]] || fail candidate_sha_invalid
  [[ "$CANDIDATE_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || fail candidate_digest_invalid
  positive_integer "$ELSPETH_COMMAND_OUTPUT_LIMIT_BYTES"
  positive_integer "$ELSPETH_JOB_WAIT_SECONDS"
  positive_integer "$ELSPETH_POLL_SECONDS"
  export RESOURCE_GROUP="elspeth-acc-${ACCEPTANCE_RUN_ID}"
  export BUNDLE_DIR
  BUNDLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  export EVIDENCE_DIR="${EVIDENCE_DIR:-${HOME}/.local/state/elspeth/azure-container-apps/${ACCEPTANCE_RUN_ID}}"
  mkdir -p -m 0700 "$EVIDENCE_DIR"
  test ! -L "$EVIDENCE_DIR" && test "$(stat -c '%u:%a' "$EVIDENCE_DIR")" = "$(id -u):700" || fail evidence_directory_permissions
  RECEIPT_DIR="$EVIDENCE_DIR/receipts"
  REVISION_SUFFIX="r${CANDIDATE_SHA:0:12}"
  load_resolved_workload_parameters
}
load_resolved_workload_parameters() {
  WORKLOAD_PARAMETERS="${WORKLOAD_PARAMETERS:-$EVIDENCE_DIR/parameters/workload.parameters.json}"
  WORKLOAD_A_PARAMETERS="${WORKLOAD_A_PARAMETERS:-$EVIDENCE_DIR/parameters/workload-a.parameters.json}"
  WORKLOAD_B_PARAMETERS="${WORKLOAD_B_PARAMETERS:-$EVIDENCE_DIR/parameters/workload-b.parameters.json}"
}
preflight_all() {
  require_parameters "${MAIN_PARAMETERS:?set resolved local main parameters JSON}"
  require_file "${PROBE_YAML:?set P2/P4 pipeline YAML}"
  require_file "${P3_YAML:?set long-running pipeline YAML}"
  require_file "${PROBE_SOURCE_BLOB:?set source blob request JSON}"
  require_file "${P4_MESSAGE_BODY:?set normal Composer message JSON}"
  require_file "${P1_BODY:?set guided action template JSON}"
  require_file "${COMPATIBILITY_RECORD:?set operator compatibility record}"
  : "${P1_INTENT:?set guided intent}" "${P3_SINK_PATH:?set physical CSV path template}"
  : "${P3_SINK_KEY_FIELD:?set stable unique CSV key}" "${ACCEPTANCE_SECRET_DIR:?set secret value directory}"
  : "${BOOTSTRAP_PRINCIPAL_ID:?set operator principal id}" "${BOOTSTRAP_PRINCIPAL_TYPE:?set User or ServicePrincipal}"
  : "${PROVISION_STORAGE_IMAGE:?set pinned provisioner digest}" "${PGSSLROOTCERT:?set readable PostgreSQL root PEM}"
  : "${PG_MAX_CONNECTIONS:?set server connection limit}" "${PG_APPROVED_BUDGET:?set approved connection budget}"
  : "${PG_SAFETY_MARGIN:?set connection safety margin}" "${SENTINEL_HASH:?set fixture sentinel hash}"
  : "${ELSPETH_ACCEPTANCE_CANARY_TOKEN:?set log-leak canary}"
  : "${ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS:?set transport ceiling}"
  [[ "$P3_SINK_PATH" == *'{session_id}'* ]] || fail sink_path_requires_session_template
  [[ "$SENTINEL_HASH" =~ ^[0-9a-f]{64}$ ]] || fail sentinel_hash_invalid
  local trials="${PROBE_TRIALS:-20}"
  positive_integer "$trials"
  test "$trials" -ge 20 || fail probe_trials_insufficient
  if test -z "${ELSPETH_ACCEPTANCE_BEARER_TOKEN:-}"; then
    : "${ELSPETH_ACCEPTANCE_USERNAME:?set local username or bearer token}"
    : "${ELSPETH_ACCEPTANCE_PASSWORD:?set local password or bearer token}"
  fi
}

stage_environment() {
  require_parameters "${MAIN_PARAMETERS:?set resolved local main parameters JSON}"
  az_capture account set --subscription "$AZURE_SUBSCRIPTION_ID" >"$EVIDENCE_DIR/account.json"
  local exists
  exists=$(az_capture group exists --name "$RESOURCE_GROUP")
  test "$exists" = false || fail acceptance_group_already_exists
  az_deploy_capture deployment sub what-if --location "$AZURE_LOCATION" \
    --template-file "$BUNDLE_DIR/main.bicep" --parameters "@$MAIN_PARAMETERS" \
    --parameters resourceGroupName="$RESOURCE_GROUP" acceptanceRunId="$ACCEPTANCE_RUN_ID" \
    >"$EVIDENCE_DIR/what-if.json"
  GROUP_MAY_EXIST=1
  az_deploy_capture deployment sub create --name "elspeth-acc-${ACCEPTANCE_RUN_ID}" \
    --location "$AZURE_LOCATION" --template-file "$BUNDLE_DIR/main.bicep" \
    --parameters "@$MAIN_PARAMETERS" \
    --parameters resourceGroupName="$RESOURCE_GROUP" acceptanceRunId="$ACCEPTANCE_RUN_ID" \
    >"$EVIDENCE_DIR/deployment.json"
  jq -eS '.properties.outputs' "$EVIDENCE_DIR/deployment.json" >"$EVIDENCE_DIR/inventory.json"
  sha256sum "$EVIDENCE_DIR/inventory.json" >"$EVIDENCE_DIR/inventory.sha256"
}
load_inventory() {
  local inventory="$EVIDENCE_DIR/inventory.json"
  require_file "$inventory"
  KEY_VAULT_NAME=$(jq -er '.keyVaultName.value' "$inventory")
  SCHEMA_OWNER_KEY_VAULT_NAME=$(jq -er '.schemaOwnerKeyVaultName.value' "$inventory")
  LOG_ANALYTICS_CUSTOMER_ID=$(jq -er '.logAnalyticsCustomerId.value' "$inventory")
  POSTGRES_RESOURCE_ID=$(jq -er '.postgresServerResourceId.value' "$inventory")
  APP_DOMAIN=$(jq -er '.environmentDefaultDomain.value' "$inventory")
  export KEY_VAULT_NAME SCHEMA_OWNER_KEY_VAULT_NAME LOG_ANALYTICS_CUSTOMER_ID POSTGRES_RESOURCE_ID APP_DOMAIN
}
stage_image() {
  : "${ACR_LOGIN_SERVER:?set existing registry login server}"
  az_capture acr login --name "${ACR_LOGIN_SERVER%%.*}" >"$EVIDENCE_DIR/registry-login.json"
  protected_capture az-deploy image_copy_failed docker buildx imagetools create \
    --tag "${ACR_LOGIN_SERVER}/elspeth:sha-${CANDIDATE_SHA}" \
    "ghcr.io/dta-au/elspeth@${CANDIDATE_IMAGE_DIGEST}" >"$EVIDENCE_DIR/image-copy.txt"
  local digest
  digest=$(az_capture acr manifest show-metadata \
    "${ACR_LOGIN_SERVER}/elspeth:sha-${CANDIDATE_SHA}" --query digest --output tsv)
  test "$digest" = "$CANDIDATE_IMAGE_DIGEST" || fail image_copy_digest_mismatch
}
candidate_image() { printf '%s/elspeth@%s\n' "${ACR_LOGIN_SERVER:?}" "$CANDIDATE_IMAGE_DIGEST"; }
stage_bootstrap() {
  export CANDIDATE_IMAGE
  CANDIDATE_IMAGE=$(candidate_image)
  protected_capture az-deploy acceptance_bootstrap_failed bash "$BUNDLE_DIR/scripts/bootstrap-acceptance.sh" \
    "$EVIDENCE_DIR/inventory.json" "${ACCEPTANCE_SECRET_DIR:?set operator-local secret value directory}" \
    "$EVIDENCE_DIR/parameters" >"$EVIDENCE_DIR/bootstrap.log"
  WORKLOAD_PARAMETERS="$EVIDENCE_DIR/parameters/workload.parameters.json"
  WORKLOAD_A_PARAMETERS="$EVIDENCE_DIR/parameters/workload-a.parameters.json"
  WORKLOAD_B_PARAMETERS="$EVIDENCE_DIR/parameters/workload-b.parameters.json"
  load_database_environment
}
load_database_environment() {
  local document="$EVIDENCE_DIR/parameters/acceptance-env.json" name value
  require_file "$document"
  for name in ELSPETH_ACCEPTANCE_PG_ADMIN_URL ELSPETH_ACCEPTANCE_PG_RUNTIME_A_URL \
    ELSPETH_ACCEPTANCE_PG_RUNTIME_B_URL ELSPETH_ACCEPTANCE_SESSION_DB_URL \
    ELSPETH_ACCEPTANCE_LANDSCAPE_URL ELSPETH_TEST_POSTGRES_URL; do
    value=$(jq -er --arg name "$name" '.[$name] | select(type == "string" and length > 0)' "$document")
    export "$name=$value"
  done
}
stage_testcontainer() {
  : "${ELSPETH_TEST_POSTGRES_URL:?set provisioned Flexible Server URL}"
  local status=0 repo_root
  repo_root="$(cd "$BUNDLE_DIR/../.." && pwd)"
  test "$(git -C "$repo_root" rev-parse HEAD)" = "$CANDIDATE_SHA" || fail candidate_checkout_mismatch
  test -z "$(git -C "$repo_root" status --porcelain --untracked-files=normal)" || fail candidate_checkout_dirty
  local expected_host
  expected_host=$(jq -er '.postgresFqdn.value' "$EVIDENCE_DIR/inventory.json")
  EXPECTED_POSTGRES_HOST="$expected_host" protected_capture probe postgres_target_mismatch "$PYTHON" -c \
    'import os, sys; from urllib.parse import urlsplit, parse_qs; u=urlsplit(os.environ["ELSPETH_TEST_POSTGRES_URL"]); q=parse_qs(u.query); sys.exit(0 if u.hostname == os.environ["EXPECTED_POSTGRES_HOST"] and q.get("sslmode") == ["verify-full"] else 1)'
  local junit="$EVIDENCE_DIR/testcontainer-junit.xml"
  test ! -e "$junit" || fail testcontainer_evidence_already_exists
  (
    cd "$repo_root"
    export PYTHONPATH="$repo_root/src:$repo_root/elspeth-lints/src"
    "$PYTHON" -m pytest tests/ -m testcontainer -n 0 --junitxml="$junit"
  ) >"$EVIDENCE_DIR/testcontainer-pytest.log" 2>&1 || status=$?
  printf '%s\n' "$status" >"$EVIDENCE_DIR/testcontainer-pytest.exit"
  TESTCONTAINER_RECEIPT="$EVIDENCE_DIR/testcontainer-run.json"
  protected_capture probe testcontainer_receipt_failed "$PYTHON" -m elspeth.web._acceptance_common.testcontainer_run \
    --provider azure --junit "$junit" --exit-code "$status" --candidate-sha "$CANDIDATE_SHA" \
    --scenario-id A >"$TESTCONTAINER_RECEIPT"
  local junit_hash
  junit_hash=$(jq -er '.junit_sha256' "$TESTCONTAINER_RECEIPT")
  facade receipt-store --store-dir "$RECEIPT_DIR" --kind testcontainer-run --scenario-id A --subject-id "$junit_hash" \
    --candidate-sha "$CANDIDATE_SHA" --receipt-file "$TESTCONTAINER_RECEIPT" >"$EVIDENCE_DIR/testcontainer-receipt.sha256"
  return "$status"
}
stage_workload() {
  local shape="$1" label="$2" suffix="$3" deploy_web="${4:-true}" parameters
  local extra=()
  : "${ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS:?set transport ceiling}"
  positive_integer "$ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS"
  test "$ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS" -le 240 || fail transport_ceiling_invalid
  if test "$shape" = production; then
    parameters="${WORKLOAD_PARAMETERS:?set resolved production parameters JSON}"
    extra=(activeRevisionsMode=Single stickySessionsAffinity=sticky minReplicas=2 maxReplicas=2)
  elif test "$label" = a; then
    parameters="${WORKLOAD_A_PARAMETERS:?set resolved role A parameters JSON}"
  else
    parameters="${WORKLOAD_B_PARAMETERS:?set resolved role B parameters JSON}"
  fi
  require_parameters "$parameters"
  if test "$shape" = acceptance; then
    extra=(activeRevisionsMode=Multiple stickySessionsAffinity=none minReplicas=1 maxReplicas=1 runtimeRoleLabel="$label")
  fi
  if test "$deploy_web" = false; then
    extra+=(verifyBlobManagedIdentity=true)
  fi
  az_deploy_capture deployment group create --name "elspeth-workload-${suffix}-${deploy_web}" \
    --resource-group "$RESOURCE_GROUP" --template-file "$BUNDLE_DIR/workload.bicep" \
    --parameters "@$parameters" --parameters image="$(candidate_image)" candidateSourceSha="$CANDIDATE_SHA" \
    revisionSuffix="$suffix" deployWebApp="$deploy_web" \
    composerTransportIdleCeilingSeconds="$ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS" "${extra[@]}" \
    >"$EVIDENCE_DIR/workload-${suffix}-${deploy_web}.json"
}
run_job_to_completion() {
  local job="$1" execution status deadline=$((SECONDS + ELSPETH_JOB_WAIT_SECONDS))
  execution=$(az_capture containerapp job start --name "$job" --resource-group "$RESOURCE_GROUP" --query name --output tsv)
  [[ "$execution" =~ ^[a-z0-9][a-z0-9-]{0,127}$ ]] || fail job_execution_name_invalid
  printf '%s\n' "$execution" >"$EVIDENCE_DIR/execution-${job}.txt"
  while :; do
    az_capture containerapp job execution show --name "$job" --resource-group "$RESOURCE_GROUP" \
      --job-execution-name "$execution" >"$EVIDENCE_DIR/execution-${job}.json"
    status=$(jq -er '.properties.status' "$EVIDENCE_DIR/execution-${job}.json")
    case "$status" in
      Succeeded) return 0 ;;
      Failed|Stopped|Degraded) fail job_execution_failed; return 1 ;;
      Running|Processing|Pending|Unknown) ;;
      *) fail job_execution_status_invalid; return 1 ;;
    esac
    test "$SECONDS" -lt "$deadline" || { fail job_execution_timeout; return 1; }
    sleep "$ELSPETH_POLL_SECONDS"
  done
}
job_report() {
  local job="$1" container="$2" execution
  execution=$(cat "$EVIDENCE_DIR/execution-${job}.txt")
  az_capture containerapp job logs show --name "$job" --resource-group "$RESOURCE_GROUP" \
    --execution "$execution" --container "$container" --tail 300 --format json \
    >"$EVIDENCE_DIR/logs-${job}.jsonl"
  # The execution-scoped log stream includes connection notices. Only a JSON
  # document emitted by the doctor / MI probe is a report; exactly one is required.
  jq -es '[.[] | .Log | fromjson? | select(type == "array" or type == "object")]
    | if length == 1 then .[0] else error("job_report_missing_or_ambiguous") end' \
    "$EVIDENCE_DIR/logs-${job}.jsonl" >"$EVIDENCE_DIR/report-${job}.json"
}
stage_jobs() {
  date -u +%Y-%m-%dT%H:%M:%SZ >"$EVIDENCE_DIR/jobs-window-start.txt"
  stage_workload acceptance a "${REVISION_SUFFIX}-a" false
  stage_workload acceptance b "${REVISION_SUFFIX}-b" false
  run_job_to_completion provision-storage
  local job
  for job in doctor-schema-init doctor-runtime-a doctor-runtime-b; do
    run_job_to_completion "$job"
    job_report "$job" doctor
  done
  run_job_to_completion verify-blob-managed-identity
  job_report verify-blob-managed-identity verify-blob-managed-identity
}

bind_replica() {
  local revision="$1" minimum="${2:-1}" deadline=$((SECONDS + ELSPETH_JOB_WAIT_SECONDS))
  az_capture containerapp show --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" >"$EVIDENCE_DIR/app.json"
  CONTAINER_APP_ID=$(jq -er '.id' "$EVIDENCE_DIR/app.json")
  while :; do
    az_capture containerapp replica list --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
      --revision "$revision" >"$EVIDENCE_DIR/replicas-${revision}.json"
    if jq -e --argjson minimum "$minimum" 'length >= $minimum and all(.[]; .properties.runningState == "Running")' \
      "$EVIDENCE_DIR/replicas-${revision}.json" >/dev/null; then break; fi
    test "$SECONDS" -lt "$deadline" || { fail replica_readiness_timeout; return 1; }
    sleep "$ELSPETH_POLL_SECONDS"
  done
  REPLICA=$(jq -er '[.[] | select(.properties.runningState == "Running")] | .[0].name' "$EVIDENCE_DIR/replicas-${revision}.json")
  REVISION="$revision"
  BINDING_ARGS=(--candidate-sha "$CANDIDATE_SHA" --scenario-id A --container-app-id "$CONTAINER_APP_ID" --revision "$REVISION" --replica "$REPLICA")
  SUBJECT="${CONTAINER_APP_ID}/revisions/${REVISION}/replicas/${REPLICA}"
  jq -n --arg app "$CONTAINER_APP_ID" --arg revision "$REVISION" --arg replica "$REPLICA" \
    '{app:$app,revision:$revision,replica:$replica}' >"$EVIDENCE_DIR/binding.json"
}
load_binding() {
  CONTAINER_APP_ID=$(jq -er '.app' "$EVIDENCE_DIR/binding.json")
  REVISION=$(jq -er '.revision' "$EVIDENCE_DIR/binding.json")
  REPLICA=$(jq -er '.replica' "$EVIDENCE_DIR/binding.json")
  BINDING_ARGS=(--candidate-sha "$CANDIDATE_SHA" --scenario-id A --container-app-id "$CONTAINER_APP_ID" --revision "$REVISION" --replica "$REPLICA")
  SUBJECT="${CONTAINER_APP_ID}/revisions/${REVISION}/replicas/${REPLICA}"
}
store_exec_receipt() {
  local kind="$1" name="$2"
  facade extract-exec-receipt "${BINDING_ARGS[@]}" --check "$kind" \
    <"$EVIDENCE_DIR/${name}.stream" >"$EVIDENCE_DIR/${name}.receipt.json"
  facade receipt-store --store-dir "$RECEIPT_DIR" --kind "$kind" --scenario-id A \
    --subject-id "$SUBJECT" --candidate-sha "$CANDIDATE_SHA" \
    --receipt-file "$EVIDENCE_DIR/${name}.receipt.json" >"$EVIDENCE_DIR/${name}.receipt.sha256"
}
verify_receipt() {
  local kind="$1" name="$2"
  shift 2
  facade "$@" "${BINDING_ARGS[@]}" >"$EVIDENCE_DIR/${name}.stream"
  store_exec_receipt "$kind" "$name"
}
wait_http_ready() {
  local origin="$1" name="$2" deadline=$((SECONDS + ELSPETH_JOB_WAIT_SECONDS)) health ready
  while :; do
    if health=$(curl_capture --output "$EVIDENCE_DIR/${name}-health.json" --write-out '%{http_code}' "$origin/api/health") \
      && test "$health" = 200 \
      && ready=$(curl_capture --output "$EVIDENCE_DIR/${name}-ready.json" --write-out '%{http_code}' "$origin/api/ready") \
      && test "$ready" = 200 \
      && jq -e '.ready == true' "$EVIDENCE_DIR/${name}-ready.json" >/dev/null; then
      LAST_HEALTH_STATUS="$health"
      LAST_READY_STATUS="$ready"
      return 0
    fi
    test "$SECONDS" -lt "$deadline" || { fail http_readiness_timeout; return 1; }
    sleep "$ELSPETH_POLL_SECONDS"
  done
}
stage_rollout() {
  local suffix="${1:-$REVISION_SUFFIX}"
  stage_workload production "" "$suffix"
  local deadline=$((SECONDS + ELSPETH_JOB_WAIT_SECONDS)) health ready
  while :; do
    az_capture containerapp revision list --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
      --query '[?properties.active].{name:name,traffic:properties.trafficWeight,state:properties.runningState}' \
      >"$EVIDENCE_DIR/production-revisions.json"
    if jq -e --arg revision "${APP_NAME}--${suffix}" \
      'length == 1 and .[0].name == $revision and .[0].traffic == 100 and .[0].state == "Running"' \
      "$EVIDENCE_DIR/production-revisions.json" >/dev/null; then break; fi
    test "$SECONDS" -lt "$deadline" || { fail revision_rollout_timeout; return 1; }
    sleep "$ELSPETH_POLL_SECONDS"
  done
  bind_replica "${APP_NAME}--${suffix}" 2
  az_capture containerapp revision show --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
    --revision "$REVISION" >"$EVIDENCE_DIR/candidate-revision.json"
  jq -e --arg image "$(candidate_image)" '.properties.template.containers
    | length == 1 and .[0].image == $image' "$EVIDENCE_DIR/candidate-revision.json" >/dev/null
  local origin
  origin=$(jq -er '.properties.configuration.ingress.fqdn' "$EVIDENCE_DIR/app.json")
  wait_http_ready "https://${origin}" production
  health="$LAST_HEALTH_STATUS"
  ready="$LAST_READY_STATUS"
  verify_receipt revision-rollout revision-rollout revision-rollout --revisions "$EVIDENCE_DIR/production-revisions.json" \
    --replicas "$EVIDENCE_DIR/replicas-${REVISION}.json" --image-digest "$CANDIDATE_IMAGE_DIGEST" --health-status "$health" --ready-status "$ready"
  local job
  for job in doctor-schema-init doctor-runtime-a doctor-runtime-b; do
    facade verify-doctor-job "${BINDING_ARGS[@]}" --job-name "$job" \
      --execution "$EVIDENCE_DIR/execution-${job}.json" --report "$EVIDENCE_DIR/report-${job}.json" \
      >"$EVIDENCE_DIR/${job}.stream"
    facade extract-exec-receipt "${BINDING_ARGS[@]}" --check verify-doctor-job \
      <"$EVIDENCE_DIR/${job}.stream" >"$EVIDENCE_DIR/${job}.receipt.json"
  done
  # The store admits one receipt per kind and replica subject. All three
  # execution reports above are validated; runtime A supplies that index row.
  store_exec_receipt verify-doctor-job doctor-runtime-a
  verify_receipt verify-blob-managed-identity blob-managed-identity verify-blob-managed-identity \
    --execution "$EVIDENCE_DIR/execution-verify-blob-managed-identity.json" --report "$EVIDENCE_DIR/report-verify-blob-managed-identity.json"
  az_exec_capture --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" --revision "$REVISION" --replica "$REPLICA" \
    --command "stat -c '%u %g %a' /mnt/elspeth/data /mnt/elspeth/data/blobs /mnt/elspeth/payloads" \
    >"$EVIDENCE_DIR/storage-stat.txt"
  local stat_values
  stat_values=$(jq -Rrse 'split("\n") | map(gsub("\r"; "")) | map(select(test("^[0-9]+ [0-9]+ [0-9]+$")))
    | if length == 3 and (unique | length) == 1 then .[0] else error("storage_stat_invalid") end' "$EVIDENCE_DIR/storage-stat.txt")
  local uid gid mode
  read -r uid gid mode <<<"$stat_values"
  verify_receipt verify-storage-job storage-job verify-storage-job --execution "$EVIDENCE_DIR/execution-provision-storage.json" \
    --owner-uid "$uid" --owner-gid "$gid" --mode "0${mode}"
}
stage_probe_workload() {
  load_inventory
  local label
  for label in a b; do
    stage_workload acceptance "$label" "${REVISION_SUFFIX}-${label}"
    az_capture containerapp revision label add --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
      --label "$label" --revision "${APP_NAME}--${REVISION_SUFFIX}-${label}" >"$EVIDENCE_DIR/label-${label}.json"
  done
  az_capture containerapp ingress traffic set --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
    --label-weight a=50 b=50 >"$EVIDENCE_DIR/traffic-set.json"
  az_capture containerapp ingress traffic show --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" >"$EVIDENCE_DIR/traffic.json"
  wait_http_ready "https://${APP_NAME}---a.${APP_DOMAIN}" label-a
  wait_http_ready "https://${APP_NAME}---b.${APP_DOMAIN}" label-b
}
prepare_auth() (
  test -z "${ELSPETH_ACCEPTANCE_BEARER_TOKEN:-}" || exit 0
  : "${ELSPETH_ACCEPTANCE_USERNAME:?set local username or bearer token}"
  : "${ELSPETH_ACCEPTANCE_PASSWORD:?set local password or bearer token}"
  local scratch status cleanup
  scratch=$(mktemp -d -p /tmp elspeth-auth.XXXXXX)
  printf -v cleanup 'rm -rf -- %q' "$scratch"
  trap "$cleanup" EXIT
  jq -n '{username:env.ELSPETH_ACCEPTANCE_USERNAME,password:env.ELSPETH_ACCEPTANCE_PASSWORD,
    display_name:env.ELSPETH_ACCEPTANCE_USERNAME}' >"$scratch/register.json"
  status=$(protected_capture http http_request_failed curl --silent --show-error \
    --max-filesize "$ELSPETH_COMMAND_OUTPUT_LIMIT_BYTES" \
    --header 'Content-Type: application/json' --data-binary "@$scratch/register.json" \
    --output "$scratch/response.json" --write-out '%{http_code}' \
    "https://${APP_NAME}---a.${APP_DOMAIN}/api/auth/register")
  if test "$status" = 409; then
    jq 'del(.display_name)' "$scratch/register.json" >"$scratch/login.json"
    status=$(curl_capture --header 'Content-Type: application/json' --data-binary "@$scratch/login.json" \
      --output "$scratch/response.json" --write-out '%{http_code}' \
      "https://${APP_NAME}---a.${APP_DOMAIN}/api/auth/login")
  fi
  test "$status" = 200 || { fail authentication_failed; exit 1; }
  jq -er 'select(.token_type == "bearer") | .access_token | select(type == "string" and length > 0)' "$scratch/response.json"
)
api_post() (
  local path="$1" body="$2" out="$3" scratch cleanup
  : "${ELSPETH_ACCEPTANCE_BEARER_TOKEN:?set acceptance bearer token}"
  [[ "$ELSPETH_ACCEPTANCE_BEARER_TOKEN" != *$'\n'* && "$ELSPETH_ACCEPTANCE_BEARER_TOKEN" != *$'\r'* ]] || exit 1
  scratch=$(mktemp -d -p /tmp elspeth-http.XXXXXX)
  printf -v cleanup 'rm -rf -- %q' "$scratch"
  trap "$cleanup" EXIT
  printf 'Authorization: Bearer %s\n' "$ELSPETH_ACCEPTANCE_BEARER_TOKEN" >"$scratch/header"
  curl_capture --header "@$scratch/header" --header 'Content-Type: application/json' \
    --data-binary "@$body" --output "$out" "${PREPARATION_ORIGIN:-https://${APP_NAME}---a.${APP_DOMAIN}}${path}"
)
prepare_session() {
  local name="$1" yaml="${2:-}" session blob
  api_post /api/sessions "$EVIDENCE_DIR/empty-body.json" "$EVIDENCE_DIR/prepared-${name}.json"
  session=$(jq -er '.id | select(test("^[0-9a-f-]{36}$"))' "$EVIDENCE_DIR/prepared-${name}.json")
  if test -n "$yaml"; then
    api_post "/api/sessions/${session}/blobs/inline" "$PROBE_SOURCE_BLOB" "$EVIDENCE_DIR/prepared-${name}-blob.json"
    blob=$(jq -er '.id | select(test("^[0-9a-f-]{36}$"))' "$EVIDENCE_DIR/prepared-${name}-blob.json")
    jq -n --rawfile yaml "$yaml" --arg source "${PROBE_SOURCE_NAME:-input}" --arg blob "$blob" \
      '{yaml:$yaml, source_blob_ids:{($source):$blob}}' >"$EVIDENCE_DIR/prepared-${name}-import.json"
    api_post "/api/sessions/${session}/state/yaml" "$EVIDENCE_DIR/prepared-${name}-import.json" "$EVIDENCE_DIR/prepared-${name}-state.json"
  fi
  printf '%s\n' "$session"
}
prepare_guided_trials() {
  local prefix="$1" trials="${PROBE_TRIALS:-20}" index session operation turn
  positive_integer "$trials"
  test "$trials" -ge 20 || fail probe_trials_insufficient
  : >"$EVIDENCE_DIR/${prefix}-trial-requests.jsonl"
  for ((index=0; index<trials; index++)); do
    session=$(prepare_session "${prefix}-${index}")
    operation=$(cat /proc/sys/kernel/random/uuid)
    jq -n --arg operation "$operation" --arg intent "$P1_INTENT" \
      '{operation_id:$operation,profile:"live",intent:$intent}' >"$EVIDENCE_DIR/${prefix}-${index}-start.json"
    api_post "/api/sessions/${session}/guided/start" "$EVIDENCE_DIR/${prefix}-${index}-start.json" "$EVIDENCE_DIR/${prefix}-${index}-turn.json"
    turn=$(jq -er '.next_turn.turn_token | select(test("^[0-9a-f]{64}$"))' "$EVIDENCE_DIR/${prefix}-${index}-turn.json")
    operation=$(cat /proc/sys/kernel/random/uuid)
    jq -c --arg session "$session" --arg operation "$operation" --arg turn "$turn" \
      '{session_id:$session,body:(. + {operation_id:$operation,turn_token:$turn})}' "$P1_BODY" >>"$EVIDENCE_DIR/${prefix}-trial-requests.jsonl"
  done
  jq -s '.' "$EVIDENCE_DIR/${prefix}-trial-requests.jsonl" >"$EVIDENCE_DIR/${prefix}-trial-requests.json"
}
stage_prepare() {
  PREPARATION_ORIGIN="https://${APP_NAME}---a.${APP_DOMAIN}"
  require_file "${PROBE_YAML:?set executable P2/P4 pipeline YAML}"
  require_file "${P3_YAML:?set long-running physical CSV sink pipeline YAML}"
  require_file "${PROBE_SOURCE_BLOB:?set inline source blob request JSON}"
  require_file "${P4_MESSAGE_BODY:?set a normal Composer message JSON to observe}"
  require_file "${P1_BODY:?set the guided action template JSON}"
  : "${P1_INTENT:?set guided acceptance intent}"
  local trials="${PROBE_TRIALS:-20}" index token
  positive_integer "$trials"
  test "$trials" -ge 20 || fail probe_trials_insufficient
  if test -z "${ELSPETH_ACCEPTANCE_BEARER_TOKEN:-}"; then
    token=$(prepare_auth)
    export ELSPETH_ACCEPTANCE_BEARER_TOKEN="$token"
    unset ELSPETH_ACCEPTANCE_USERNAME ELSPETH_ACCEPTANCE_PASSWORD
  fi
  printf '{}\n' >"$EVIDENCE_DIR/empty-body.json"
  P3_SESSION_ID=$(prepare_session p3 "$P3_YAML")
  P4_SESSION_ID=$(prepare_session p4 "$PROBE_YAML")
  api_post "/api/sessions/${P4_SESSION_ID}/messages" "$P4_MESSAGE_BODY" "$EVIDENCE_DIR/prepared-p4-message.json"
  : >"$EVIDENCE_DIR/p2-session-ids.txt"
  prepare_guided_trials p1
  for ((index=0; index<trials; index++)); do
    prepare_session "p2-${index}" "$PROBE_YAML" >>"$EVIDENCE_DIR/p2-session-ids.txt"
  done
  P1_TRIAL_REQUESTS="$EVIDENCE_DIR/p1-trial-requests.json"
  P2_SESSION_IDS="$EVIDENCE_DIR/p2-session-ids.json"
  jq -Rsc 'split("\n") | map(select(length > 0))' "$EVIDENCE_DIR/p2-session-ids.txt" >"$P2_SESSION_IDS"
  jq -n --arg p3 "$P3_SESSION_ID" --arg p4 "$P4_SESSION_ID" \
    '{p3:$p3,p4:$p4}' >"$EVIDENCE_DIR/prepared-sessions.json"
}
probe_receipt() {
  local probe="$1" kind="$2"
  shift 2
  local status=0
  facade replica-probes "${BINDING_ARGS[@]}" --probe "$probe" "$@" >"$EVIDENCE_DIR/${kind}.stream" || status=$?
  # A scored failed probe still emits a receipt. Preserve it, then let the
  # bundle refuse; a transport/schema error has no receipt and stops here.
  store_exec_receipt "$kind" "$kind"
  if test "$status" -ne 0; then PROBE_FAILED=1; fi
}
stage_probes() {
  if test -f "$EVIDENCE_DIR/prepared-sessions.json"; then
    P3_SESSION_ID="${P3_SESSION_ID:-$(jq -er '.p3' "$EVIDENCE_DIR/prepared-sessions.json")}"
    P4_SESSION_ID="${P4_SESSION_ID:-$(jq -er '.p4' "$EVIDENCE_DIR/prepared-sessions.json")}"
    P2_SESSION_IDS="${P2_SESSION_IDS:-$EVIDENCE_DIR/p2-session-ids.json}"
    P1_TRIAL_REQUESTS="${P1_TRIAL_REQUESTS:-$EVIDENCE_DIR/p1-trial-requests.json}"
  fi
  : "${P1_TRIAL_REQUESTS:?set fresh guided trial requests JSON}"
  : "${P2_SESSION_IDS:?set fresh executable sessions JSON array}"
  : "${P4_SESSION_ID:?set prepared progress session}" "${P3_SESSION_ID:?set prepared long-run session}"
  : "${P3_SINK_PATH:?set shared NFS physical CSV path}" "${P3_SINK_KEY_FIELD:?set stable unique CSV key}"
  P3_SINK_PATH="${P3_SINK_PATH//\{session_id\}/$P3_SESSION_ID}"
  local trials="${PROBE_TRIALS:-20}"
  positive_integer "$trials"
  test "$trials" -ge 20 || fail probe_trials_insufficient
  require_file "$P1_TRIAL_REQUESTS"
  require_file "$P2_SESSION_IDS"
  bind_replica "${APP_NAME}--${REVISION_SUFFIX}-b"
  export PROBE_WINDOW_START
  PROBE_WINDOW_START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  printf '%s\n' "$PROBE_WINDOW_START" >"$EVIDENCE_DIR/probe-window-start.txt"
  date -u +%Y-%m-%dT%H:%M:00Z >"$EVIDENCE_DIR/budget-window-start.txt"
  local common=(--app-name "$APP_NAME" --resource-group "$RESOURCE_GROUP" --default-domain "$APP_DOMAIN" --revision-suffix "$REVISION_SUFFIX")
  probe_receipt fence-conflict replica-fence-conflict "${common[@]}" --traffic "$EVIDENCE_DIR/traffic.json" \
    --trial-requests "$P1_TRIAL_REQUESTS" --trials "$trials"
  probe_receipt run-start replica-run-start "${common[@]}" --traffic "$EVIDENCE_DIR/traffic.json" \
    --session-ids "$P2_SESSION_IDS" --trials "$trials"
  mkdir -m 0700 "$EVIDENCE_DIR/p4" "$EVIDENCE_DIR/p3"
  protected_capture probe observation_collection_failed "$PYTHON" -m elspeth.web.azure_container_apps_observations progress \
    "${common[@]}" --session-id "$P4_SESSION_ID" --evidence-dir "$EVIDENCE_DIR/p4" >"$EVIDENCE_DIR/progress-observation.json"
  probe_receipt progress replica-progress --observation "$EVIDENCE_DIR/progress-observation.json"
  RESTORE_ROLES=1
  protected_capture probe observation_collection_failed "$PYTHON" -m elspeth.web.azure_container_apps_observations takeover \
    "${common[@]}" --session-id "$P3_SESSION_ID" \
    --sink-path "$P3_SINK_PATH" --sink-key-field "$P3_SINK_KEY_FIELD" \
    --evidence-dir "$EVIDENCE_DIR/p3" >"$EVIDENCE_DIR/takeover-observation.json"
  probe_receipt lease-takeover replica-lease-takeover --observation "$EVIDENCE_DIR/takeover-observation.json"
  restore_roles
  export PROBE_WINDOW_END
  PROBE_WINDOW_END=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  printf '%s\n' "$PROBE_WINDOW_END" >"$EVIDENCE_DIR/probe-window-end.txt"
}
single_revision_receipt() {
  local probe="$1" kind="$2" name="$3"
  shift 3
  local status=0 directory="$EVIDENCE_DIR/${name}"
  protected_capture probe single_revision_probe_failed "$PYTHON" -m elspeth.web.azure_container_apps_single_revision \
    --probe "$probe" --app-json "$EVIDENCE_DIR/app.json" --replicas-json "$EVIDENCE_DIR/replicas-${REVISION}.json" \
    --revision "$REVISION" --candidate-sha "$CANDIDATE_SHA" --scenario-id A --evidence-dir "$directory" \
    "$@" >"$EVIDENCE_DIR/${name}.stream" || status=$?
  # Discovery owns the selected cookie-pinned replica. Never substitute the
  # first control-plane replica for the actual owner recorded by the probe.
  CONTAINER_APP_ID=$(jq -er '.container_app_id' "$directory/binding.json")
  REVISION=$(jq -er '.revision' "$directory/binding.json")
  REPLICA=$(jq -er '.replica' "$directory/binding.json")
  BINDING_ARGS=(--candidate-sha "$CANDIDATE_SHA" --scenario-id A --container-app-id "$CONTAINER_APP_ID" --revision "$REVISION" --replica "$REPLICA")
  SUBJECT="${CONTAINER_APP_ID}/revisions/${REVISION}/replicas/${REPLICA}"
  store_exec_receipt "$kind" "$name"
  jq -n --arg app "$CONTAINER_APP_ID" --arg revision "$REVISION" --arg replica "$REPLICA" \
    '{app:$app,revision:$revision,replica:$replica}' >"$EVIDENCE_DIR/binding.json"
  if test "$status" -ne 0; then PROBE_FAILED=1; fi
}
stage_single_revision() {
  : "${ELSPETH_ACCEPTANCE_BEARER_TOKEN:?set existing acceptance bearer token}"
  : "${P1_INTENT:?set guided acceptance intent}"
  require_file "${P1_BODY:?set guided action template JSON}"
  require_file "${PROBE_YAML:?set executable P4 pipeline YAML}"
  require_file "${PROBE_SOURCE_BLOB:?set inline source blob request JSON}"
  require_file "${P4_MESSAGE_BODY:?set normal Composer message JSON}"
  printf '{}\n' >"$EVIDENCE_DIR/empty-body.json"
  stage_rollout "${REVISION_SUFFIX}-single"
  az_capture containerapp show --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" >"$EVIDENCE_DIR/app.json"
  PREPARATION_ORIGIN="https://$(jq -er '.properties.configuration.ingress.fqdn' "$EVIDENCE_DIR/app.json")"
  prepare_guided_trials single-p1
  local session
  session=$(prepare_session single-p4 "$PROBE_YAML")
  api_post "/api/sessions/${session}/messages" "$P4_MESSAGE_BODY" "$EVIDENCE_DIR/prepared-single-p4-message.json"
  single_revision_receipt P1 single-revision-fence-conflict single-p1 \
    --trial-requests "$EVIDENCE_DIR/single-p1-trial-requests.json" --trials "${PROBE_TRIALS:-20}"
  single_revision_receipt P4a single-revision-progress single-p4 --session-id "$session"
  unset PREPARATION_ORIGIN
  PROBE_WINDOW_END=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  printf '%s\n' "$PROBE_WINDOW_END" >"$EVIDENCE_DIR/probe-window-end.txt"
}

kql_capture() {
  local query_file="$1" out="$2" query window_start="$PROBE_WINDOW_START"
  if test "${query_file##*/}" = doctor-report.kql; then
    window_start=$(cat "$EVIDENCE_DIR/jobs-window-start.txt")
  fi
  query=$(cat "$query_file")
  query=${query//__WINDOW_START__/$window_start}
  query=${query//__WINDOW_END__/${PROBE_WINDOW_END:?}}
  query=${query//__APP_NAME__/$APP_NAME}
  query=${query//__JOB_NAME__/doctor-runtime-a}
  local execution
  execution=$(cat "$EVIDENCE_DIR/execution-doctor-runtime-a.txt")
  query=${query//__EXECUTION_NAME__/$execution}
  query=${query//__SENTINEL_HASH__/${SENTINEL_HASH:?set probe sentinel hash}}
  ELSPETH_AZ_CALL_CEILING_SECONDS="$ELSPETH_LOG_QUERY_CEILING_SECONDS" az_capture monitor log-analytics query \
    --workspace "$LOG_ANALYTICS_CUSTOMER_ID" --analytics-query "$query" >"$out"
  sha256sum "$query_file" >>"$EVIDENCE_DIR/kql.sha256"
}
stage_evidence() {
  load_binding
  PROBE_WINDOW_START=$(cat "$EVIDENCE_DIR/probe-window-start.txt")
  PROBE_WINDOW_END=$(cat "$EVIDENCE_DIR/probe-window-end.txt")
  local name deadline=$((SECONDS + ELSPETH_LOG_QUERY_CEILING_SECONDS)) lag
  local queries=()
  for name in doctor-report run-sentinel-by-replica replica-lifecycle fence-conflict-409; do
    while :; do
      kql_capture "$BUNDLE_DIR/kql/${name}.kql" "$EVIDENCE_DIR/${name}.json"
      if jq -e 'type == "array" and length > 0' "$EVIDENCE_DIR/${name}.json" >/dev/null; then break; fi
      test "$SECONDS" -lt "$deadline" || { fail log_ingestion_timeout; return 1; }
      sleep "$ELSPETH_POLL_SECONDS"
    done
    # Measure ingestion lag on the same source table and bounded window.
    local table=ContainerAppConsoleLogs_CL
    if test "$name" = replica-lifecycle; then table=ContainerAppSystemLogs_CL; fi
    az_capture monitor log-analytics query --workspace "$LOG_ANALYTICS_CUSTOMER_ID" \
      --analytics-query "$table | where TimeGenerated between (datetime($PROBE_WINDOW_START) .. datetime($PROBE_WINDOW_END)) | extend Lag = (ingestion_time() - TimeGenerated) / 1s | summarize Lag = max(Lag)" \
      >"$EVIDENCE_DIR/${name}-lag.json"
    lag=$(jq -er '.[0].Lag | select(type == "number" and . >= 0)' "$EVIDENCE_DIR/${name}-lag.json")
    queries+=(--query "$name" "$EVIDENCE_DIR/${name}.json" "$BUNDLE_DIR/kql/${name}.kql" "$lag")
  done
  verify_receipt verify-log-analytics log-analytics verify-log-analytics --workspace-id "$LOG_ANALYTICS_CUSTOMER_ID" "${queries[@]}"
  stage_connection_budget
}
stage_connection_budget() {
  local start end start_epoch end_epoch now deadline=$((SECONDS + 660))
  start=$(cat "$EVIDENCE_DIR/budget-window-start.txt")
  start_epoch=$(date -u -d "$start" +%s)
  end_epoch=$((start_epoch + 600))
  end=$(date -u -d "@$end_epoch" +%Y-%m-%dT%H:%M:%SZ)
  printf '%s\n' "$end" >"$EVIDENCE_DIR/budget-window-end.txt"
  # Azure PT1M points name the minute's start. Do not query a partial last
  # bucket or feed arbitrary probe seconds to the exact ten-minute validator.
  while :; do
    now=$(date -u +%s)
    if test "$now" -ge "$end_epoch"; then break; fi
    test "$SECONDS" -lt "$deadline" || { fail connection_window_timeout; return 1; }
    sleep "$ELSPETH_POLL_SECONDS"
  done
  deadline=$((SECONDS + ELSPETH_LOG_QUERY_CEILING_SECONDS))
  while :; do
    az_capture monitor metrics list --resource "$POSTGRES_RESOURCE_ID" --metric active_connections --interval PT1M \
      --aggregation Maximum --start-time "$start" --end-time "$end" >"$EVIDENCE_DIR/active-connections.json"
    if facade verify-connection-budget "${BINDING_ARGS[@]}" --metrics "$EVIDENCE_DIR/active-connections.json" \
      --window-start "$start" --acceptance-run-id "$ACCEPTANCE_RUN_ID" --server-id "$POSTGRES_RESOURCE_ID" \
      --max-connections "${PG_MAX_CONNECTIONS:?}" --approved-budget "${PG_APPROVED_BUDGET:?}" --safety-margin "${PG_SAFETY_MARGIN:?}" \
      >"$EVIDENCE_DIR/connection-budget.stream"; then
      store_exec_receipt verify-connection-budget connection-budget
      return 0
    fi
    test "$SECONDS" -lt "$deadline" || { fail connection_budget_evidence_timeout; return 1; }
    sleep "$ELSPETH_POLL_SECONDS"
  done
}
stage_receipts() {
  load_binding
  : "${COMPATIBILITY_RECORD:?set operator compatibility record}" "${TESTCONTAINER_RECEIPT:?set provisioned PostgreSQL receipt}"
  az_capture containerapp revision show --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
    --revision "${APP_NAME}--${REVISION_SUFFIX}" >"$EVIDENCE_DIR/candidate-revision.json"
  az_capture containerapp job show --name doctor-runtime-a --resource-group "$RESOURCE_GROUP" >"$EVIDENCE_DIR/candidate-doctor.json"
  local revision_hash doctor_hash junit_hash
  jq -cSj '.properties.template' "$EVIDENCE_DIR/candidate-revision.json" >"$EVIDENCE_DIR/candidate-revision-template.json"
  jq -cSj '.properties.template' "$EVIDENCE_DIR/candidate-doctor.json" >"$EVIDENCE_DIR/candidate-doctor-template.json"
  read -r revision_hash _ < <(sha256sum "$EVIDENCE_DIR/candidate-revision-template.json")
  read -r doctor_hash _ < <(sha256sum "$EVIDENCE_DIR/candidate-doctor-template.json")
  verify_receipt compatibility-record compatibility-record compatibility-record-validate --record "$COMPATIBILITY_RECORD" \
    --acceptance-run-id "$ACCEPTANCE_RUN_ID" --candidate-image-digest "$CANDIDATE_IMAGE_DIGEST" \
    --candidate-revision-sha256 "$revision_hash" --candidate-doctor-job-sha256 "$doctor_hash"
  junit_hash=$(jq -er '.junit_sha256' "$TESTCONTAINER_RECEIPT")
  facade receipt-store --store-dir "$RECEIPT_DIR" --kind testcontainer-run --scenario-id A --subject-id "$junit_hash" \
    --candidate-sha "$CANDIDATE_SHA" --receipt-file "$TESTCONTAINER_RECEIPT" >"$EVIDENCE_DIR/testcontainer-receipt.sha256"
}
restore_roles() {
  if test "$RESTORE_ROLES" = 1; then
    facade restore-owner --role elspeth_runtime_a >"$EVIDENCE_DIR/restore-a.json"
    facade restore-owner --role elspeth_runtime_b >"$EVIDENCE_DIR/restore-b.json"
    RESTORE_ROLES=0
  fi
}
stage_cleanup() {
  export ELSPETH_CLEANUP_MODE=1
  az_deploy_capture group delete --name "$RESOURCE_GROUP" --yes >"$EVIDENCE_DIR/group-delete.json"
  local deadline=$((SECONDS + ELSPETH_JOB_WAIT_SECONDS)) remaining
  while :; do
    az_capture graph query -q "Resources | where resourceGroup =~ '${RESOURCE_GROUP}' | count" >"$EVIDENCE_DIR/resource-graph.json"
    remaining=$(jq -er '.data[0].Count' "$EVIDENCE_DIR/resource-graph.json")
    if test "$remaining" = 0; then break; fi
    test "$SECONDS" -lt "$deadline" || { fail resource_graph_cleanup_timeout; return 1; }
    sleep "$ELSPETH_POLL_SECONDS"
  done
  local vault_args=() vault_names=() name role scheduled
  if test -f "$EVIDENCE_DIR/binding.json"; then
    test -n "${KEY_VAULT_NAME:-}" && test -n "${SCHEMA_OWNER_KEY_VAULT_NAME:-}" \
      && test "$KEY_VAULT_NAME" != "$SCHEMA_OWNER_KEY_VAULT_NAME" \
      || { fail cleanup_vault_unresolved; return 1; }
  fi
  # A failed ARM deployment may not return outputs. Discover every tombstone
  # in this disposable group, and also retain the known output inventory.
  az_capture keyvault list-deleted >"$EVIDENCE_DIR/deleted-vaults.json"
  jq -r --arg group "/resourcegroups/${RESOURCE_GROUP,,}/" \
    --arg runtime "${KEY_VAULT_NAME:-}" --arg owner "${SCHEMA_OWNER_KEY_VAULT_NAME:-}" \
    '[.[] | select(.properties.vaultId | ascii_downcase | contains($group)) | .name]
    + [$runtime, $owner] | map(select(length > 0)) | unique | .[]' \
    "$EVIDENCE_DIR/deleted-vaults.json" >"$EVIDENCE_DIR/cleanup-vault-names.txt"
  mapfile -t vault_names <"$EVIDENCE_DIR/cleanup-vault-names.txt"
  for name in "${vault_names[@]}"; do
    role=""
    if test "$name" = "${KEY_VAULT_NAME:-}"; then role=runtime; fi
    if test "$name" = "${SCHEMA_OWNER_KEY_VAULT_NAME:-}"; then role=schema-owner; fi
    if az_capture keyvault purge --name "$name" --location "$AZURE_LOCATION" >"$EVIDENCE_DIR/vault-${name}-purge.json"; then
      if test -n "$role"; then vault_args+=("--${role}-key-vault-purged"); fi
    else
      az_capture keyvault show-deleted --name "$name" --location "$AZURE_LOCATION" >"$EVIDENCE_DIR/vault-${name}-tombstone.json"
      scheduled=$(jq -er '.properties.scheduledPurgeDate' "$EVIDENCE_DIR/vault-${name}-tombstone.json")
      if test -n "$role"; then vault_args+=("--${role}-scheduled-purge-date" "$scheduled"); fi
    fi
  done
  if test -f "$EVIDENCE_DIR/binding.json"; then
    load_binding
    verify_receipt resource-graph-cleanup resource-graph-cleanup resource-graph-cleanup-validate \
      --count "$EVIDENCE_DIR/resource-graph.json" --resource-group "$RESOURCE_GROUP" "${vault_args[@]}"
  fi
  CLEANED_UP=1
}
on_exit() {
  local original="$?" cleanup_status=0
  trap - EXIT
  # Independent cleanup subprocesses retain errexit: one failed operation must
  # never be hidden by a later successful command inside an `if` condition.
  if test "$RESTORE_ROLES" = 1; then
    "$BASH" "$BUNDLE_DIR/scripts/acceptance.sh" restore >"$EVIDENCE_DIR/restore.log" 2>&1 || cleanup_status=$?
  fi
  if test "$GROUP_MAY_EXIST" = 1 && test "$CLEANED_UP" = 0; then
    "$BASH" "$BUNDLE_DIR/scripts/acceptance.sh" cleanup >"$EVIDENCE_DIR/cleanup.log" 2>&1 || cleanup_status=$?
  fi
  if test "$cleanup_status" -ne 0; then
    printf '%s\n' cleanup_failed >&2
    if test "$original" = 0; then original="$cleanup_status"; fi
  fi
  exit "$original"
}
main() {
  local stage="${1:-all}"
  require_inputs
  case "$stage" in
    environment) stage_environment ;;
    image) stage_image ;;
    bootstrap) load_inventory; stage_bootstrap ;;
    testcontainer) load_database_environment; stage_testcontainer ;;
    jobs) stage_jobs ;;
    workload-production) load_inventory; stage_rollout ;;
    workload-probes) stage_probe_workload ;;
    prepare) load_inventory; stage_prepare ;;
    probes)
      : "${ELSPETH_ACCEPTANCE_BEARER_TOKEN:?set existing acceptance bearer token}"
      load_inventory
      load_database_environment
      trap on_exit EXIT
      stage_probes
      test "$PROBE_FAILED" = 0
      ;;
    single-revision)
      load_inventory
      load_database_environment
      stage_single_revision
      test "$PROBE_FAILED" = 0
      ;;
    evidence) load_inventory; stage_evidence ;;
    connection-budget) load_inventory; load_binding; stage_connection_budget ;;
    receipts) stage_receipts ;;
    restore) RESTORE_ROLES=1; restore_roles ;;
    cleanup) if test -f "$EVIDENCE_DIR/inventory.json"; then load_inventory; fi; stage_cleanup ;;
    bundle) facade bundle-validate --store-dir "$RECEIPT_DIR" --candidate-sha "$CANDIDATE_SHA" --scenario-id A >"$EVIDENCE_DIR/bundle.json" ;;
    all)
      preflight_all
      trap on_exit EXIT
      stage_environment
      load_inventory
      stage_image
      stage_bootstrap
      stage_jobs
      stage_testcontainer
      stage_rollout
      stage_receipts
      stage_probe_workload
      stage_prepare
      stage_probes
      stage_single_revision
      stage_evidence
      stage_cleanup
      facade bundle-validate --store-dir "$RECEIPT_DIR" --candidate-sha "$CANDIDATE_SHA" --scenario-id A >"$EVIDENCE_DIR/bundle.json"
      test "$PROBE_FAILED" = 0
      ;;
    *) fail stage_invalid; return 64 ;;
  esac
}
main "$@"
