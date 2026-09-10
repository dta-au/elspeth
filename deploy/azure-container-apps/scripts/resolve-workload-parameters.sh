#!/usr/bin/env bash
# Cold-install parameter materialisation. All Azure calls read secret IDs only.
# Redeploy reuses this file to preserve pinned secret versions and configuration.
set -Eeuo pipefail
umask 077
test "$#" -eq 2 || { echo 'usage: resolve-workload-parameters.sh ENVIRONMENT_OUTPUTS OUTPUT_JSON' >&2; exit 2; }
: "${CANDIDATE_IMAGE:?digest-pinned runtime image required}"
: "${CANDIDATE_SHA:?full source SHA required}"
: "${PROVISION_STORAGE_IMAGE:?digest-pinned root provisioner image required}"
: "${ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS:?explicit transport ceiling required}"
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
output=$2
test ! -e "$output" || { echo 'output already exists; use a new candidate file' >&2; exit 2; }
vault=$(jq -er '.keyVaultName.value' "$1")
schema_vault=$(jq -er '.schemaOwnerKeyVaultName.value' "$1")
parameters=$(mktemp)
trap 'rm -f -- "$parameters"' EXIT
# Start with an ARM parameter envelope, not the tracked compilation fixture.
# Optional sizing/probe settings retain the workload template defaults.
jq -n '{
  "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
  contentVersion: "1.0.0.0",
  parameters: {
    containerAppName: {value: "elspeth-web"},
    activeRevisionsMode: {value: "Single"}, stickySessionsAffinity: {value: "sticky"},
    minReplicas: {value: 2}, maxReplicas: {value: 4}
  }
}' >"$parameters"
# Freeze the version returned now. No secret value is written or printed.
while read -r parameter secret_name; do
  secret_vault=$vault
  if [[ "$secret_name" == *-schema-owner ]]; then secret_vault=$schema_vault; fi
  secret_id=$(az keyvault secret show --vault-name "$secret_vault" --name "$secret_name" --query id --output tsv --only-show-errors)
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
# Set COMPOSER_ENDPOINT_SECRET_NAME only if the deployment uses that endpoint.
composer_secret=''
if [[ -n ${COMPOSER_ENDPOINT_SECRET_NAME:-} ]]; then
  composer_secret=$(az keyvault secret show --vault-name "$vault" --name "$COMPOSER_ENDPOINT_SECRET_NAME" --query id --output tsv --only-show-errors)
fi
document=$(jq --slurpfile outputs "$1" --arg image "$CANDIDATE_IMAGE" \
  --arg sha "$CANDIDATE_SHA" --arg provisioner "$PROVISION_STORAGE_IMAGE" \
  --arg composer "$composer_secret" \
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
  .parameters.composerEndpointApiKeySecretUrl.value = $composer
  ' "$parameters")
printf '%s\n' "$document" >"$parameters"
jq -e -f "$script_dir/validate-workload-parameters.jq" "$parameters" >/dev/null || {
  echo 'unresolved or invalid workload parameters; no output written' >&2; exit 2;
}
# Noclobber also protects against an output appearing during the read-only work.
(set -o noclobber; cat "$parameters" >"$output")
