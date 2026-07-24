@description('Name of the ELSPETH Container App workload.')
param containerAppName string = 'elspeth-web'

@description('Azure region for the Container App.')
param location string = resourceGroup().location

@description('Resource ID of an existing custom-VNet Container Apps environment.')
param containerAppsEnvironmentId string

@description('Immutable ELSPETH image reference built with webui, llm, azure, and postgres extras.')
param image string

@description('Resource ID of the existing user-assigned identity used for image pull and Key Vault access.')
param userAssignedIdentityResourceId string

@description('Name of the existing NFS Azure Files environment storage definition.')
param nfsStorageName string

@description('Relative, operator-prepared storage directory mounted into the workload.')
param storageSubPath string

@description('Key Vault secret URL for the session PostgreSQL database URL.')
param sessionDatabaseSecretUrl string

@description('Key Vault secret URL for the Landscape PostgreSQL database URL.')
param landscapeDatabaseSecretUrl string

@description('Key Vault secret URL for the ELSPETH web secret key.')
param webSecretKeySecretUrl string

@description('Key Vault secret URL for the shareable-link signing key.')
param shareableLinkSigningKeySecretUrl string

@description('Key Vault secret URL for the fingerprint key.')
param fingerprintKeySecretUrl string

@description('Maximum turns the composer may spend composing a pipeline.')
param composerMaxCompositionTurns int = 15

@description('Maximum turns the composer may spend discovering capabilities.')
param composerMaxDiscoveryTurns int = 10

@description('Composer request timeout in seconds.')
param composerTimeoutSeconds int = 85

@description('Composer request rate limit per minute.')
param composerRateLimitPerMinute int = 10

var registryServer = split(image, '/')[0]

// Before deploying, an operator must prepare storageSubPath/data/blobs and
// storageSubPath/payloads from a trusted NFS administration host. Each path
// must be owned by UID/GID 1000 and have mode 0700. The workload deliberately
// has no privilege to repair ownership or permissions.
resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: containerAppName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${userAssignedIdentityResourceId}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerAppsEnvironmentId
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        allowInsecure: false
        targetPort: 8451
        transport: 'auto'
      }
      registries: [
        {
          server: registryServer
          identity: userAssignedIdentityResourceId
        }
      ]
      secrets: [
        {
          name: 'session-database-url'
          keyVaultUrl: sessionDatabaseSecretUrl
          identity: userAssignedIdentityResourceId
        }
        {
          name: 'landscape-database-url'
          keyVaultUrl: landscapeDatabaseSecretUrl
          identity: userAssignedIdentityResourceId
        }
        {
          name: 'web-secret-key'
          keyVaultUrl: webSecretKeySecretUrl
          identity: userAssignedIdentityResourceId
        }
        {
          name: 'shareable-link-signing-key'
          keyVaultUrl: shareableLinkSigningKeySecretUrl
          identity: userAssignedIdentityResourceId
        }
        {
          name: 'fingerprint-key'
          keyVaultUrl: fingerprintKeySecretUrl
          identity: userAssignedIdentityResourceId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'elspeth-web'
          image: image
          command: [
            'elspeth'
            'web'
            '--host'
            '0.0.0.0'
            '--port'
            '8451'
          ]
          env: [
            {
              name: 'ELSPETH_WEB__DEPLOYMENT_TARGET'
              value: 'azure-container-apps'
            }
            {
              name: 'ELSPETH_WEB__DEPLOYMENT_STATE_MODE'
              value: 'external-postgresql'
            }
            {
              name: 'ELSPETH_WEB__SESSION_DB_URL'
              secretRef: 'session-database-url'
            }
            {
              name: 'ELSPETH_WEB__LANDSCAPE_URL'
              secretRef: 'landscape-database-url'
            }
            {
              name: 'ELSPETH_WEB__SECRET_KEY'
              secretRef: 'web-secret-key'
            }
            {
              name: 'ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY'
              secretRef: 'shareable-link-signing-key'
            }
            {
              name: 'ELSPETH_FINGERPRINT_KEY'
              secretRef: 'fingerprint-key'
            }
            {
              name: 'ELSPETH_WEB__DATA_DIR'
              value: '/mnt/elspeth/data'
            }
            {
              name: 'ELSPETH_WEB__PAYLOAD_STORE_PATH'
              value: '/mnt/elspeth/payloads'
            }
            {
              name: 'ELSPETH_WEB__COMPOSER_MAX_COMPOSITION_TURNS'
              value: string(composerMaxCompositionTurns)
            }
            {
              name: 'ELSPETH_WEB__COMPOSER_MAX_DISCOVERY_TURNS'
              value: string(composerMaxDiscoveryTurns)
            }
            {
              name: 'ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS'
              value: string(composerTimeoutSeconds)
            }
            {
              name: 'ELSPETH_WEB__COMPOSER_RATE_LIMIT_PER_MINUTE'
              value: string(composerRateLimitPerMinute)
            }
          ]
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          volumeMounts: [
            {
              volumeName: 'elspeth-state'
              mountPath: '/mnt/elspeth'
              subPath: storageSubPath
            }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/api/health'
                port: 8451
                scheme: 'HTTP'
              }
              initialDelaySeconds: 10
              periodSeconds: 30
              timeoutSeconds: 5
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/api/ready'
                port: 8451
                scheme: 'HTTP'
              }
              initialDelaySeconds: 5
              periodSeconds: 10
              timeoutSeconds: 5
              failureThreshold: 3
            }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
      volumes: [
        {
          name: 'elspeth-state'
          storageType: 'NfsAzureFile'
          storageName: nfsStorageName
        }
      ]
    }
  }
}
