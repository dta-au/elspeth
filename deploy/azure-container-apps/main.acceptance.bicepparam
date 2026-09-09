// Disposable acceptance stack (plan §10, one resource group per run).
// The acceptance runbook overrides resourceGroupName, acceptanceRunId, the
// operator IP and the registry id on the command line.
using 'main.bicep'

param resourceGroupName = 'elspeth-acc-RUN-ID'
param location = 'australiaeast'
param acceptanceRunId = 'RUN-ID'
param namePrefix = 'elspeth'
param containerRegistryResourceId = '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/shared-registry/providers/Microsoft.ContainerRegistry/registries/elspethregistry'
param zoneRedundant = false
param logAnalyticsRetentionDays = 30
// Purge protection off so the vault can be purged at cleanup (facts §0 C8).
param keyVaultPurgeProtection = false
// The operator host writes secrets from outside the VNet.
param keyVaultAllowedIpRules = [
  '203.0.113.7'
]
param postgresAdministratorLogin = 'elspeth_admin'
param postgresAdministratorPassword = readEnvironmentVariable('ELSPETH_POSTGRES_ADMIN_PASSWORD', '')
param postgresVersion = '17'
param postgresTier = 'Burstable'
param postgresSkuName = 'Standard_B2s'
param postgresHighAvailability = 'Disabled'
param postgresGeoRedundantBackup = 'Disabled'
// Public access plus an operator-IP firewall rule so psql runs from the
// operator host; the app still reaches the server over the private endpoint.
param postgresPublicNetworkAccess = 'Enabled'
param postgresFirewallRules = [
  {
    name: 'operator-host'
    startIpAddress: '203.0.113.7'
    endIpAddress: '203.0.113.7'
  }
]
param postgresThreatProtection = 'Disabled'
param tags = {
  'elspeth.stack': 'acceptance'
}
