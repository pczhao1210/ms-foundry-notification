param logAnalyticsName string
param appInsightsName string
param location string
param tags object
param enableCollectionAlerts bool = false
param alertActionGroupIds array = []

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

resource collectionHealth 'Microsoft.Insights/scheduledQueryRules@2023-12-01' = if (enableCollectionAlerts) {
  name: '${appInsightsName}-collection-health'
  location: location
  tags: tags
  properties: {
    displayName: 'Foundry collection failed or overdue'
    description: 'Latest daily_collect invocation failed, or no completed invocation in 32 hours. Enable after the first collection.'
    enabled: true
    severity: 2
    evaluationFrequency: 'PT1H'
    windowSize: 'P2D'
    scopes: [workspace.id]
    autoMitigate: true
    skipQueryValidation: true
    criteria: {
      allOf: [
        {
          query: '''
AppRequests
| where Name endswith "daily_collect" or OperationName endswith "daily_collect"
| summarize LastAttempt = max(TimeGenerated), LastSuccess = maxif(TimeGenerated, Success == true)
| where isnull(LastAttempt) or LastAttempt < ago(32h) or isnull(LastSuccess) or LastSuccess < LastAttempt
'''
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    actions: {
      actionGroups: alertActionGroupIds
    }
  }
}

output appInsightsName string = appInsights.name
output connectionString string = appInsights.properties.ConnectionString
