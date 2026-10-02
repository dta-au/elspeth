#!/usr/bin/env bash
# Materialise launch configuration with the exact IDs captured during upload.
# APPLICATION_PARAMETERS is a flat JSON map of launch parameter names to values.
# SECRET_VERSION_DIR contains <secret-name>.version files; Azure is never queried.
set -Eeuo pipefail
umask 077
test "$#" -eq 2 || { echo 'usage: resolve-workload-parameters.sh ENVIRONMENT_OUTPUTS OUTPUT_JSON' >&2; exit 2; }
: "${CANDIDATE_IMAGE:?digest-pinned runtime image required}"
: "${CANDIDATE_SHA:?full source SHA required}"
: "${PROVISION_STORAGE_IMAGE:?digest-pinned root provisioner image required}"
: "${ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS:?explicit transport ceiling required}"
: "${SECRET_VERSION_DIR:?directory of captured secret version IDs required}"
: "${APPLICATION_PARAMETERS:?flat application parameter JSON file required}"
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
output=$2
test ! -e "$output" || { echo 'output already exists; use a new candidate file' >&2; exit 2; }
vault=$(jq -er '.keyVaultName.value' "$1")
schema_vault=$(jq -er '.schemaOwnerKeyVaultName.value' "$1")
parameters=$(mktemp)
trap 'rm -f -- "$parameters"' EXIT
# Infrastructure, image, database URLs and secret version pins are never supplied
# by this file. Reject unknown keys rather than silently dropping misspellings.
jq -e '
  ["containerAppName", "minReplicas", "maxReplicas", "webCpu", "webMemory",
   "terminationGracePeriodSeconds", "tags", "composerMaxCompositionTurns",
   "composerMaxDiscoveryTurns", "composerTimeoutSeconds", "composerRateLimitPerMinute",
   "authProvider", "registrationMode", "composerEndpointBaseUrl",
   "composerAdvisorEndpointBaseUrl", "composerModel", "composerAdvisorModel",
   "extraSecrets", "extraEnvironment"] as $allowed |
  if type != "object" then error("application parameters must be a flat JSON object")
  elif ((keys - $allowed) | length) != 0 then error("unknown or reserved application parameter")
  else . end |
  {containerAppName: "elspeth-web", minReplicas: 2, maxReplicas: 4,
   activeRevisionsMode: "Single", stickySessionsAffinity: "sticky",
   registrationMode: "closed", composerEndpointBaseUrl: "",
   composerAdvisorEndpointBaseUrl: "", extraSecrets: [], extraEnvironment: []} + . |
  {"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
   contentVersion: "1.0.0.0", parameters: map_values({value: .})}
' "$APPLICATION_PARAMETERS" >"$parameters"

captured_secret_id() {
  local name=$1 secret_vault=$2 secret_id
  [[ "$name" =~ ^[a-zA-Z0-9-]+$ ]] || { echo 'invalid secret name' >&2; return 2; }
  secret_id=$(cat -- "$SECRET_VERSION_DIR/$name.version")
  # Match the expected vault AND name; a swapped or malformed capture must fail.
  [[ "$secret_id" =~ ^https://[a-z0-9-]+\.vault\.azure\.net/secrets/[a-zA-Z0-9-]+/[0-9a-f]{32}$ ]] &&
    [[ "$secret_id" == "https://$secret_vault.vault.azure.net/secrets/$name/"* ]] || {
      echo "invalid captured version ID for $name" >&2; return 2;
    }
  printf '%s' "$secret_id"
}
while read -r parameter secret_name; do
  secret_vault=$vault
  if [[ "$secret_name" == *-schema-owner ]]; then secret_vault=$schema_vault; fi
  secret_id=$(captured_secret_id "$secret_name" "$secret_vault")
  document=$(jq --arg key "$parameter" --arg id "$secret_id" '.parameters[$key] = {value: $id}' "$parameters")
  printf '%s\n' "$document" >"$parameters"
done <<'SECRETS'
sessionDbUrlRuntimeSecretUrl elspeth-session-db-url-runtime
landscapeUrlRuntimeSecretUrl elspeth-landscape-url-runtime
sessionDbUrlSchemaOwnerSecretUrl elspeth-session-db-url-schema-owner
landscapeUrlSchemaOwnerSecretUrl elspeth-landscape-url-schema-owner
secretKeySecretUrl elspeth-secret-key
shareableLinkSigningKeySecretUrl elspeth-shareable-link-signing-key
fingerprintKeySecretUrl elspeth-fingerprint-key
operatorMetricsBearerTokenSecretUrl elspeth-operator-metrics-bearer-token
SECRETS
composer_secret=''
advisor_secret=''
if [[ -n ${COMPOSER_ENDPOINT_SECRET_NAME:-} ]]; then
  composer_secret=$(captured_secret_id "$COMPOSER_ENDPOINT_SECRET_NAME" "$vault")
fi
if [[ -n ${COMPOSER_ADVISOR_ENDPOINT_SECRET_NAME:-} ]]; then
  advisor_secret=$(captured_secret_id "$COMPOSER_ADVISOR_ENDPOINT_SECRET_NAME" "$vault")
fi
document=$(jq --slurpfile outputs "$1" --arg image "$CANDIDATE_IMAGE" \
  --arg sha "$CANDIDATE_SHA" --arg provisioner "$PROVISION_STORAGE_IMAGE" \
  --arg composer "$composer_secret" --arg advisor "$advisor_secret" \
  --argjson ceiling "$ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS" '
  .parameters.environmentResourceId = {value: $outputs[0].environmentResourceId.value} |
  .parameters.identityResourceId = {value: $outputs[0].identityResourceId.value} |
  .parameters.identityClientId = {value: $outputs[0].identityClientId.value} |
  .parameters.schemaOwnerIdentityResourceId = {value: $outputs[0].schemaOwnerIdentityResourceId.value} |
  .parameters.nfsStorageName = {value: $outputs[0].nfsStorageName.value} |
  .parameters.image.value = $image |
  .parameters.provisionStorageImage.value = $provisioner |
  .parameters.candidateSourceSha.value = $sha |
  .parameters.revisionSuffix.value = ("r" + $sha[0:12]) |
  .parameters.composerTransportIdleCeilingSeconds.value = $ceiling |
  .parameters.composerEndpointApiKeySecretUrl.value = $composer |
  .parameters.composerAdvisorEndpointApiKeySecretUrl.value = $advisor
  ' "$parameters")
printf '%s\n' "$document" >"$parameters"
jq -e -f "$script_dir/validate-workload-parameters.jq" "$parameters" >/dev/null || {
  echo 'unresolved or invalid workload parameters; no output written' >&2; exit 2;
}
(set -o noclobber; cat "$parameters" >"$output")
