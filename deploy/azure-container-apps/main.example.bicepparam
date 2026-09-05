// Production stack: resource group + environment (subscription-scope deployment).
//   az deployment sub create --location <region> --template-file main.bicep \
//     --parameters main.example.bicepparam
// Replace every placeholder; the administrator password is read from the
// environment, never written into this file.
using 'main.bicep'

param resourceGroupName = 'elspeth-prod'
param location = 'australiaeast'
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
