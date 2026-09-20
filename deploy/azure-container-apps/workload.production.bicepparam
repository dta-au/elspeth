// Production workload: one revision, session affinity on, N replicas.
//   az deployment group create --resource-group <rg> --template-file workload.bicep \
//     --parameters workload.production.bicepparam \
//     --parameters image=<registry>/elspeth@sha256:<digest> revisionSuffix=<sha12>
// Every secret URL is a VERSIONED Key Vault reference; replace the placeholders
// with the outputs of environment.bicep and the versions you created.
using 'workload.bicep'

param environmentResourceId = '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/elspeth-prod/providers/Microsoft.App/managedEnvironments/elspeth-env'
param identityResourceId = '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/elspeth-prod/providers/Microsoft.ManagedIdentity/userAssignedIdentities/elspeth-id'
param identityClientId = '00000000-0000-0000-0000-000000000000'
param schemaOwnerIdentityResourceId = '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/elspeth-prod/providers/Microsoft.ManagedIdentity/userAssignedIdentities/elspeth-schema-owner-id'
param nfsStorageName = 'elspeth'
param candidateSourceSha = '0000000000000000000000000000000000000000'
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
param composerMaxCompositionTurns = 50
param composerMaxDiscoveryTurns = 20
param composerTimeoutSeconds = 180
param composerRateLimitPerMinute = 10
param authProvider = 'entra'
param registrationMode = 'closed'
param composerModel = 'gpt-5.5'
param composerAdvisorModel = 'anthropic/claude-sonnet-4-6'
param runtimeRoleLabel = ''
param webCpu = '1.0'
param webMemory = '2Gi'
param sessionDbUrlRuntimeSecretUrl = 'https://elspeth-kv-example.vault.azure.net/secrets/elspeth-session-db-url-runtime/00000000000000000000000000000000'
param landscapeUrlRuntimeSecretUrl = 'https://elspeth-kv-example.vault.azure.net/secrets/elspeth-landscape-url-runtime/00000000000000000000000000000000'
param sessionDbUrlSchemaOwnerSecretUrl = 'https://elspeth-skv-example.vault.azure.net/secrets/elspeth-session-db-url-schema-owner/00000000000000000000000000000000'
param landscapeUrlSchemaOwnerSecretUrl = 'https://elspeth-skv-example.vault.azure.net/secrets/elspeth-landscape-url-schema-owner/00000000000000000000000000000000'
param secretKeySecretUrl = 'https://elspeth-kv-example.vault.azure.net/secrets/elspeth-secret-key/00000000000000000000000000000000'
param shareableLinkSigningKeySecretUrl = 'https://elspeth-kv-example.vault.azure.net/secrets/elspeth-shareable-link-signing-key/00000000000000000000000000000000'
param fingerprintKeySecretUrl = 'https://elspeth-kv-example.vault.azure.net/secrets/elspeth-fingerprint-key/00000000000000000000000000000000'
param operatorMetricsBearerTokenSecretUrl = 'https://elspeth-kv-example.vault.azure.net/secrets/elspeth-operator-metrics-bearer-token/00000000000000000000000000000000'
// Replace model IDs, endpoints and versioned credentials with your deployed services.
param composerEndpointBaseUrl = 'https://primary.example.com/v1'
param composerEndpointApiKeySecretUrl = 'https://elspeth-kv-example.vault.azure.net/secrets/composer-primary-key/00000000000000000000000000000000'
param composerAdvisorEndpointBaseUrl = 'https://advisor.example.com/v1'
param composerAdvisorEndpointApiKeySecretUrl = 'https://elspeth-kv-example.vault.azure.net/secrets/composer-advisor-key/00000000000000000000000000000000'
param extraSecrets = [
  {
    name: 'sso-client-secret'
    keyVaultUrl: 'https://elspeth-kv-example.vault.azure.net/secrets/sso-client-secret/00000000000000000000000000000000'
  }
  {
    name: 'sso-transaction-secret'
    keyVaultUrl: 'https://elspeth-kv-example.vault.azure.net/secrets/sso-transaction-secret/00000000000000000000000000000000'
  }
]
param extraEnvironment = [
  { name: 'ELSPETH_WEB__ENTRA_TENANT_ID', value: '00000000-0000-0000-0000-000000000000' }
  { name: 'ELSPETH_WEB__SSO_CLIENT_ID', value: '00000000-0000-0000-0000-000000000000' }
  { name: 'ELSPETH_WEB__SSO_CLIENT_SECRET', secretRef: 'sso-client-secret' }
  { name: 'ELSPETH_WEB__SSO_TRANSACTION_SECRET', secretRef: 'sso-transaction-secret' }
  { name: 'ELSPETH_WEB__PUBLIC_BASE_URL', value: 'https://elspeth.example.com' }
  { name: 'ELSPETH_WEB__COMPARTMENT_ID', value: 'soft-launch' }
  { name: 'ELSPETH_WEB__QUOTA_DEFAULT_TOKENS_PER_DAY', value: '1000000' }
  { name: 'ELSPETH_WEB__QUOTA_DEFAULT_STORAGE_BYTES', value: '1073741824' }
]
param tags = {
  'elspeth.stack': 'production'
}
