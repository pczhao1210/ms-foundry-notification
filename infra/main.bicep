targetScope = 'subscription'

@minLength(1)
@maxLength(48)
@description('Environment name; used for the resource group name and the resource name token.')
param environmentName string

@minLength(1)
@description('Region for all resources; must support Flex Consumption.')
param location string

@description('Optional user object ID granted Blob/Table data access for local development.')
param principalId string = ''

@minValue(40)
@maxValue(1000)
param maximumInstanceCount int = 40

@allowed([512, 2048, 4096])
param instanceMemoryMB int = 2048

var tags = { 'azd-env-name': environmentName }
var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))
var readerRoleId = 'acdd72a7-3385-48ef-bd42-f606fba81ae7'

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-${environmentName}'
  location: location
  tags: tags
}

module identity 'modules/identity.bicep' = {
  scope: rg
  name: 'identity'
  params: {
    name: 'id-${resourceToken}'
    location: location
    tags: tags
  }
}

module monitoring 'modules/monitoring.bicep' = {
  scope: rg
  name: 'monitoring'
  params: {
    logAnalyticsName: 'log-${resourceToken}'
    appInsightsName: 'appi-${resourceToken}'
    location: location
    tags: tags
  }
}

module storage 'modules/storage.bicep' = {
  scope: rg
  name: 'storage'
  params: {
    name: 'st${resourceToken}'
    location: location
    tags: tags
  }
}

module rbac 'modules/rbac.bicep' = {
  scope: rg
  name: 'rbac'
  params: {
    storageAccountName: storage.outputs.name
    appInsightsName: monitoring.outputs.appInsightsName
    identityPrincipalId: identity.outputs.principalId
    userPrincipalId: principalId
  }
}

// The collector lists models in every region of this subscription.
resource subscriptionReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(subscription().id, environmentName, location, readerRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', readerRoleId)
    principalId: identity.outputs.principalId
    principalType: 'ServicePrincipal'
  }
}

module functionApp 'modules/functionapp.bicep' = {
  scope: rg
  name: 'functionapp'
  params: {
    name: 'func-${resourceToken}'
    planName: 'plan-${resourceToken}'
    location: location
    tags: union(tags, { 'azd-service-name': 'api' })
    identityId: identity.outputs.id
    identityClientId: identity.outputs.clientId
    blobEndpoint: storage.outputs.blobEndpoint
    queueEndpoint: storage.outputs.queueEndpoint
    tableEndpoint: storage.outputs.tableEndpoint
    deploymentContainerName: storage.outputs.deploymentContainerName
    appInsightsConnectionString: monitoring.outputs.connectionString
    maximumInstanceCount: maximumInstanceCount
    instanceMemoryMB: instanceMemoryMB
  }
  dependsOn: [
    rbac
  ]
}

output AZURE_LOCATION string = location
output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_FUNCTION_APP_NAME string = functionApp.outputs.name
output AZURE_FUNCTION_APP_HOST string = functionApp.outputs.defaultHostName
output STORAGE_BLOB_ENDPOINT string = storage.outputs.blobEndpoint
output STORAGE_TABLE_ENDPOINT string = storage.outputs.tableEndpoint
