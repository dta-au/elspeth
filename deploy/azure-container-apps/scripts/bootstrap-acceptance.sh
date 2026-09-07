#!/usr/bin/env bash
# Create the fresh acceptance's SQL roles, KV versions and resolved parameters.
# Runs only when explicitly invoked by the operator's acceptance controller.
set -Eeuo pipefail
umask 077
test "$#" -eq 3 || { echo 'usage: bootstrap-acceptance.sh INVENTORY_JSON SECRET_DIR OUTPUT_DIR' >&2; exit 2; }
: "${MAIN_PARAMETERS:?concrete environment ARM parameter JSON required}"
: "${PGSSLROOTCERT:?operator PostgreSQL trust bundle path required}"
: "${BOOTSTRAP_PRINCIPAL_ID:?object id of the operator writing Key Vault secrets}"
: "${BOOTSTRAP_PRINCIPAL_TYPE:?User or ServicePrincipal}"
: "${CANDIDATE_IMAGE:?verified digest-pinned candidate image required}"
: "${CANDIDATE_SHA:?full candidate source SHA required}"
: "${PROVISION_STORAGE_IMAGE:?verified digest-pinned root provisioner required}"
: "${ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS:?explicit transport ceiling required}"
case "$BOOTSTRAP_PRINCIPAL_TYPE" in User|ServicePrincipal) ;; *) echo 'invalid bootstrap principal type' >&2; exit 2 ;; esac
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
inventory=$1
secret_dir=$2
output_dir=$3
mkdir -p "$output_dir"
chmod 700 "$output_dir"
for name in workload.parameters.json workload-a.parameters.json workload-b.parameters.json; do
  test ! -e "$output_dir/$name" || { echo 'bootstrap output exists; do not rerun cold bootstrap' >&2; exit 2; }
done
private_dir=$(mktemp -d "$output_dir/bootstrap-private.XXXXXX")
trap 'rm -rf -- "$private_dir"' EXIT

# Bounded subprocess output stays private and is never replayed into receipts.
capture() {
  local output=$1
  shift
  local code=0
  (ulimit -f 4096; timeout --signal=TERM --kill-after=5s 900 "$@" >"$output" 2>"$private_dir/stderr") || code=$?
  if (( code != 0 )); then
    cp -- "$private_dir/stderr" "$output_dir/bootstrap-error.log"
    echo 'acceptance bootstrap command failed; bootstrap has not completed' >&2
    return "$code"
  fi
}

wait_for_keyvault_rbac() {
  local wait_seconds=${KEY_VAULT_RBAC_WAIT_SECONDS:-600}
  [[ "$wait_seconds" =~ ^[1-9][0-9]*$ ]] && (( wait_seconds <= 600 )) || {
    echo 'invalid Key Vault RBAC wait budget' >&2; return 2;
  }
  local deadline=$((SECONDS + wait_seconds)) remaining command_timeout delay code
  while (( SECONDS < deadline )); do
    remaining=$((deadline - SECONDS))
    command_timeout=$((remaining < 30 ? remaining : 30))
    code=0
    (ulimit -f 4096; timeout --signal=TERM --kill-after=5s "$command_timeout" \
      az keyvault secret set --vault-name "$vault" --name elspeth-secret-key \
      --file "$secret_dir/elspeth-secret-key" --encoding utf-8 --query id --output tsv --only-show-errors \
      >"$private_dir/elspeth-secret-key.version" 2>"$private_dir/stderr") || code=$?
    if (( code == 0 )); then return 0; fi
    # Only this explicit data-plane RBAC refusal is eligible for propagation
    # retry after the just-created assignment. Probe the required WRITE action
    # using the first real secret; a pre-existing Reader grant is insufficient.
    # Firewall/network/other errors
    # retain their failure code and do not repeat any SQL or secret writes.
    if [[ $(<"$private_dir/stderr") != *ForbiddenByRbac* ]]; then
      cp -- "$private_dir/stderr" "$output_dir/bootstrap-error.log"
      echo 'key_vault_access_probe_failed' >&2
      return "$code"
    fi
    remaining=$((deadline - SECONDS))
    if (( remaining > 0 )); then
      delay=$((remaining < 10 ? remaining : 10))
      sleep "$delay"
    fi
  done
  cp -- "$private_dir/stderr" "$output_dir/bootstrap-error.log"
  echo 'key_vault_rbac_propagation_timeout' >&2
  return 124
}

for name in elspeth-schema-owner-password elspeth-runtime-password elspeth-runtime-a-password elspeth-runtime-b-password \
  elspeth-secret-key elspeth-shareable-link-signing-key elspeth-fingerprint-key elspeth-operator-metrics-bearer-token; do
  test -s "$secret_dir/$name" || { echo 'required operator secret file missing or empty' >&2; exit 2; }
done
export PGHOST PGUSER PGPASSWORD PGDATABASE=postgres PGSSLMODE=verify-full PGCONNECT_TIMEOUT=30
PGHOST=$(jq -er '.postgresFqdn.value' "$inventory")
PGUSER=$(jq -er '.parameters.postgresAdministratorLogin.value | select(length > 0)' "$MAIN_PARAMETERS")
PGPASSWORD=$(jq -er '.parameters.postgresAdministratorPassword.value | select(length > 0)' "$MAIN_PARAMETERS")
vault=$(jq -er '.keyVaultName.value' "$inventory")
capture "$private_dir/vault-id" az keyvault show --name "$vault" --query id --output tsv --only-show-errors
vault_id=$(cat "$private_dir/vault-id")
capture "$private_dir/role.json" az role assignment create --assignee-object-id "$BOOTSTRAP_PRINCIPAL_ID" \
  --assignee-principal-type "$BOOTSTRAP_PRINCIPAL_TYPE" --role 'Key Vault Secrets Officer' --scope "$vault_id" --only-show-errors
wait_for_keyvault_rbac

# Do not create non-idempotent SQL roles until Key Vault access has propagated.
export ELSPETH_SCHEMA_OWNER_PASSWORD ELSPETH_RUNTIME_PASSWORD ELSPETH_RUNTIME_A_PASSWORD ELSPETH_RUNTIME_B_PASSWORD
ELSPETH_SCHEMA_OWNER_PASSWORD=$(cat "$secret_dir/elspeth-schema-owner-password")
ELSPETH_RUNTIME_PASSWORD=$(cat "$secret_dir/elspeth-runtime-password")
ELSPETH_RUNTIME_A_PASSWORD=$(cat "$secret_dir/elspeth-runtime-a-password")
ELSPETH_RUNTIME_B_PASSWORD=$(cat "$secret_dir/elspeth-runtime-b-password")
capture "$private_dir/bootstrap-sql.log" psql --no-psqlrc --set=ON_ERROR_STOP=1 --file "$script_dir/bootstrap-acceptance-roles.sql"
unset PGPASSWORD ELSPETH_SCHEMA_OWNER_PASSWORD ELSPETH_RUNTIME_PASSWORD ELSPETH_RUNTIME_A_PASSWORD ELSPETH_RUNTIME_B_PASSWORD

# Build database URLs from raw password files using URL encoding. No value is
# passed to a child process through argv; psql read the same password bytes.
for role in schema-owner runtime runtime-a runtime-b; do
  for database in session-db landscape; do
    if [[ "$database" == session-db ]]; then database_name=elspeth_sessions; else database_name=elspeth_landscape; fi
    jq -nj --rawfile password "$secret_dir/elspeth-${role}-password" \
      --arg role "elspeth_${role//-/_}" --arg host "$PGHOST" --arg database "$database_name" '
      "postgresql+psycopg://" + $role + ":" + ($password | sub("\\n+$"; "") | @uri) + "@" + $host + ":5432/" +
      $database + "?sslmode=verify-full&sslrootcert=system"
      ' >"$private_dir/elspeth-${database}-url-${role}"
  done
done
for name in elspeth-secret-key elspeth-shareable-link-signing-key elspeth-fingerprint-key elspeth-operator-metrics-bearer-token; do
  cp -- "$secret_dir/$name" "$private_dir/$name"
done
if [[ -n ${COMPOSER_ENDPOINT_SECRET_NAME:-} ]]; then
  [[ "$COMPOSER_ENDPOINT_SECRET_NAME" =~ ^[a-zA-Z0-9-]+$ ]]
  test -s "$secret_dir/$COMPOSER_ENDPOINT_SECRET_NAME"
  cp -- "$secret_dir/$COMPOSER_ENDPOINT_SECRET_NAME" "$private_dir/$COMPOSER_ENDPOINT_SECRET_NAME"
fi
for value_file in "$private_dir"/elspeth-*-url-* "$private_dir"/elspeth-secret-key \
  "$private_dir"/elspeth-shareable-link-signing-key "$private_dir"/elspeth-fingerprint-key \
  "$private_dir"/elspeth-operator-metrics-bearer-token; do
  name=${value_file##*/}
  # The permission proof already wrote this exact value and captured its version.
  if [[ "$name" == elspeth-secret-key ]]; then continue; fi
  capture "$private_dir/$name.version" az keyvault secret set --vault-name "$vault" --name "$name" \
    --file "$value_file" --encoding utf-8 --query id --output tsv --only-show-errors
done
if [[ -n ${COMPOSER_ENDPOINT_SECRET_NAME:-} ]]; then
  capture "$private_dir/composer.version" az keyvault secret set --vault-name "$vault" --name "$COMPOSER_ENDPOINT_SECRET_NAME" \
    --file "$private_dir/$COMPOSER_ENDPOINT_SECRET_NAME" --encoding utf-8 --query id --output tsv --only-show-errors
fi
capture "$private_dir/resolve.log" bash "$script_dir/resolve-workload-parameters.sh" "$inventory" "$output_dir/workload.parameters.json"
document=$(jq --slurpfile inventory "$inventory" '
  .parameters.verifyBlobManagedIdentity.value = true |
  .parameters.blobAccountUrl.value = ("https://" + $inventory[0].blobStorageAccountName.value + ".blob.core.windows.net") |
  .parameters.blobContainerName.value = $inventory[0].payloadContainerName.value |
  .parameters.identityClientId.value = $inventory[0].identityClientId.value
  ' "$output_dir/workload.parameters.json")
printf '%s\n' "$document" >"$output_dir/workload.parameters.json"
for role in a b; do
  session_version=$(cat "$private_dir/elspeth-session-db-url-runtime-${role}.version")
  landscape_version=$(cat "$private_dir/elspeth-landscape-url-runtime-${role}.version")
  jq --arg role "$role" --arg session "$session_version" --arg landscape "$landscape_version" '
    .parameters.sessionDbUrlRuntimeSecretUrl.value = $session |
    .parameters.landscapeUrlRuntimeSecretUrl.value = $landscape |
    .parameters.runtimeRoleLabel.value = $role |
    .parameters.activeRevisionsMode.value = "Multiple" |
    .parameters.stickySessionsAffinity.value = "none" |
    .parameters.minReplicas.value = 1 | .parameters.maxReplicas.value = 1
    ' "$output_dir/workload.parameters.json" >"$output_dir/workload-${role}.parameters.json"
done

# The host-side observers need SQL connections too. Keep this credentials
# envelope local and mode 0600; it is never a receipt or command-line argument.
jq -n --slurpfile main "$MAIN_PARAMETERS" --arg host "$PGHOST" --arg cert "$PGSSLROOTCERT" \
  --rawfile role_a "$private_dir/elspeth-session-db-url-runtime-a" \
  --rawfile role_b "$private_dir/elspeth-session-db-url-runtime-b" '
  ("?sslmode=verify-full&sslrootcert=" + ($cert | @uri)) as $tls |
  ("postgresql+psycopg://" + ($main[0].parameters.postgresAdministratorLogin.value | @uri) + ":" +
   ($main[0].parameters.postgresAdministratorPassword.value | @uri) + "@" + $host + ":5432/") as $admin |
  {
    ELSPETH_ACCEPTANCE_PG_ADMIN_URL: ($admin + "elspeth_sessions" + $tls),
    ELSPETH_ACCEPTANCE_PG_RUNTIME_A_URL: (($role_a | split("?")[0]) + $tls),
    ELSPETH_ACCEPTANCE_PG_RUNTIME_B_URL: (($role_b | split("?")[0]) + $tls),
    ELSPETH_ACCEPTANCE_SESSION_DB_URL: ($admin + "elspeth_sessions" + $tls),
    ELSPETH_ACCEPTANCE_LANDSCAPE_URL: ($admin + "elspeth_landscape" + $tls),
    ELSPETH_TEST_POSTGRES_URL: ($admin + "postgres" + $tls)
  }
  ' >"$output_dir/acceptance-env.json"
