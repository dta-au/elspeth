// Environment only, into an EXISTING resource group (the cold-install runbook).
//   az deployment group create --resource-group <rg> --template-file environment.bicep \
//     --parameters environment.example.bicepparam
using 'environment.bicep'

param namePrefix = 'elspeth'
param containerRegistryResourceId = '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/shared-registry/providers/Microsoft.ContainerRegistry/registries/elspethregistry'
param zoneRedundant = true
param logAnalyticsRetentionDays = 90
param keyVaultPurgeProtection = true
param keyVaultAllowedIpRules = []
param postgresAdministratorLogin = 'elspeth_admin'
param postgresAdministratorPassword = readEnvironmentVariable('ELSPETH_POSTGRES_ADMIN_PASSWORD', '')
param postgresVersion = '17'
param postgresTier = 'GeneralPurpose'
param postgresSkuName = 'Standard_D2ds_v5'
param postgresHighAvailability = 'ZoneRedundant'
param postgresGeoRedundantBackup = 'Enabled'
param postgresPublicNetworkAccess = 'Disabled'
param postgresFirewallRules = []
param postgresThreatProtection = 'Enabled'
param tags = {
  'elspeth.stack': 'production'
}
