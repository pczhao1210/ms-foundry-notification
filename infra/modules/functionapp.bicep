param name string
param planName string
param location string
param tags object
param identityId string
param identityClientId string
param blobEndpoint string
param queueEndpoint string
param tableEndpoint string
param deploymentContainerName string
param appInsightsConnectionString string
param maximumInstanceCount int
param instanceMemoryMB int
param dailyCollectSchedule string

resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: planName
  location: location
  tags: tags
  kind: 'functionapp'
  sku: {
    name: 'FC1'
    tier: 'FlexConsumption'
  }
  properties: {
    reserved: true
  }
}

resource site 'Microsoft.Web/sites@2024-04-01' = {
  name: name
  location: location
  tags: tags
  kind: 'functionapp,linux'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityId}': {}
    }
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      cors: {
        allowedOrigins: [
          'https://portal.azure.com'
        ]
        supportCredentials: false
      }
      appSettings: [
        { name: 'AzureWebJobsStorage__credential', value: 'managedidentity' }
        { name: 'AzureWebJobsStorage__clientId', value: identityClientId }
        { name: 'AzureWebJobsStorage__blobServiceUri', value: blobEndpoint }
        { name: 'AzureWebJobsStorage__queueServiceUri', value: queueEndpoint }
        { name: 'AzureWebJobsStorage__tableServiceUri', value: tableEndpoint }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsightsConnectionString }
        { name: 'APPLICATIONINSIGHTS_AUTHENTICATION_STRING', value: 'ClientId=${identityClientId};Authorization=AAD' }
        { name: 'AZURE_CLIENT_ID', value: identityClientId }
        { name: 'FOUNDRY_SUBSCRIPTION_ID', value: subscription().subscriptionId }
        { name: 'STORAGE_BLOB_ENDPOINT', value: blobEndpoint }
        { name: 'STORAGE_TABLE_ENDPOINT', value: tableEndpoint }
        { name: 'DAILY_COLLECT_SCHEDULE', value: dailyCollectSchedule }
      ]
    }
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${blobEndpoint}${deploymentContainerName}'
          authentication: {
            type: 'UserAssignedIdentity'
            userAssignedIdentityResourceId: identityId
          }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: maximumInstanceCount
        instanceMemoryMB: instanceMemoryMB
      }
      runtime: {
        name: 'python'
        version: '3.12'
      }
    }
  }
}

resource ftpPublishing 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-04-01' = {
  parent: site
  name: 'ftp'
  properties: {
    allow: false
  }
}

resource scmPublishing 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-04-01' = {
  parent: site
  name: 'scm'
  properties: {
    allow: false
  }
}

output name string = site.name
output defaultHostName string = site.properties.defaultHostName
