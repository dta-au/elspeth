// Production workload: one revision, session affinity on, N replicas.
//   az deployment group create --resource-group <rg> --template-file workload.bicep \
//     --parameters workload.production.bicepparam \
//     --parameters image=<registry>/elspeth@sha256:<digest> revisionSuffix=<sha12>
// Every secret URL is a VERSIONED Key Vault reference; replace the placeholders
// with the outputs of environment.bicep and the versions you created.
using 'workload.bicep'

param environmentResourceId = '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/elspeth-prod/providers/Microsoft.App/managedEnvironments/elspeth-env'
param identityResourceId = '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/elspeth-prod/providers/Microsoft.ManagedIdentity/userAssignedIdentities/elspeth-id'
param nfsStorageName = 'elspeth-nfs'
param containerAppName = 'elspeth-web'
param image = 'elspethregistry.azurecr.io/elspeth@sha256:0000000000000000000000000000000000000000000000000000000000000000'
param provisionStorageImage = 'mcr.microsoft.com/azurelinux/base/core@sha256:0000000000000000000000000000000000000000000000000000000000000000'
param revisionSuffix = 'candidate'
param activeRevisionsMode = 'Single'
param stickySessionsAffinity = 'sticky'
param minReplicas = 2
param maxReplicas = 4
param terminationGracePeriodSeconds = 60
// Ingress request timeout is a fixed 240 s; keep headroom (facts §2.2).
param composerTransportIdleCeilingSeconds = 210
param runtimeRoleLabel = ''
param webCpu = '1.0'
param webMemory = '2Gi'
param sessionDbUrlRuntimeSecretUrl = 'https://elspeth-kv-example.vault.azure.net/secrets/elspeth-session-db-url-runtime/00000000000000000000000000000000'
param landscapeUrlRuntimeSecretUrl = 'https://elspeth-kv-example.vault.azure.net/secrets/elspeth-landscape-url-runtime/00000000000000000000000000000000'
param sessionDbUrlSchemaOwnerSecretUrl = 'https://elspeth-kv-example.vault.azure.net/secrets/elspeth-session-db-url-schema-owner/00000000000000000000000000000000'
param landscapeUrlSchemaOwnerSecretUrl = 'https://elspeth-kv-example.vault.azure.net/secrets/elspeth-landscape-url-schema-owner/00000000000000000000000000000000'
param secretKeySecretUrl = 'https://elspeth-kv-example.vault.azure.net/secrets/elspeth-secret-key/00000000000000000000000000000000'
param shareableLinkSigningKeySecretUrl = 'https://elspeth-kv-example.vault.azure.net/secrets/elspeth-shareable-link-signing-key/00000000000000000000000000000000'
param fingerprintKeySecretUrl = 'https://elspeth-kv-example.vault.azure.net/secrets/elspeth-fingerprint-key/00000000000000000000000000000000'
param operatorMetricsBearerTokenSecretUrl = 'https://elspeth-kv-example.vault.azure.net/secrets/elspeth-operator-metrics-bearer-token/00000000000000000000000000000000'
param composerEndpointApiKeySecretUrl = ''
param extraEnvironment = []
param tags = {
  'elspeth.stack': 'production'
}
