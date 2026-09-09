// ELSPETH on Azure Container Apps — composition (subscription scope).
//
// Creates the resource group and deploys environment.bicep into it. The
// workload (app + Jobs) is deployed separately with workload.bicep at
// resource-group scope so an image rollout never re-plans the platform.
targetScope = 'subscription'

@description('Resource group that owns every resource of this stack. The acceptance uses one disposable group per run.')
param resourceGroupName string

@description('Azure region.')
param location string

@description('Acceptance run id; empty for a production stack. Tags the resource group so Resource Graph can prove cleanup.')
param acceptanceRunId string = ''

@description('Tags applied to every resource.')
param tags object = {}

@minLength(3)
@maxLength(12)
param namePrefix string = 'elspeth'
param containerRegistryResourceId string
param vnetAddressPrefix string = '10.60.0.0/16'
param infrastructureSubnetPrefix string = '10.60.0.0/23'
param privateEndpointSubnetPrefix string = '10.60.2.0/24'
param zoneRedundant bool = true
param logAnalyticsRetentionDays int = 90
param keyVaultPurgeProtection bool = true
param keyVaultAllowedIpRules array = []
param postgresAdministratorLogin string
@secure()
param postgresAdministratorPassword string
param postgresVersion string = '17'
param postgresTier string = 'GeneralPurpose'
param postgresSkuName string = 'Standard_D2ds_v5'
param postgresHighAvailability string = 'Disabled'
param postgresGeoRedundantBackup string = 'Disabled'
param postgresPublicNetworkAccess string = 'Disabled'
param postgresFirewallRules array = []
param postgresThreatProtection string = 'Enabled'
param nfsShareQuotaGiB int = 100

var groupTags = union(tags, empty(acceptanceRunId) ? {} : { 'elspeth.acceptance-run-id': acceptanceRunId })

resource resourceGroup 'Microsoft.Resources/resourceGroups@2024-11-01' = {
  name: resourceGroupName
  location: location
  tags: groupTags
}

module environment 'environment.bicep' = {
  name: '${namePrefix}-environment-stack'
  scope: resourceGroup
  params: {
    namePrefix: namePrefix
    location: location
    tags: groupTags
    containerRegistryResourceId: containerRegistryResourceId
    vnetAddressPrefix: vnetAddressPrefix
    infrastructureSubnetPrefix: infrastructureSubnetPrefix
    privateEndpointSubnetPrefix: privateEndpointSubnetPrefix
    zoneRedundant: zoneRedundant
    logAnalyticsRetentionDays: logAnalyticsRetentionDays
    keyVaultPurgeProtection: keyVaultPurgeProtection
    keyVaultAllowedIpRules: keyVaultAllowedIpRules
    postgresAdministratorLogin: postgresAdministratorLogin
    postgresAdministratorPassword: postgresAdministratorPassword
    postgresVersion: postgresVersion
    postgresTier: postgresTier
    postgresSkuName: postgresSkuName
    postgresHighAvailability: postgresHighAvailability
    postgresGeoRedundantBackup: postgresGeoRedundantBackup
    postgresPublicNetworkAccess: postgresPublicNetworkAccess
    postgresFirewallRules: postgresFirewallRules
    postgresThreatProtection: postgresThreatProtection
    nfsShareQuotaGiB: nfsShareQuotaGiB
  }
}

output resourceGroupName string = resourceGroup.name
output environmentResourceId string = environment.outputs.environmentResourceId
output environmentName string = environment.outputs.environmentName
output environmentDefaultDomain string = environment.outputs.environmentDefaultDomain
output identityResourceId string = environment.outputs.identityResourceId
output identityPrincipalId string = environment.outputs.identityPrincipalId
output identityClientId string = environment.outputs.identityClientId
output keyVaultName string = environment.outputs.keyVaultName
output keyVaultUri string = environment.outputs.keyVaultUri
output postgresServerResourceId string = environment.outputs.postgresServerResourceId
output postgresFqdn string = environment.outputs.postgresFqdn
output logAnalyticsWorkspaceResourceId string = environment.outputs.logAnalyticsWorkspaceResourceId
output logAnalyticsCustomerId string = environment.outputs.logAnalyticsCustomerId
output fileStorageAccountName string = environment.outputs.fileStorageAccountName
output nfsShareName string = environment.outputs.nfsShareName
output nfsStorageName string = environment.outputs.nfsStorageName
output blobStorageAccountName string = environment.outputs.blobStorageAccountName
output payloadContainerName string = environment.outputs.payloadContainerName
output registryPullRoleAssignmentId string = environment.outputs.registryPullRoleAssignmentId
