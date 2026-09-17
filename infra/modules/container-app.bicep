@description('Container App name.')
param name string

@description('Azure region.')
param location string

@description('AppOnboard session tags.')
param tags object

@description('ARM resource ID of the Container Apps managed environment.')
param environmentId string

@description('Application image. The MCR placeholder keeps Phase 1 free of ACR, Key Vault and EasyAuth dependencies.')
param containerImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

@description('Port the FastAPI/uvicorn process listens on.')
param appPort int = 8000

@description('Anonymous health probe path served by the app.')
param healthProbePath string = '/healthz'

@description('ARM resource ID of the user-assigned managed identity.')
param userAssignedIdentityId string

@description('Client ID of the user-assigned managed identity.')
param userAssignedIdentityClientId string

@description('ACR login server used for image pulls in Phase 2.')
param containerRegistryLoginServer string

@description('Key Vault name holding the EasyAuth client secret.')
param keyVaultName string

@description('Application Insights connection string (not a secret).')
param appInsightsConnectionString string

@description('Entra tenant ID used by EasyAuth and the OBO exchange.')
param authTenantId string

@description('Entra app registration (client) ID used by EasyAuth and the OBO exchange.')
param authClientId string

param foundryProjectEndpoint string
param foundryModel string
param databricksHost string
param databricksUcCatalog string
param databricksUcSchema string
param databricksGenieSpaceId string

@description('Cosmos DB account endpoint holding conversation history.')
param cosmosEndpoint string

@description('Cosmos DB database holding conversation history.')
param cosmosDatabase string

@description('Cosmos DB container holding conversation history.')
param cosmosContainer string

@allowed([
  'obo'
  'app'
])
param databricksIdentityMode string = 'obo'

var placeholderImage = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'
var isPlaceholder = containerImage == placeholderImage
var effectivePort = isPlaceholder ? 80 : appPort
var easyAuthSecretName = 'easyauth-client-secret'

resource containerApp 'Microsoft.App/containerApps@2026-01-01' = {
  name: name
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${userAssignedIdentityId}': {}
    }
  }
  properties: {
    environmentId: environmentId
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: effectivePort
        transport: 'auto'
        allowInsecure: false
        // Conversation history is durable in Cosmos DB, so affinity is not required for
        // correctness; it keeps a user's turns on one replica and avoids write conflicts.
        stickySessions: {
          affinity: 'sticky'
        }
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      registries: isPlaceholder ? [] : [
        {
          server: containerRegistryLoginServer
          identity: userAssignedIdentityId
        }
      ]
      secrets: isPlaceholder ? [] : [
        {
          name: easyAuthSecretName
          #disable-next-line no-hardcoded-env-urls
          keyVaultUrl: 'https://${keyVaultName}.vault.azure.net/secrets/${easyAuthSecretName}'
          identity: userAssignedIdentityId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'app'
          image: containerImage
          resources: {
            cpu: json('1.0')
            memory: '2.0Gi'
          }
          env: [
            {
              name: 'FOUNDRY_PROJECT_ENDPOINT'
              value: foundryProjectEndpoint
            }
            {
              name: 'FOUNDRY_MODEL'
              value: foundryModel
            }
            {
              name: 'DATABRICKS_HOST'
              value: databricksHost
            }
            {
              name: 'DATABRICKS_UC_CATALOG'
              value: databricksUcCatalog
            }
            {
              name: 'DATABRICKS_UC_SCHEMA'
              value: databricksUcSchema
            }
            {
              name: 'DATABRICKS_GENIE_SPACE_ID'
              value: databricksGenieSpaceId
            }
            {
              name: 'WEBAPP_DATABRICKS_IDENTITY'
              value: databricksIdentityMode
            }
            {
              name: 'WEBAPP_TENANT_ID'
              value: authTenantId
            }
            {
              name: 'WEBAPP_CLIENT_ID'
              value: authClientId
            }
            {
              name: 'WEBAPP_USE_MANAGED_IDENTITY'
              value: '1'
            }
            {
              name: 'WEBAPP_MANAGED_IDENTITY_CLIENT_ID'
              value: userAssignedIdentityClientId
            }
            {
              name: 'AZURE_CLIENT_ID'
              value: userAssignedIdentityClientId
            }
            {
              name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
              value: appInsightsConnectionString
            }
            {
              name: 'COSMOS_ENDPOINT'
              value: cosmosEndpoint
            }
            {
              name: 'COSMOS_DATABASE'
              value: cosmosDatabase
            }
            {
              name: 'COSMOS_CONTAINER'
              value: cosmosContainer
            }
          ]
          probes: isPlaceholder ? [] : [
            {
              type: 'Liveness'
              httpGet: {
                path: healthProbePath
                port: appPort
                scheme: 'HTTP'
              }
              initialDelaySeconds: 20
              periodSeconds: 30
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: {
                path: healthProbePath
                port: appPort
                scheme: 'HTTP'
              }
              initialDelaySeconds: 10
              periodSeconds: 10
              failureThreshold: 6
            }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 3
        rules: [
          {
            name: 'http-scale'
            http: {
              metadata: {
                concurrentRequests: '20'
              }
            }
          }
        ]
      }
    }
  }
}

// Phase 2 only: EasyAuth needs the Key Vault-backed secret, which does not exist in Phase 1.
resource authConfig 'Microsoft.App/containerApps/authConfigs@2026-01-01' = if (!isPlaceholder) {
  parent: containerApp
  name: 'current'
  properties: {
    platform: {
      enabled: true
    }
    globalValidation: {
      unauthenticatedClientAction: 'RedirectToLoginPage'
      redirectToProvider: 'azureactivedirectory'
      excludedPaths: [
        healthProbePath
      ]
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          #disable-next-line no-hardcoded-env-urls
          openIdIssuer: 'https://login.microsoftonline.com/${authTenantId}/v2.0'
          clientId: authClientId
          clientSecretSettingName: easyAuthSecretName
        }
        validation: {
          allowedAudiences: [
            'api://${authClientId}'
            authClientId
          ]
        }
        login: {
          // access_as_user is required so the injected token is audienced to this app for OBO.
          loginParameters: [
            'scope=openid profile email offline_access api://${authClientId}/access_as_user'
          ]
        }
      }
    }
    login: {
      tokenStore: {
        enabled: true
      }
    }
  }
}

output name string = containerApp.name
output id string = containerApp.id
output fqdn string = containerApp.properties.configuration.ingress.fqdn
output uri string = 'https://${containerApp.properties.configuration.ingress.fqdn}'
