@description('Key Vault name.')
param name string

@description('Azure region.')
param location string

@description('AppOnboard session tags.')
param tags object

@description('EasyAuth client secret. Left empty in Phase 1; deploy seeds the value in Phase 2 (either via this parameter or via az keyvault secret set).')
@secure()
param easyAuthClientSecret string = ''

resource kv 'Microsoft.KeyVault/vaults@2026-05-15' = {
  name: name
  location: location
  tags: tags
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
  }
}

resource easyAuthSecret 'Microsoft.KeyVault/vaults/secrets@2026-05-15' = if (!empty(easyAuthClientSecret)) {
  parent: kv
  name: 'easyauth-client-secret'
  properties: {
    value: easyAuthClientSecret
  }
}

output id string = kv.id
output name string = kv.name
output vaultUri string = kv.properties.vaultUri
