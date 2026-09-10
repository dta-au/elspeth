// ELSPETH on Azure Container Apps — workload (resource-group scope).
//
// One container app plus the manual Jobs, all on the same digest, the same
// NFS mount. Schema init has a separate identity; the provisioner has none.
// Every secret is a versioned
// Key Vault reference; nothing here carries a secret value.
targetScope = 'resourceGroup'

@description('Resource id of the Container Apps environment from environment.bicep.')
param environmentResourceId string

@description('Resource id of the user-assigned identity from environment.bicep.')
param identityResourceId string

@description('Client id of the runtime user-assigned identity; selects it in DefaultAzureCredential.')
@minLength(36)
@maxLength(36)
param identityClientId string

@description('Dedicated schema-init identity; only this identity can read the schema-owner vault.')
param schemaOwnerIdentityResourceId string

@description('Name of the NFS storage definition on the environment.')
param nfsStorageName string = 'elspeth'

@description('False creates/updates only the manual Jobs. Cold install runs these Jobs successfully before deploying the app. Always use Incremental deployment mode.')
param deployWebApp bool = true

@description('Create the disposable acceptance Blob managed-identity proof Job.')
param verifyBlobManagedIdentity bool = false
param blobAccountUrl string = ''
param blobContainerName string = ''

@description('Full source commit SHA of the digest-pinned candidate; binds membership and telemetry to the published release.')
@minLength(40)
@maxLength(40)
param candidateSourceSha string

@description('Container app name.')
param containerAppName string = 'elspeth-web'

@description('ELSPETH image reference by DIGEST (<registry>/elspeth@sha256:...). Tags are refused by the runbook, not by this template.')
@minLength(80)
param image string

@description('Digest-pinned root image for the provision-storage Job. The runtime image runs as UID 1654 and Container Apps offers no runAsUser (facts §0 C6).')
@minLength(72)
param provisionStorageImage string

@description('Revision suffix; the runbooks use the candidate short sha.')
@minLength(1)
@maxLength(64)
param revisionSuffix string

@description('Single for production; Multiple only for the acceptance probes.')
@allowed([
  'Single'
  'Multiple'
])
param activeRevisionsMode string = 'Single'

@description('Session affinity. The platform supports sticky only in single revision mode (facts §2.3), so the acceptance parameter set uses none.')
@allowed([
  'none'
  'sticky'
])
param stickySessionsAffinity string = 'sticky'

@minValue(0)
param minReplicas int = 2

@minValue(1)
param maxReplicas int = 4

@description('Seconds the platform waits after SIGTERM before SIGKILL; covers the drain sequence (facts §2.5).')
@minValue(0)
@maxValue(600)
param terminationGracePeriodSeconds int = 60

@description('REQUIRED, NO DEFAULT. The composer transport idle ceiling is the minimum idle timeout of every hop in front of the process. The Container Apps ingress request timeout is a fixed 240 seconds (facts §2.2); a Front Door in front lowers it further.')
@minValue(1)
@maxValue(240)
param composerTransportIdleCeilingSeconds int

@description('Label distinguishing the runtime role a revision runs as; empty in production, a or b in the acceptance (two revisions, two roles).')
@allowed(['', 'a', 'b'])
param runtimeRoleLabel string = ''

@sealed()
type databaseSecretUrls = {
  sessionDbUrl: string
  landscapeUrl: string
}

@sealed()
type acceptanceSecretUrls = {
  a: databaseSecretUrls
  b: databaseSecretUrls
}

@description('Both acceptance roles, retained identically on every app deployment. Required when runtimeRoleLabel is a or b.')
param acceptanceRuntimeSecretUrls acceptanceSecretUrls?

@description('CPU for the web container (Consumption pairs: 0.5/1Gi, 1.0/2Gi, 2.0/4Gi).')
param webCpu string = '1.0'

@description('Memory for the web container.')
param webMemory string = '2Gi'

@description('Extra environment entries ({name, value}) appended to the web container.')
param extraEnvironment array = []

@description('Tags applied to every resource.')
param tags object = {}

// Versioned Key Vault secret URLs (https://<vault>.vault.azure.net/secrets/<name>/<version>).
param sessionDbUrlRuntimeSecretUrl string
param landscapeUrlRuntimeSecretUrl string
param sessionDbUrlSchemaOwnerSecretUrl string
param landscapeUrlSchemaOwnerSecretUrl string
param secretKeySecretUrl string
param shareableLinkSigningKeySecretUrl string
param fingerprintKeySecretUrl string
param operatorMetricsBearerTokenSecretUrl string

@description('Optional composer endpoint API key secret URL; empty leaves the composer endpoint unset.')
param composerEndpointApiKeySecretUrl string = ''

// ---------------------------------------------------------------------------
// Derived values
// ---------------------------------------------------------------------------
var registryServer = split(image, '/')[0]
var mountPath = '/mnt/elspeth'
var webPort = 8451
var jobSuffix = empty(runtimeRoleLabel) ? '' : '-${runtimeRoleLabel}'
var composerSecret = empty(composerEndpointApiKeySecretUrl) ? [] : [
  {
    name: 'composer-endpoint-api-key'
    keyVaultUrl: composerEndpointApiKeySecretUrl
    identity: identityResourceId
  }
]
var composerEnv = empty(composerEndpointApiKeySecretUrl) ? [] : [
  {
    name: 'ELSPETH_WEB__COMPOSER_ENDPOINT_API_KEY'
    secretRef: 'composer-endpoint-api-key'
  }
]

var applicationSecrets = [
  {
    name: 'secret-key'
    keyVaultUrl: secretKeySecretUrl
    identity: identityResourceId
  }
  {
    name: 'shareable-link-signing-key'
    keyVaultUrl: shareableLinkSigningKeySecretUrl
    identity: identityResourceId
  }
  {
    name: 'fingerprint-key'
    keyVaultUrl: fingerprintKeySecretUrl
    identity: identityResourceId
  }
  {
    name: 'operator-metrics-bearer-token'
    keyVaultUrl: operatorMetricsBearerTokenSecretUrl
    identity: identityResourceId
  }
]

var productionDatabaseSecrets = [
  {
    name: 'session-db-url'
    keyVaultUrl: sessionDbUrlRuntimeSecretUrl
    identity: identityResourceId
  }
  {
    name: 'landscape-url'
    keyVaultUrl: landscapeUrlRuntimeSecretUrl
    identity: identityResourceId
  }
]

var acceptanceDatabaseSecrets = acceptanceRuntimeSecretUrls == null ? [] : [
  { name: 'session-db-url-a', keyVaultUrl: acceptanceRuntimeSecretUrls!.a.sessionDbUrl, identity: identityResourceId }
  { name: 'landscape-url-a', keyVaultUrl: acceptanceRuntimeSecretUrls!.a.landscapeUrl, identity: identityResourceId }
  { name: 'session-db-url-b', keyVaultUrl: acceptanceRuntimeSecretUrls!.b.sessionDbUrl, identity: identityResourceId }
  { name: 'landscape-url-b', keyVaultUrl: acceptanceRuntimeSecretUrls!.b.landscapeUrl, identity: identityResourceId }
]
var runtimeSecrets = concat(applicationSecrets, productionDatabaseSecrets, acceptanceDatabaseSecrets, composerSecret)

var schemaOwnerSecrets = concat(map(applicationSecrets, secret => {
  name: secret.name
  keyVaultUrl: secret.keyVaultUrl
  identity: schemaOwnerIdentityResourceId
}), [
  {
    name: 'session-db-url'
    keyVaultUrl: sessionDbUrlSchemaOwnerSecretUrl
    identity: schemaOwnerIdentityResourceId
  }
  {
    name: 'landscape-url'
    keyVaultUrl: landscapeUrlSchemaOwnerSecretUrl
    identity: schemaOwnerIdentityResourceId
  }
])

// The provider-neutral external-PostgreSQL contract: azure-container-apps
// resolves to external-postgresql and refuses sqlite-single at config time.
var contractEnvironment = [
  {
    name: 'ELSPETH_WEB__OPERATOR_TELEMETRY_RELEASE'
    value: candidateSourceSha
  }
  {
    name: 'ELSPETH_ACCEPTANCE_CANDIDATE_SHA'
    value: candidateSourceSha
  }
  {
    name: 'ELSPETH_WEB__DEPLOYMENT_TARGET'
    value: 'azure-container-apps'
  }
  {
    name: 'ELSPETH_WEB__DEPLOYMENT_STATE_MODE'
    value: 'external-postgresql'
  }
  {
    name: 'ELSPETH_WEB__HOST'
    value: '0.0.0.0'
  }
  {
    name: 'ELSPETH_WEB__PORT'
    value: string(webPort)
  }
  {
    name: 'WEB_CONCURRENCY'
    value: '1'
  }
  {
    name: 'ELSPETH_WEB__LOG_JSON'
    value: 'true'
  }
  {
    name: 'ELSPETH_WEB__OPERATOR_TELEMETRY'
    value: 'prometheus'
  }
  {
    name: 'ELSPETH_WEB__DATA_DIR'
    value: '${mountPath}/data'
  }
  {
    name: 'ELSPETH_WEB__PAYLOAD_STORE_PATH'
    value: '${mountPath}/payloads'
  }
  {
    name: 'ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS'
    value: string(composerTransportIdleCeilingSeconds)
  }
]

var databaseSecretEnvironment = [
  {
    name: 'ELSPETH_WEB__SESSION_DB_URL'
    secretRef: 'session-db-url'
  }
  {
    name: 'ELSPETH_WEB__LANDSCAPE_URL'
    secretRef: 'landscape-url'
  }
]

var applicationSecretEnvironment = [
  {
    name: 'ELSPETH_WEB__SECRET_KEY'
    secretRef: 'secret-key'
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
    name: 'ELSPETH_WEB__OPERATOR_METRICS_BEARER_TOKEN'
    secretRef: 'operator-metrics-bearer-token'
  }
]

var runtimeSecretEnvironment = concat([
  { name: 'ELSPETH_WEB__SESSION_DB_URL', secretRef: 'session-db-url${jobSuffix}' }
  { name: 'ELSPETH_WEB__LANDSCAPE_URL', secretRef: 'landscape-url${jobSuffix}' }
], applicationSecretEnvironment)
var runtimeIdentityEnvironment = [{ name: 'AZURE_CLIENT_ID', value: identityClientId }]
var webEnvironment = concat(contractEnvironment, runtimeSecretEnvironment, runtimeIdentityEnvironment, composerEnv, extraEnvironment)
var doctorEnvironment = concat(contractEnvironment, databaseSecretEnvironment, applicationSecretEnvironment)
var runtimeDoctorEnvironment = concat(contractEnvironment, runtimeSecretEnvironment, runtimeIdentityEnvironment)

var stateVolumes = [
  {
    name: 'elspeth-state'
    storageType: 'NfsAzureFile'
    storageName: nfsStorageName
    mountOptions: 'actimeo=30,nconnect=4,noresvport'
  }
]

var stateVolumeMounts = [
  {
    volumeName: 'elspeth-state'
    mountPath: mountPath
  }
]

var registries = [
  {
    server: registryServer
    identity: identityResourceId
  }
]

var managedIdentities = {
  userAssignedResourceIds: [
    identityResourceId
  ]
}

// Startup: 15 s x 10 = 150 s, the ECS startPeriod; failureThreshold is capped
// at 10 and initialDelaySeconds at 60 by the platform (facts §2.4).
var webProbes = [
  {
    type: 'Startup'
    httpGet: {
      path: '/api/health'
      port: webPort
      scheme: 'HTTP'
    }
    periodSeconds: 15
    timeoutSeconds: 5
    failureThreshold: 10
  }
  {
    type: 'Liveness'
    httpGet: {
      path: '/api/health'
      port: webPort
      scheme: 'HTTP'
    }
    periodSeconds: 30
    timeoutSeconds: 5
    failureThreshold: 3
  }
  {
    type: 'Readiness'
    httpGet: {
      path: '/api/ready'
      port: webPort
      scheme: 'HTTP'
    }
    periodSeconds: 10
    timeoutSeconds: 5
    failureThreshold: 3
  }
]

// ---------------------------------------------------------------------------
// The web app
// ---------------------------------------------------------------------------
module containerApp 'br/public:avm/res/app/container-app:0.23.0' = if (deployWebApp) {
  name: '${containerAppName}-app'
  params: {
    name: containerAppName
    location: resourceGroup().location
    tags: tags
    environmentResourceId: environmentResourceId
    workloadProfileName: 'Consumption'
    managedIdentities: managedIdentities
    registries: registries
    secrets: runtimeSecrets
    activeRevisionsMode: activeRevisionsMode
    revisionSuffix: revisionSuffix
    terminationGracePeriodSeconds: terminationGracePeriodSeconds
    ingressExternal: true
    ingressTargetPort: webPort
    ingressTransport: 'auto'
    ingressAllowInsecure: false
    stickySessionsAffinity: stickySessionsAffinity
    traffic: activeRevisionsMode == 'Multiple' ? [
      {
        latestRevision: true
        weight: 100
      }
    ] : null
    scaleSettings: {
      minReplicas: minReplicas
      maxReplicas: maxReplicas
    }
    volumes: stateVolumes
    containers: [
      {
        name: 'elspeth-web'
        image: image
        args: [
          'web'
          '--host'
          '0.0.0.0'
          '--port'
          string(webPort)
        ]
        env: webEnvironment
        resources: {
          cpu: json(webCpu)
          memory: webMemory
        }
        volumeMounts: stateVolumeMounts
        probes: webProbes
      }
    ]
  }
}

// ---------------------------------------------------------------------------
// Jobs (Manual, no retry): same mount, least-privilege identities.
// ---------------------------------------------------------------------------
module provisionStorageJob 'br/public:avm/res/app/job:0.7.2' = {
  name: 'provision-storage-job'
  params: {
    name: 'provision-storage'
    location: resourceGroup().location
    tags: tags
    environmentResourceId: environmentResourceId
    workloadProfileName: 'Consumption'
    triggerType: 'Manual'
    manualTriggerConfig: {
      parallelism: 1
      replicaCompletionCount: 1
    }
    replicaRetryLimit: 0
    replicaTimeout: 600
    volumes: stateVolumes
    containers: [
      {
        name: 'provision-storage'
        image: provisionStorageImage
        command: [
          '/bin/sh'
          '-c'
          'set -eu; mkdir -p ${mountPath}/data/blobs ${mountPath}/payloads; chown -R 1654:1654 ${mountPath}/data ${mountPath}/payloads; chmod 0700 ${mountPath}/data ${mountPath}/data/blobs ${mountPath}/payloads; ls -ln ${mountPath}'
        ]
        resources: {
          cpu: json('0.25')
          memory: '0.5Gi'
        }
        volumeMounts: stateVolumeMounts
      }
    ]
  }
}

module doctorSchemaInitJob 'br/public:avm/res/app/job:0.7.2' = {
  name: 'doctor-schema-init-job'
  params: {
    name: 'doctor-schema-init'
    location: resourceGroup().location
    tags: tags
    environmentResourceId: environmentResourceId
    workloadProfileName: 'Consumption'
    triggerType: 'Manual'
    manualTriggerConfig: {
      parallelism: 1
      replicaCompletionCount: 1
    }
    replicaRetryLimit: 0
    replicaTimeout: 1800
    managedIdentities: { userAssignedResourceIds: [schemaOwnerIdentityResourceId] }
    registries: [{ server: registryServer, identity: schemaOwnerIdentityResourceId }]
    secrets: schemaOwnerSecrets
    volumes: stateVolumes
    containers: [
      {
        name: 'doctor'
        image: image
        args: [
          'doctor'
          'deployment'
          '--init-schema'
          '--json'
        ]
        env: doctorEnvironment
        resources: {
          cpu: json('0.5')
          memory: '1Gi'
        }
        volumeMounts: stateVolumeMounts
      }
    ]
  }
}

module doctorRuntimeJob 'br/public:avm/res/app/job:0.7.2' = {
  name: 'doctor-runtime${jobSuffix}-job'
  params: {
    name: 'doctor-runtime${jobSuffix}'
    location: resourceGroup().location
    tags: tags
    environmentResourceId: environmentResourceId
    workloadProfileName: 'Consumption'
    triggerType: 'Manual'
    manualTriggerConfig: {
      parallelism: 1
      replicaCompletionCount: 1
    }
    replicaRetryLimit: 0
    replicaTimeout: 1800
    managedIdentities: managedIdentities
    registries: registries
    secrets: runtimeSecrets
    volumes: stateVolumes
    containers: [
      {
        name: 'doctor'
        image: image
        args: [
          'doctor'
          'deployment'
          '--json'
        ]
        env: runtimeDoctorEnvironment
        resources: {
          cpu: json('0.5')
          memory: '1Gi'
        }
        volumeMounts: stateVolumeMounts
      }
    ]
  }
}

module verifyBlobManagedIdentityJob 'br/public:avm/res/app/job:0.7.2' = if (verifyBlobManagedIdentity) {
  name: 'verify-blob-managed-identity-job'
  params: {
    name: 'verify-blob-managed-identity'
    location: resourceGroup().location
    tags: tags
    environmentResourceId: environmentResourceId
    workloadProfileName: 'Consumption'
    triggerType: 'Manual'
    manualTriggerConfig: {
      parallelism: 1
      replicaCompletionCount: 1
    }
    replicaRetryLimit: 0
    replicaTimeout: 600
    managedIdentities: managedIdentities
    registries: registries
    containers: [
      {
        name: 'verify-blob-managed-identity'
        image: image
        command: [
          'python'
          '-m'
          'elspeth.web.azure_blob_acceptance_job'
        ]
        env: [
          {
            name: 'ELSPETH_ACCEPTANCE_BLOB_ACCOUNT_URL'
            value: blobAccountUrl
          }
          {
            name: 'ELSPETH_ACCEPTANCE_BLOB_CONTAINER'
            value: blobContainerName
          }
          {
            name: 'AZURE_CLIENT_ID'
            value: identityClientId
          }
          {
            name: 'AZURE_TOKEN_CREDENTIALS'
            value: 'ManagedIdentityCredential'
          }
          {
            name: 'ELSPETH_ACCEPTANCE_CANDIDATE_SHA'
            value: candidateSourceSha
          }
        ]
        resources: {
          cpu: json('0.5')
          memory: '1Gi'
        }
      }
    ]
  }
}

output containerAppResourceId string = deployWebApp ? containerApp!.outputs.resourceId : ''
output containerAppFqdn string = deployWebApp ? containerApp!.outputs.fqdn : ''
output revisionName string = '${containerAppName}--${revisionSuffix}'
output doctorRuntimeJobName string = 'doctor-runtime${jobSuffix}'
