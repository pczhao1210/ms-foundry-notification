targetScope = 'subscription'

param identityPrincipalId string

@description('Optional existing Reader assignment GUID; it must already belong to this principal at this subscription scope.')
param existingAssignmentName string = ''

var readerRoleId = 'acdd72a7-3385-48ef-bd42-f606fba81ae7'

resource subscriptionReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: empty(existingAssignmentName)
    ? guid(subscription().id, identityPrincipalId, readerRoleId)
    : existingAssignmentName
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', readerRoleId)
    principalId: identityPrincipalId
    principalType: 'ServicePrincipal'
  }
}