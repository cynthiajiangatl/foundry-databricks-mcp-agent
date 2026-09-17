@description('Cosmos DB account name.')
param name string

@description('Azure region.')
param location string

@description('AppOnboard session tags.')
param tags object

@description('SQL database holding conversation history.')
param databaseName string = 'agent'

@description('Container holding one document per conversation.')
param containerName string = 'conversations'

@description('Principal ID of the app identity granted data-plane access.')
param appPrincipalId string

@description('Seconds before an idle conversation is removed by the Cosmos TTL.')
param conversationTtlSeconds int = 2592000

resource account 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' = {
  name: name
  location: location
  tags: tags
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    // Chat traffic is low and bursty, so pay per request instead of reserving RU/s.
    capabilities: [
      {
        name: 'EnableServerless'
      }
    ]
    // Entra ID only: account keys cannot be used to reach the data plane.
    disableLocalAuth: true
    minimalTlsVersion: 'Tls12'
    consistencyPolicy: {
      defaultConsistencyLevel: 'Session'
    }
    locations: [
      {
        locationName: location
        failoverPriority: 0
        isZoneRedundant: false
      }
    ]
  }
}

resource database 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-11-15' = {
  parent: account
  name: databaseName
  properties: {
    resource: {
      id: databaseName
    }
  }
}

resource container 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = {
  parent: database
  name: containerName
  properties: {
    resource: {
      id: containerName
      // Every read and write is a point operation keyed by the signed-in owner, so the
      // owner is both the tenancy boundary and the partition key.
      partitionKey: {
        paths: [
          '/ownerId'
        ]
        kind: 'Hash'
      }
      defaultTtl: conversationTtlSeconds
      indexingPolicy: {
        indexingMode: 'consistent'
        automatic: true
        // The serialized session is never queried, so leaving it unindexed keeps write cost flat.
        includedPaths: [
          {
            path: '/ownerId/?'
          }
          {
            path: '/updatedAt/?'
          }
        ]
        excludedPaths: [
          {
            path: '/*'
          }
        ]
      }
    }
  }
}

// Cosmos data-plane access uses its own RBAC system rather than Azure RBAC.
var dataContributorRoleId = '00000000-0000-0000-0000-000000000002'

resource dataContributor 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = {
  parent: account
  name: guid(account.id, appPrincipalId, dataContributorRoleId)
  properties: {
    roleDefinitionId: '${account.id}/sqlRoleDefinitions/${dataContributorRoleId}'
    principalId: appPrincipalId
    scope: account.id
  }
}

output accountName string = account.name
output endpoint string = account.properties.documentEndpoint
output databaseName string = database.name
output containerName string = container.name
