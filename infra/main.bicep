targetScope = 'subscription'

@minLength(1)
@maxLength(48)
@description('Environment name; used for the resource group name and the resource name token.')
param environmentName string

@minLength(1)
@description('Region for all resources; must support Flex Consumption.')
param location string

@maxLength(90)
@description('Optional resource group name; defaults to rg-<environmentName>.')
param resourceGroupName string = ''

@description('Resource group metadata location; preserve this value when selecting an existing group.')
param resourceGroupLocation string = location

@maxLength(16)
@description('Optional lowercase resource name prefix; empty preserves the original names. Storage uses up to nine alphanumeric prefix characters.')
param resourceNamePrefix string = ''

@description('Optional user object ID granted Blob/Table data access for local development.')
param principalId string = ''

@description('Opt in to an hourly collection health alert after the first successful run; Azure Monitor charges may apply.')
param enableCollectionAlerts bool = false

@description('Optional existing Action Group resource IDs used by the collection health alert.')
param alertActionGroupIds array = []

@minValue(40)
@maxValue(1000)
param maximumInstanceCount int = 40

@allowed([512, 2048, 4096])
param instanceMemoryMB int = 2048

var tags = { 'azd-env-name': environmentName }
var resourceToken = empty(resourceGroupName) || resourceGroupName == 'rg-${environmentName}'
  ? toLower(uniqueString(subscription().id, environmentName, location))
  : toLower(uniqueString(subscription().id, environmentName, location, resourceGroupName))
var resourceSuffix = empty(resourceNamePrefix) ? resourceToken : '${resourceNamePrefix}-${resourceToken}'
var readerRoleId = 'acdd72a7-3385-48ef-bd42-f606fba81ae7'

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: empty(resourceGroupName) ? 'rg-${environmentName}' : resourceGroupName
  location: resourceGroupLocation
  tags: tags
}

module identity 'modules/identity.bicep' = {
  scope: rg
  name: 'identity'
  params: {
    name: 'id-${resourceSuffix}'
    location: location
    tags: tags
  }
}

module monitoring 'modules/monitoring.bicep' = {
  scope: rg
  name: 'monitoring'
  params: {
    logAnalyticsName: 'log-${resourceSuffix}'
    appInsightsName: 'appi-${resourceSuffix}'
    location: location
    tags: tags
    enableCollectionAlerts: enableCollectionAlerts
    alertActionGroupIds: alertActionGroupIds
  }
}

module storage 'modules/storage.bicep' = {
  scope: rg
  name: 'storage'
  params: {
    name: 'st${take(replace(resourceNamePrefix, '-', ''), 9)}${resourceToken}'
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
  name: empty(resourceNamePrefix) && rg.name == 'rg-${environmentName}'
    ? guid(subscription().id, environmentName, location, readerRoleId)
    : guid(subscription().id, rg.name, resourceSuffix, readerRoleId)
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
    name: 'func-${resourceSuffix}'
    planName: 'plan-${resourceSuffix}'
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
