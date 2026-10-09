param storageAccountName string
param appInsightsName string
param identityPrincipalId string

@description('Optional user object ID granted Blob/Queue/Table data access for local development and portal access.')
param userPrincipalId string = ''

var roles = {
  storageBlobDataOwner: 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
  storageBlobDataContributor: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
  storageQueueDataContributor: '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
  storageTableDataContributor: '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
  monitoringMetricsPublisher: '3913510d-42f4-4e42-8a64-420c390055eb'
}
var identityStorageRoles = [
  roles.storageBlobDataOwner
  roles.storageQueueDataContributor
  roles.storageTableDataContributor
]
var userStorageRoles = empty(userPrincipalId)
  ? []
  : [
      roles.storageBlobDataContributor
      roles.storageQueueDataContributor
      roles.storageTableDataContributor
    ]

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: appInsightsName
}

resource identityStorage 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for roleId in identityStorageRoles: {
    name: guid(storage.id, identityPrincipalId, roleId)
    scope: storage
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleId)
      principalId: identityPrincipalId
      principalType: 'ServicePrincipal'
    }
  }
]

resource identityMetrics 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(appInsights.id, identityPrincipalId, roles.monitoringMetricsPublisher)
  scope: appInsights
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.monitoringMetricsPublisher)
    principalId: identityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource userStorage 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for roleId in userStorageRoles: {
    name: guid(storage.id, userPrincipalId, roleId)
    scope: storage
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleId)
      principalId: userPrincipalId
      principalType: 'User'
    }
  }
]
