// Grants AcrPull on an EXISTING container registry to the workload identity.
//
// The registry is the one build-push.yaml publishes to (secrets.ACR_REGISTRY);
// it lives outside the disposable resource group, so this module is deployed
// at the registry's own resource-group scope from environment.bicep.
targetScope = 'resourceGroup'

@description('Name of the existing container registry that holds the ELSPETH image.')
param registryName string

@description('Principal id of the user-assigned identity that pulls the image.')
param principalId string

// Built-in AcrPull role definition id.
var acrPullRoleDefinitionId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '7f951dda-4ed3-4680-a7ca-43fe172d538d'
)

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, principalId, 'AcrPull')
  scope: registry
  properties: {
    roleDefinitionId: acrPullRoleDefinitionId
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}

output registryResourceId string = registry.id
output roleAssignmentId string = acrPull.id
