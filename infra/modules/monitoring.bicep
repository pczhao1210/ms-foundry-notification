param logAnalyticsName string
param appInsightsName string
param location string
param tags object

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsName
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: workspace.id
    // Telemetry must be sent with Entra ID (Monitoring Metrics Publisher), not the instrumentation key.
    DisableLocalAuth: true
  }
}

output appInsightsName string = appInsights.name
output connectionString string = appInsights.properties.ConnectionString
