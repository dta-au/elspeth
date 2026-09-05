// ELSPETH on Azure Container Apps — environment prerequisites (resource-group scope).
//
// Composed from Azure Verified Modules pinned in
// docs/plans/2026-09-05-phase6b-azure-container-apps-platform-facts.md §1.2.
// Storage contract (plan §4, stated once): BOTH databases on Azure Database for
// PostgreSQL Flexible Server; data/, data/blobs and payloads/ on ONE NFS 4.1
// Azure Files share; SMB is not supported; no SQLite at replicas > 1.
// Azure Files carries no database.
targetScope = 'resourceGroup'

@description('Prefix for every resource name in the group.')
@minLength(3)
@maxLength(12)
param namePrefix string = 'elspeth'

@description('Azure region for every resource.')
param location string = resourceGroup().location

@description('Tags applied to every resource.')
param tags object = {}

@description('Resource id of the EXISTING container registry that build-push.yaml publishes to. The bundle never creates a registry (facts §0 C11).')
param containerRegistryResourceId string

@description('Address space of the virtual network the Container Apps environment is injected into (NFS mounts require a custom VNet; facts §3.2).')
param vnetAddressPrefix string = '10.60.0.0/16'

@description('Infrastructure subnet for the Container Apps environment (workload profiles need at least /27).')
param infrastructureSubnetPrefix string = '10.60.0.0/23'

@description('Subnet that hosts the private endpoints for storage, PostgreSQL and Key Vault.')
param privateEndpointSubnetPrefix string = '10.60.2.0/24'

@description('Whether the Container Apps environment is zone redundant. The disposable acceptance sets false.')
param zoneRedundant bool = true

@description('Log Analytics retention in days.')
@minValue(30)
@maxValue(730)
param logAnalyticsRetentionDays int = 90

@description('Key Vault purge protection. Production keeps it on; the disposable acceptance sets false so the vault can be purged at cleanup (facts §0 C8).')
param keyVaultPurgeProtection bool = true

@description('Public IPv4 addresses allowed to reach the Key Vault data plane (the operator host writing secrets). Empty keeps public network access disabled.')
param keyVaultAllowedIpRules array = []

@description('Flexible Server administrator login.')
param postgresAdministratorLogin string

@description('Flexible Server administrator password. Never stored in a parameter file; read from the environment.')
@secure()
param postgresAdministratorPassword string

@description('PostgreSQL major version. 17 is the conservative pin (facts §4.5); 18 is also supported on Azure.')
@allowed([
  '16'
  '17'
  '18'
])
param postgresVersion string = '17'

@description('Flexible Server compute tier.')
@allowed([
  'Burstable'
  'GeneralPurpose'
  'MemoryOptimized'
])
param postgresTier string = 'GeneralPurpose'

@description('Flexible Server SKU name; must match the tier.')
param postgresSkuName string = 'Standard_D2ds_v5'

@description('High availability mode. Burstable tiers support only Disabled.')
@allowed([
  'Disabled'
  'SameZone'
  'ZoneRedundant'
])
param postgresHighAvailability string = 'Disabled'

@description('Geo-redundant backup.')
@allowed([
  'Disabled'
  'Enabled'
])
param postgresGeoRedundantBackup string = 'Disabled'

@description('Public network access on the Flexible Server. Disabled in production; the acceptance enables it together with an operator-IP firewall rule so psql runs from the operator host (facts §4.5).')
@allowed([
  'Disabled'
  'Enabled'
])
param postgresPublicNetworkAccess string = 'Disabled'

@description('Firewall rules for the Flexible Server ({name, startIpAddress, endIpAddress}); only meaningful with public network access enabled.')
param postgresFirewallRules array = []

@description('Microsoft Defender for open-source relational databases on the server.')
@allowed([
  'Disabled'
  'Enabled'
])
param postgresThreatProtection string = 'Enabled'

@description('Quota of the NFS share in GiB.')
@minValue(100)
param nfsShareQuotaGiB int = 100

// ---------------------------------------------------------------------------
// Names
// ---------------------------------------------------------------------------
var suffix = uniqueString(resourceGroup().id)
var fileStorageAccountName = toLower(take(replace('${namePrefix}fs${suffix}', '-', ''), 24))
var blobStorageAccountName = toLower(take(replace('${namePrefix}bs${suffix}', '-', ''), 24))
var keyVaultName = take('${namePrefix}-kv-${suffix}', 24)
var postgresServerName = '${namePrefix}-pg-${suffix}'
var nfsShareName = 'elspeth'
var nfsStorageName = 'elspeth-nfs'
var payloadContainerName = 'elspeth-payloads'
var registrySubscriptionId = split(containerRegistryResourceId, '/')[2]
var registryResourceGroupName = split(containerRegistryResourceId, '/')[4]
var registryName = last(split(containerRegistryResourceId, '/'))

// ---------------------------------------------------------------------------
// Identity and observability
// ---------------------------------------------------------------------------
module identity 'br/public:avm/res/managed-identity/user-assigned-identity:0.6.0' = {
  name: '${namePrefix}-identity'
  params: {
    name: '${namePrefix}-id'
    location: location
    tags: tags
  }
}

module logAnalytics 'br/public:avm/res/operational-insights/workspace:0.16.1' = {
  name: '${namePrefix}-log-analytics'
  params: {
    name: '${namePrefix}-law'
    location: location
    tags: tags
    dataRetention: logAnalyticsRetentionDays
  }
}

// AcrPull on the EXISTING registry, deployed at the registry's own scope.
module registryPull 'modules/registry-pull-role.bicep' = {
  name: '${namePrefix}-registry-pull'
  scope: resourceGroup(registrySubscriptionId, registryResourceGroupName)
  params: {
    registryName: registryName
    principalId: identity.outputs.principalId
  }
}

// ---------------------------------------------------------------------------
// Network: the NFS mount and every private endpoint need a custom VNet, and the
// infrastructure subnet's NSG must allow 445 and 2049 to storage (facts §3.2).
// ---------------------------------------------------------------------------
module infrastructureNsg 'br/public:avm/res/network/network-security-group:0.5.3' = {
  name: '${namePrefix}-infra-nsg'
  params: {
    name: '${namePrefix}-infra-nsg'
    location: location
    tags: tags
    securityRules: [
      {
        name: 'allow-nfs-to-private-endpoints'
        properties: {
          priority: 100
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: infrastructureSubnetPrefix
          sourcePortRange: '*'
          destinationAddressPrefix: privateEndpointSubnetPrefix
          destinationPortRange: '2049'
          description: 'NFS 4.1 to the Azure Files private endpoint'
        }
      }
      {
        name: 'allow-smb-to-private-endpoints'
        properties: {
          priority: 110
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: infrastructureSubnetPrefix
          sourcePortRange: '*'
          destinationAddressPrefix: privateEndpointSubnetPrefix
          destinationPortRange: '445'
          description: 'Container Apps requires 445 alongside 2049 for Azure Files mounts'
        }
      }
    ]
  }
}

module vnet 'br/public:avm/res/network/virtual-network:0.10.2' = {
  name: '${namePrefix}-vnet'
  params: {
    name: '${namePrefix}-vnet'
    location: location
    tags: tags
    addressPrefixes: [
      vnetAddressPrefix
    ]
    subnets: [
      {
        name: 'infrastructure'
        addressPrefix: infrastructureSubnetPrefix
        delegation: 'Microsoft.App/environments'
        networkSecurityGroupResourceId: infrastructureNsg.outputs.resourceId
      }
      {
        name: 'private-endpoints'
        addressPrefix: privateEndpointSubnetPrefix
        privateEndpointNetworkPolicies: 'Disabled'
      }
    ]
  }
}

module fileDnsZone 'br/public:avm/res/network/private-dns-zone:0.8.1' = {
  name: '${namePrefix}-dns-file'
  params: {
    name: 'privatelink.file.${environment().suffixes.storage}'
    tags: tags
    virtualNetworkLinks: [
      {
        virtualNetworkResourceId: vnet.outputs.resourceId
        registrationEnabled: false
      }
    ]
  }
}

module blobDnsZone 'br/public:avm/res/network/private-dns-zone:0.8.1' = {
  name: '${namePrefix}-dns-blob'
  params: {
    name: 'privatelink.blob.${environment().suffixes.storage}'
    tags: tags
    virtualNetworkLinks: [
      {
        virtualNetworkResourceId: vnet.outputs.resourceId
        registrationEnabled: false
      }
    ]
  }
}

module postgresDnsZone 'br/public:avm/res/network/private-dns-zone:0.8.1' = {
  name: '${namePrefix}-dns-postgres'
  params: {
    name: 'privatelink.postgres.database.azure.com'
    tags: tags
    virtualNetworkLinks: [
      {
        virtualNetworkResourceId: vnet.outputs.resourceId
        registrationEnabled: false
      }
    ]
  }
}

module keyVaultDnsZone 'br/public:avm/res/network/private-dns-zone:0.8.1' = {
  name: '${namePrefix}-dns-vault'
  params: {
    name: 'privatelink.vaultcore.azure.net'
    tags: tags
    virtualNetworkLinks: [
      {
        virtualNetworkResourceId: vnet.outputs.resourceId
        registrationEnabled: false
      }
    ]
  }
}

// ---------------------------------------------------------------------------
// Storage: ONE NFS 4.1 share for data/, data/blobs and payloads/ (Premium
// FileStorage, private endpoint, root squash off, encryption in transit off
// because Container Apps cannot mount an encrypted NFS share; facts §3.2).
// ---------------------------------------------------------------------------
module fileStorage 'br/public:avm/res/storage/storage-account:0.33.0' = {
  name: '${namePrefix}-file-storage'
  params: {
    name: fileStorageAccountName
    location: location
    tags: tags
    kind: 'FileStorage'
    skuName: 'Premium_LRS'
    supportsHttpsTrafficOnly: false
    allowSharedKeyAccess: false
    publicNetworkAccess: 'Disabled'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Deny'
    }
    fileServices: {
      shares: [
        {
          name: nfsShareName
          enabledProtocols: 'NFS'
          rootSquash: 'NoRootSquash'
          shareQuota: nfsShareQuotaGiB
        }
      ]
    }
    privateEndpoints: [
      {
        service: 'file'
        subnetResourceId: vnet.outputs.subnetResourceIds[1]
        privateDnsZoneGroup: {
          privateDnsZoneGroupConfigs: [
            {
              privateDnsZoneResourceId: fileDnsZone.outputs.resourceId
            }
          ]
        }
      }
    ]
  }
}

// Blob payload storage for the azure_blob@managed_identity cases. The identity
// holds Storage Blob Data Contributor on this ONE container only (plan §3.2).
module blobStorage 'br/public:avm/res/storage/storage-account:0.33.0' = {
  name: '${namePrefix}-blob-storage'
  params: {
    name: blobStorageAccountName
    location: location
    tags: tags
    kind: 'StorageV2'
    skuName: 'Standard_LRS'
    allowSharedKeyAccess: false
    publicNetworkAccess: 'Disabled'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Deny'
    }
    blobServices: {
      containers: [
        {
          name: payloadContainerName
          publicAccess: 'None'
          roleAssignments: [
            {
              principalId: identity.outputs.principalId
              roleDefinitionIdOrName: 'Storage Blob Data Contributor'
              principalType: 'ServicePrincipal'
            }
          ]
        }
      ]
    }
    privateEndpoints: [
      {
        service: 'blob'
        subnetResourceId: vnet.outputs.subnetResourceIds[1]
        privateDnsZoneGroup: {
          privateDnsZoneGroupConfigs: [
            {
              privateDnsZoneResourceId: blobDnsZone.outputs.resourceId
            }
          ]
        }
      }
    ]
  }
}

// ---------------------------------------------------------------------------
// Key Vault: RBAC, secrets resolved by the identity (Key Vault Secrets User).
// ---------------------------------------------------------------------------
module keyVault 'br/public:avm/res/key-vault/vault:0.14.0' = {
  name: '${namePrefix}-key-vault'
  params: {
    name: keyVaultName
    location: location
    tags: tags
    sku: 'standard'
    enableRbacAuthorization: true
    enableSoftDelete: true
    enablePurgeProtection: keyVaultPurgeProtection
    publicNetworkAccess: empty(keyVaultAllowedIpRules) ? 'Disabled' : 'Enabled'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Deny'
      ipRules: map(keyVaultAllowedIpRules, ip => { value: ip })
    }
    roleAssignments: [
      {
        principalId: identity.outputs.principalId
        roleDefinitionIdOrName: 'Key Vault Secrets User'
        principalType: 'ServicePrincipal'
      }
    ]
    privateEndpoints: [
      {
        service: 'vault'
        subnetResourceId: vnet.outputs.subnetResourceIds[1]
        privateDnsZoneGroup: {
          privateDnsZoneGroupConfigs: [
            {
              privateDnsZoneResourceId: keyVaultDnsZone.outputs.resourceId
            }
          ]
        }
      }
    ]
  }
}

// ---------------------------------------------------------------------------
// PostgreSQL Flexible Server: BOTH databases, password authentication (Entra
// excluded on the record, plan D4), private endpoint plus optional operator
// firewall rule.
// ---------------------------------------------------------------------------
module postgres 'br/public:avm/res/db-for-postgre-sql/flexible-server:0.16.0' = {
  name: '${namePrefix}-postgres'
  params: {
    name: postgresServerName
    location: location
    tags: tags
    version: postgresVersion
    tier: postgresTier
    skuName: postgresSkuName
    availabilityZone: -1
    highAvailability: postgresHighAvailability
    geoRedundantBackup: postgresGeoRedundantBackup
    backupRetentionDays: 7
    storageSizeGB: 32
    administratorLogin: postgresAdministratorLogin
    administratorLoginPassword: postgresAdministratorPassword
    authConfig: {
      activeDirectoryAuth: 'Disabled'
      passwordAuth: 'Enabled'
    }
    databases: [
      {
        name: 'elspeth_sessions'
      }
      {
        name: 'elspeth_landscape'
      }
    ]
    publicNetworkAccess: postgresPublicNetworkAccess
    firewallRules: postgresFirewallRules
    serverThreatProtection: postgresThreatProtection
    enableAdvancedThreatProtection: postgresThreatProtection == 'Enabled'
    privateEndpoints: [
      {
        service: 'postgresqlServer'
        subnetResourceId: vnet.outputs.subnetResourceIds[1]
        privateDnsZoneGroup: {
          privateDnsZoneGroupConfigs: [
            {
              privateDnsZoneResourceId: postgresDnsZone.outputs.resourceId
            }
          ]
        }
      }
    ]
  }
}

// ---------------------------------------------------------------------------
// Container Apps environment: VNet-injected, Log Analytics destination, the NFS
// storage definition every replica and Job mounts.
// ---------------------------------------------------------------------------
module managedEnvironment 'br/public:avm/res/app/managed-environment:0.16.0' = {
  name: '${namePrefix}-environment'
  params: {
    name: '${namePrefix}-env'
    location: location
    tags: tags
    infrastructureSubnetResourceId: vnet.outputs.subnetResourceIds[0]
    internal: false
    publicNetworkAccess: 'Enabled'
    zoneRedundant: zoneRedundant
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsWorkspaceResourceId: logAnalytics.outputs.resourceId
    }
    storages: [
      {
        name: nfsStorageName
        kind: 'NFS'
        storageAccountName: fileStorage.outputs.name
        accessMode: 'ReadWrite'
      }
    ]
  }
}

// ---------------------------------------------------------------------------
// Outputs consumed by workload.bicep and the acceptance driver
// ---------------------------------------------------------------------------
output environmentResourceId string = managedEnvironment.outputs.resourceId
output environmentName string = managedEnvironment.outputs.name
output environmentDefaultDomain string = managedEnvironment.outputs.defaultDomain
output identityResourceId string = identity.outputs.resourceId
output identityPrincipalId string = identity.outputs.principalId
output identityClientId string = identity.outputs.clientId
output keyVaultName string = keyVault.outputs.name
output keyVaultUri string = keyVault.outputs.uri
output postgresServerResourceId string = postgres.outputs.resourceId
// AVM types fqdn as nullable (tryGet over fullyQualifiedDomainName); a created
// Flexible Server always carries one, so assert it rather than widen the output.
output postgresFqdn string = postgres.outputs.fqdn!
output logAnalyticsWorkspaceResourceId string = logAnalytics.outputs.resourceId
output logAnalyticsCustomerId string = logAnalytics.outputs.logAnalyticsWorkspaceId
output fileStorageAccountName string = fileStorage.outputs.name
output nfsShareName string = nfsShareName
output nfsStorageName string = nfsStorageName
output blobStorageAccountName string = blobStorage.outputs.name
output payloadContainerName string = payloadContainerName
output registryPullRoleAssignmentId string = registryPull.outputs.roleAssignmentId
