# Fail closed before what-if; these are operator inputs, never secret values.
def concrete: type == "string" and length > 0 and (test("00000000|<|>|example|placeholder"; "i") | not);
def digest: concrete and test("^[a-z0-9.-]+(:[0-9]+)?/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$");
def secret: concrete and test("^https://[a-z0-9-]+\\.vault\\.azure\\.net/secrets/[a-z0-9-]+/[0-9a-f]{32}$");
.parameters | map_values(.value) |
(.environmentResourceId | concrete and test("^/subscriptions/[0-9a-f-]{36}/resourceGroups/[^/]+/providers/Microsoft.App/managedEnvironments/[^/]+$"; "i")) and
(.identityResourceId | concrete and test("^/subscriptions/[0-9a-f-]{36}/resourceGroups/[^/]+/providers/Microsoft.ManagedIdentity/userAssignedIdentities/[^/]+$"; "i")) and
(.nfsStorageName | concrete) and (.containerAppName | concrete) and
(.image | digest) and (.provisionStorageImage | digest) and
(.candidateSourceSha | concrete and test("^[0-9a-f]{40}$")) and
(.revisionSuffix | concrete and test("^[a-z][a-z0-9-]*[a-z0-9]$") and length <= 64 and (contains("--") | not)) and
(.composerTransportIdleCeilingSeconds | type == "number" and floor == . and . >= 1 and . <= 240) and
([.sessionDbUrlRuntimeSecretUrl, .landscapeUrlRuntimeSecretUrl,
  .sessionDbUrlSchemaOwnerSecretUrl, .landscapeUrlSchemaOwnerSecretUrl,
  .secretKeySecretUrl, .shareableLinkSigningKeySecretUrl,
  .fingerprintKeySecretUrl, .operatorMetricsBearerTokenSecretUrl] | all(secret)) and
(.composerEndpointApiKeySecretUrl | . == "" or secret) and
(.sessionDbUrlRuntimeSecretUrl != .sessionDbUrlSchemaOwnerSecretUrl) and
(.landscapeUrlRuntimeSecretUrl != .landscapeUrlSchemaOwnerSecretUrl) and
(.minReplicas >= 1 and .maxReplicas >= .minReplicas) and
(.activeRevisionsMode == "Single" and .stickySessionsAffinity == "sticky")
