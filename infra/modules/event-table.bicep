metadata description = '''
Creates the normalised ISC event table (and optionally onboards Sentinel) on a
Log Analytics workspace.

This lives in a module purely so it can be deployed at the *workspace's*
resource group scope. When an adopter reuses a workspace they already own, that
workspace is frequently in a different resource group from this pipeline's
resources, and a table is a child of the workspace -- it cannot be declared
from another resource group's deployment.
'''

targetScope = 'resourceGroup'

@description('Name of the workspace to create the table on. Must already exist, or be created by the caller.')
param workspaceName string

@description('Name of the custom table, including the _CL suffix.')
param tableName string

@description('Column definitions, in Log Analytics table API form (camelCase types).')
param columns array

@description('Interactive (hot) retention for the table, in days.')
param retentionInDays int

@description('Total retention including the long-term tier, in days.')
param totalRetentionInDays int

@description('''
Onboard the workspace to Microsoft Sentinel. Safe to leave true for a workspace
that is already onboarded -- the operation is idempotent. Set false only if you
deliberately want the events in a plain Log Analytics workspace with no Sentinel.
''')
param enableSentinel bool = true

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: workspaceName
}

resource sentinelOnboarding 'Microsoft.SecurityInsights/onboardingStates@2024-09-01' = if (enableSentinel) {
  scope: workspace
  name: 'default'
  properties: {}
}

resource iscTable 'Microsoft.OperationalInsights/workspaces/tables@2022-10-01' = {
  parent: workspace
  name: tableName
  properties: {
    plan: 'Analytics'
    retentionInDays: retentionInDays
    totalRetentionInDays: totalRetentionInDays
    schema: {
      name: tableName
      columns: columns
    }
  }
}

output tableName string = iscTable.name
