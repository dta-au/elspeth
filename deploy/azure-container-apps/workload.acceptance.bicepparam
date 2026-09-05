// Acceptance probe shape: Multiple revision mode, one replica per revision, no
// session affinity (unsupported outside single revision mode), and a runtime
// role label. Deploy once with runtimeRoleLabel=a and once with b, each with
// its own revisionSuffix and runtime secret URLs (plan §7), then split traffic
// 50/50 by label with the CLI (an application-scope change).
using 'workload.bicep'

param environmentResourceId = '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/elspeth-acc-RUN-ID/providers/Microsoft.App/managedEnvironments/elspeth-env'
param identityResourceId = '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/elspeth-acc-RUN-ID/providers/Microsoft.ManagedIdentity/userAssignedIdentities/elspeth-id'
param nfsStorageName = 'elspeth-nfs'
param containerAppName = 'elspeth-web'
param image = 'elspethregistry.azurecr.io/elspeth@sha256:0000000000000000000000000000000000000000000000000000000000000000'
param provisionStorageImage = 'mcr.microsoft.com/azurelinux/base/core@sha256:0000000000000000000000000000000000000000000000000000000000000000'
param revisionSuffix = 'candidate-a'
param activeRevisionsMode = 'Multiple'
param stickySessionsAffinity = 'none'
param minReplicas = 1
param maxReplicas = 1
param terminationGracePeriodSeconds = 60
param composerTransportIdleCeilingSeconds = 210
param runtimeRoleLabel = 'a'
param webCpu = '1.0'
param webMemory = '2Gi'
param sessionDbUrlRuntimeSecretUrl = 'https://elspeth-kv-example.vault.azure.net/secrets/elspeth-session-db-url-runtime-a/00000000000000000000000000000000'
param landscapeUrlRuntimeSecretUrl = 'https://elspeth-kv-example.vault.azure.net/secrets/elspeth-landscape-url-runtime-a/00000000000000000000000000000000'
param sessionDbUrlSchemaOwnerSecretUrl = 'https://elspeth-kv-example.vault.azure.net/secrets/elspeth-session-db-url-schema-owner/00000000000000000000000000000000'
param landscapeUrlSchemaOwnerSecretUrl = 'https://elspeth-kv-example.vault.azure.net/secrets/elspeth-landscape-url-schema-owner/00000000000000000000000000000000'
param secretKeySecretUrl = 'https://elspeth-kv-example.vault.azure.net/secrets/elspeth-secret-key/00000000000000000000000000000000'
param shareableLinkSigningKeySecretUrl = 'https://elspeth-kv-example.vault.azure.net/secrets/elspeth-shareable-link-signing-key/00000000000000000000000000000000'
param fingerprintKeySecretUrl = 'https://elspeth-kv-example.vault.azure.net/secrets/elspeth-fingerprint-key/00000000000000000000000000000000'
param operatorMetricsBearerTokenSecretUrl = 'https://elspeth-kv-example.vault.azure.net/secrets/elspeth-operator-metrics-bearer-token/00000000000000000000000000000000'
param composerEndpointApiKeySecretUrl = ''
param extraEnvironment = []
param tags = {
  'elspeth.stack': 'acceptance'
}
