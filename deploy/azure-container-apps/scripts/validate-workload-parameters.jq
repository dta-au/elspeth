# Validate the resolved production launch before what-if. No secret values.
def concrete: type == "string" and length > 0 and (test("00000000|<|>|example|placeholder|REPLACE_"; "i") | not);
def digest: concrete and test("^[a-z0-9.-]+(:[0-9]+)?/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$");
def secret: concrete and test("^https://[a-z0-9-]+\\.vault\\.azure\\.net/secrets/[a-zA-Z0-9-]+/[0-9a-f]{32}$");
def positive_integer: type == "number" and floor == . and . >= 1;
def endpoint: concrete and test("^https://[^/@?#[:space:]]+(/[^?#[:space:]]*)?$");
def endpoint_pair($url; $key):
  (($url == "" and $key == "") or (($url | endpoint) and ($key | secret)));
def reserved_secrets: ["secret-key", "shareable-link-signing-key", "fingerprint-key",
  "operator-metrics-bearer-token", "session-db-url", "landscape-url",
  "session-db-url-a", "session-db-url-b", "landscape-url-a", "landscape-url-b",
  "composer-endpoint-api-key", "composer-advisor-endpoint-api-key"];
def reserved_environment: [
  "ELSPETH_WEB__OPERATOR_TELEMETRY_RELEASE", "ELSPETH_ACCEPTANCE_CANDIDATE_SHA",
  "ELSPETH_WEB__DEPLOYMENT_TARGET", "ELSPETH_WEB__DEPLOYMENT_STATE_MODE",
  "ELSPETH_WEB__HOST", "ELSPETH_WEB__PORT", "WEB_CONCURRENCY",
  "ELSPETH_WEB__LOG_JSON", "ELSPETH_WEB__OPERATOR_TELEMETRY",
  "ELSPETH_WEB__DATA_DIR", "ELSPETH_WEB__PAYLOAD_STORE_PATH",
  "ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS",
  "ELSPETH_WEB__COMPOSER_TRANSPORT_HEADROOM_SECONDS",
  "ELSPETH_WEB__SESSION_DB_URL", "ELSPETH_WEB__LANDSCAPE_URL",
  "ELSPETH_WEB__SECRET_KEY", "ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY",
  "ELSPETH_FINGERPRINT_KEY", "ELSPETH_WEB__OPERATOR_METRICS_BEARER_TOKEN",
  "AZURE_CLIENT_ID", "ELSPETH_WEB__COMPOSER_MAX_COMPOSITION_TURNS",
  "ELSPETH_WEB__COMPOSER_MAX_DISCOVERY_TURNS", "ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS",
  "ELSPETH_WEB__COMPOSER_RATE_LIMIT_PER_MINUTE", "ELSPETH_WEB__AUTH_PROVIDER",
  "ELSPETH_WEB__REGISTRATION_MODE", "ELSPETH_WEB__COMPOSER_ENDPOINT_BASE_URL",
  "ELSPETH_WEB__COMPOSER_ENDPOINT_API_KEY", "ELSPETH_WEB__COMPOSER_ADVISOR_ENDPOINT_BASE_URL",
  "ELSPETH_WEB__COMPOSER_ADVISOR_ENDPOINT_API_KEY", "ELSPETH_WEB__COMPOSER_MODEL",
  "ELSPETH_WEB__COMPOSER_ADVISOR_MODEL"];
.parameters | map_values(.value) |
(.sessionDbUrlSchemaOwnerSecretUrl | split("/")[2]) as $owner_vault |
(.sessionDbUrlRuntimeSecretUrl | split("/")[2]) as $runtime_vault |
(if has("extraSecrets") then .extraSecrets else [] end) as $extra_secrets |
(if has("extraEnvironment") then .extraEnvironment else [] end) as $extra_environment |
(["secret-key", "shareable-link-signing-key", "fingerprint-key", "operator-metrics-bearer-token",
  "session-db-url", "landscape-url"] +
  (if (.composerEndpointApiKeySecretUrl // "") != "" then ["composer-endpoint-api-key"] else [] end) +
  (if (.composerAdvisorEndpointApiKeySecretUrl // "") != "" then ["composer-advisor-endpoint-api-key"] else [] end) +
  [$extra_secrets[].name]) as $available_secrets |
([.. | strings] | all(test("REPLACE_"; "i") | not)) and
(.environmentResourceId | concrete and test("^/subscriptions/[0-9a-f-]{36}/resourceGroups/[^/]+/providers/Microsoft.App/managedEnvironments/[^/]+$"; "i")) and
(.identityResourceId | concrete and test("^/subscriptions/[0-9a-f-]{36}/resourceGroups/[^/]+/providers/Microsoft.ManagedIdentity/userAssignedIdentities/[^/]+$"; "i")) and
(.identityClientId | concrete and test("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"; "i")) and
(.schemaOwnerIdentityResourceId | concrete and test("^/subscriptions/[0-9a-f-]{36}/resourceGroups/[^/]+/providers/Microsoft.ManagedIdentity/userAssignedIdentities/[^/]+$"; "i")) and
((.schemaOwnerIdentityResourceId | ascii_downcase) != (.identityResourceId | ascii_downcase)) and
(.nfsStorageName | concrete) and (.containerAppName | concrete) and
(.image | digest) and (.provisionStorageImage | digest) and
(.candidateSourceSha | concrete and test("^[0-9a-f]{40}$")) and
(.revisionSuffix | concrete and test("^[a-z][a-z0-9-]*[a-z0-9]$") and length <= 64 and (contains("--") | not)) and
(.composerTransportIdleCeilingSeconds | positive_integer and . <= 240) and
([.composerMaxCompositionTurns, .composerMaxDiscoveryTurns,
  .composerTimeoutSeconds, .composerRateLimitPerMinute] | all(positive_integer)) and
(.composerTimeoutSeconds <= (.composerTransportIdleCeilingSeconds - 30)) and
(.authProvider | IN("oidc", "entra", "vanguard", "google")) and
(.registrationMode | IN("closed", "email_verified", "open")) and
([.sessionDbUrlRuntimeSecretUrl, .landscapeUrlRuntimeSecretUrl,
  .sessionDbUrlSchemaOwnerSecretUrl, .landscapeUrlSchemaOwnerSecretUrl,
  .secretKeySecretUrl, .shareableLinkSigningKeySecretUrl,
  .fingerprintKeySecretUrl, .operatorMetricsBearerTokenSecretUrl] | all(secret)) and
endpoint_pair((if has("composerEndpointBaseUrl") then .composerEndpointBaseUrl else "" end); (.composerEndpointApiKeySecretUrl // "")) and
endpoint_pair((if has("composerAdvisorEndpointBaseUrl") then .composerAdvisorEndpointBaseUrl else "" end); (.composerAdvisorEndpointApiKeySecretUrl // "")) and
(if has("composerModel") then (.composerModel | concrete) else true end) and
(if has("composerAdvisorModel") then (.composerAdvisorModel | concrete) else true end) and
($owner_vault != $runtime_vault) and
([.sessionDbUrlSchemaOwnerSecretUrl, .landscapeUrlSchemaOwnerSecretUrl] | all(split("/")[2] == $owner_vault)) and
([.sessionDbUrlRuntimeSecretUrl, .landscapeUrlRuntimeSecretUrl, .secretKeySecretUrl,
  .shareableLinkSigningKeySecretUrl, .fingerprintKeySecretUrl, .operatorMetricsBearerTokenSecretUrl,
  (.composerEndpointApiKeySecretUrl | select(. != null and . != "")),
  (.composerAdvisorEndpointApiKeySecretUrl | select(. != null and . != ""))] | all(split("/")[2] == $runtime_vault)) and
(.minReplicas | positive_integer) and (.maxReplicas | positive_integer) and
(.maxReplicas >= .minReplicas) and
(.activeRevisionsMode == "Single" and .stickySessionsAffinity == "sticky") and
($extra_secrets | type == "array" and all(
  type == "object" and keys == ["keyVaultUrl", "name"] and
  (.name | type == "string" and test("^[a-z0-9][a-z0-9-]{0,252}$")) and
  (.name as $name | reserved_secrets | index($name) == null) and
  (.keyVaultUrl | secret and split("/")[2] == $runtime_vault))) and
([$extra_secrets[].name] | length == (unique | length)) and
($extra_environment | type == "array" and all(
  type == "object" and (keys == ["name", "value"] or keys == ["name", "secretRef"]) and
  (.name | type == "string" and test("^[A-Za-z_][A-Za-z0-9_]*$")) and
  (.name as $name | reserved_environment | index($name | ascii_upcase) == null) and
  (if has("secretRef") then
    (.secretRef as $ref | $available_secrets | index($ref) != null)
   else (.value | type == "string") end))) and
([$extra_environment[].name | ascii_upcase] | length == (unique | length))
