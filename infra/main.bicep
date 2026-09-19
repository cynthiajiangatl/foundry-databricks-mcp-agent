targetScope = 'subscription'

@minLength(1)
@maxLength(64)
@description('AppOnboard environment name (resource prefix).')
param environmentName string

@minLength(1)
@description('Azure region for all resources.')
param location string

@description('AppOnboard session ID.')
param sessionId string

@description('Identity that performed the deployment.')
param deployedBy string

@description('ISO 8601 creation timestamp for the created-at tag.')
param createdAt string

@description('Object ID of the deploying user, granted Key Vault Secrets Officer.')
param deployerObjectId string = ''

@description('Application image. Phase 1 deploys the MCR placeholder; Phase 2 passes the ACR image.')
param containerImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

@description('EasyAuth client secret. Empty in Phase 1; deploy supplies it in Phase 2 or seeds Key Vault directly.')
@secure()
param easyAuthClientSecret string = ''

@description('Entra tenant ID used by EasyAuth and the OBO exchange.')
param authTenantId string = '7ec2f66d-b467-4c02-a8c6-4c20748961c5'

@description('Existing Entra app registration (client) ID.')
param authClientId string = 'bdb0e2d4-92f0-4d1c-b421-47f9a4290023'

param foundryProjectEndpoint string = 'https://foundrycj1.services.ai.azure.com/api/projects/projectcj1'
param foundryModel string = 'gpt-5.6-sol'
param databricksHost string = 'https://adb-7405619192018422.2.azuredatabricks.net'
param databricksUcCatalog string = 'adbwscj1'
param databricksUcSchema string = 'dbdemos_aibi_customer_support'
param databricksGenieSpaceId string = '01f1808744811fc78f0b82f240444c19'

@description('Lakebase endpoint resource name. Empty disables the Lakebase tools.')
param databricksLakebaseEndpoint string = ''

@description('Lakebase Postgres hostname. Empty disables the Lakebase tools.')
param databricksLakebaseHost string = ''

// Names come verbatim from prepare-plan.json.naming.resources — do not derive.
param resourceGroupName string = 'rg-foundry-dev-19e5'
param containerAppName string = 'ca-foundry-dev-19e5'
param containerAppsEnvironmentName string = 'cae-foundry-dev-19e5'
param containerRegistryName string = 'crfoundrydev19e5'
param managedIdentityName string = 'id-foundry-dev-19e5'
param keyVaultName string = 'kv-foundry-dev-19e5'
param logAnalyticsWorkspaceName string = 'log-foundry-dev-19e5'
param appInsightsName string = 'appi-foundry-dev-19e5'
param cosmosAccountName string = 'cos-foundry-dev-19e5'

var tags = {
  'app-onboard-skill': 'true'
  'app-onboard-session-id': sessionId
  'created-at': createdAt
  environment: environmentName
  'deployed-by': deployedBy
}

resource rg 'Microsoft.Resources/resourceGroups@2023-07-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module managedIdentity './modules/managed-identity.bicep' = {
  name: 'managed-identity'
  scope: rg
  params: {
    name: managedIdentityName
    location: location
    tags: tags
  }
}

module logAnalytics './modules/log-analytics.bicep' = {
  name: 'log-analytics'
  scope: rg
  params: {
    name: logAnalyticsWorkspaceName
    location: location
    tags: tags
  }
}

module appInsights './modules/app-insights.bicep' = {
  name: 'app-insights'
  scope: rg
  params: {
    name: appInsightsName
    location: location
    tags: tags
    logAnalyticsWorkspaceId: logAnalytics.outputs.id
  }
}

module containerRegistry './modules/container-registry.bicep' = {
  name: 'container-registry'
  scope: rg
  params: {
    name: containerRegistryName
    location: location
    tags: tags
  }
}

module keyVault './modules/key-vault.bicep' = {
  name: 'key-vault'
  scope: rg
  params: {
    name: keyVaultName
    location: location
    tags: tags
    easyAuthClientSecret: easyAuthClientSecret
  }
}

module cosmos './modules/cosmos-db.bicep' = {
  name: 'cosmos-db'
  scope: rg
  params: {
    name: cosmosAccountName
    location: location
    tags: tags
    appPrincipalId: managedIdentity.outputs.principalId
  }
}

module containerAppsEnvironment './modules/container-apps-environment.bicep' = {
  name: 'container-apps-environment'
  scope: rg
  params: {
    name: containerAppsEnvironmentName
    location: location
    tags: tags
    logAnalyticsWorkspaceName: logAnalytics.outputs.name
  }
}

// Created in Phase 1 so AcrPull / KV Secrets User RBAC has propagated before Phase 2.
module roleAssignments './modules/role-assignments.bicep' = {
  name: 'role-assignments'
  scope: rg
  params: {
    containerRegistryName: containerRegistry.outputs.name
    keyVaultName: keyVault.outputs.name
    appPrincipalId: managedIdentity.outputs.principalId
    deployerObjectId: deployerObjectId
  }
}

module containerApp './modules/container-app.bicep' = {
  name: 'container-app'
  scope: rg
  params: {
    name: containerAppName
    location: location
    tags: tags
    environmentId: containerAppsEnvironment.outputs.id
    containerImage: containerImage
    appPort: 8000
    healthProbePath: '/healthz'
    userAssignedIdentityId: managedIdentity.outputs.id
    userAssignedIdentityClientId: managedIdentity.outputs.clientId
    containerRegistryLoginServer: containerRegistry.outputs.loginServer
    keyVaultName: keyVault.outputs.name
    appInsightsConnectionString: appInsights.outputs.connectionString
    authTenantId: authTenantId
    authClientId: authClientId
    foundryProjectEndpoint: foundryProjectEndpoint
    foundryModel: foundryModel
    databricksHost: databricksHost
    databricksUcCatalog: databricksUcCatalog
    databricksUcSchema: databricksUcSchema
    databricksGenieSpaceId: databricksGenieSpaceId
    databricksLakebaseEndpoint: databricksLakebaseEndpoint
    databricksLakebaseHost: databricksLakebaseHost
    databricksIdentityMode: 'obo'
    cosmosEndpoint: cosmos.outputs.endpoint
    cosmosDatabase: cosmos.outputs.databaseName
    cosmosContainer: cosmos.outputs.containerName
  }
  dependsOn: [
    roleAssignments
  ]
}

output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_LOCATION string = location
output AZURE_CONTAINER_REGISTRY_NAME string = containerRegistry.outputs.name
output AZURE_CONTAINER_REGISTRY_LOGIN_SERVER string = containerRegistry.outputs.loginServer
output AZURE_CONTAINER_APP_NAME string = containerApp.outputs.name
output AZURE_CONTAINER_APP_FQDN string = containerApp.outputs.fqdn
output AZURE_CONTAINER_APP_URI string = containerApp.outputs.uri
output AZURE_KEY_VAULT_NAME string = keyVault.outputs.name
output AZURE_COSMOS_ACCOUNT_NAME string = cosmos.outputs.accountName
output AZURE_COSMOS_ENDPOINT string = cosmos.outputs.endpoint
output AZURE_APP_INSIGHTS_NAME string = appInsights.outputs.name
output AZURE_LOG_ANALYTICS_WORKSPACE_NAME string = logAnalytics.outputs.name
output AZURE_MANAGED_IDENTITY_NAME string = managedIdentity.outputs.name
output AZURE_MANAGED_IDENTITY_CLIENT_ID string = managedIdentity.outputs.clientId
output AZURE_MANAGED_IDENTITY_PRINCIPAL_ID string = managedIdentity.outputs.principalId
output AZURE_MANAGED_IDENTITY_RESOURCE_ID string = managedIdentity.outputs.id
