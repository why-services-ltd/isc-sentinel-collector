metadata description = '''
SailPoint ISC -> Microsoft Sentinel audit event collector.

Deploys the Sentinel workspace, the normalised custom table, the Azure Monitor
ingestion path (DCE + DCR), and two separately-identified function apps: a
collector and a credential rotator.

Deliberately absent: any parameter, output or resource that could carry the ISC
credential. It is seeded once out-of-band and thereafter owned by the rotator.
See infra/README.md and docs/threat-model.md.
'''

targetScope = 'resourceGroup'

// ---------------------------------------------------------------------------
// Parameters
// ---------------------------------------------------------------------------

@description('Short workload name; used to derive resource names.')
@minLength(3)
@maxLength(12)
param workloadName string = 'iscsiem'

@description('Environment suffix, e.g. dev / prd.')
@allowed(['dev', 'tst', 'prd'])
param environment string

@description('Azure region. Must support Flex Consumption (FC1): verify with `az functionapp list-flexconsumption-locations`.')
param location string = resourceGroup().location

@description('ISC tenant base URL, e.g. https://acme.api.identitynow.com. Not a secret.')
param iscBaseUrl string

@description('''
Send ISC events to a Log Analytics / Sentinel workspace you already own,
instead of creating one. Most organisations adopting this already run Sentinel
and will want the events in their existing workspace.
When true, set existingWorkspaceName (and existingWorkspaceResourceGroup if it
is not in this resource group).
''')
param useExistingWorkspace bool = false

@description('Name of the existing workspace. Required when useExistingWorkspace is true; ignored otherwise.')
param existingWorkspaceName string = ''

@description('''
Resource group holding the existing workspace. Defaults to this deployment's
resource group. Must be in the same subscription as this deployment -- a
cross-subscription workspace needs the table and Sentinel onboarding deployed
separately, at that subscription's scope.
''')
param existingWorkspaceResourceGroup string = ''

@description('''
Onboard the event workspace to Microsoft Sentinel. Idempotent, so it is safe to
leave true for a workspace that is already onboarded. Set false only to land the
events in a plain Log Analytics workspace with no Sentinel.
''')
param enableSentinel bool = true

@description('''
Create a second workspace for the pipeline's own telemetry (function traces,
exceptions, dependency calls), keeping it out of the workspace that holds ISC
event data. When false, Application Insights attaches to the event workspace
instead -- simpler and cheaper, but pipeline logs then share the Sentinel
workspace's billing rate, retention and hunting surface.
See infra/README.md, "Design notes", for the trade-off.
''')
param createOpsWorkspace bool = true

@description('''
Send the pipeline's own telemetry to an operational workspace you already run —
a central platform-logs workspace, for example — instead of creating one.
Setting this takes precedence over createOpsWorkspace: no ops workspace is
created, and Application Insights attaches to the named workspace.
Must be in the same subscription as this deployment.
''')
param existingOpsWorkspaceName string = ''

@description('Resource group of the existing ops workspace. Defaults to this deployment\'s resource group.')
param existingOpsWorkspaceResourceGroup string = ''

// ---------------------------------------------------------------------------
// Optional name overrides
//
// Every resource name is derived from workloadName/environment by default, so
// a minimal deployment needs none of these. Set any of them to impose your own
// naming convention instead. Leave empty to keep the derived name.
//
// Storage and Key Vault names are globally unique and length-limited (24
// characters, and storage must be lowercase alphanumeric); the derived
// defaults append a hash of the resource group id to stay unique. If you
// override them, uniqueness becomes your problem.
// ---------------------------------------------------------------------------

@description('Name for the created event workspace. Ignored when useExistingWorkspace is true.')
param workspaceNameOverride string = ''

@description('Name for the created ops workspace. Ignored when createOpsWorkspace is false.')
param opsWorkspaceNameOverride string = ''

@description('Name for the Application Insights component.')
param appInsightsNameOverride string = ''

@description('Name for the Key Vault. Globally unique, 3-24 chars.')
@maxLength(24)
param keyVaultNameOverride string = ''

@description('Name for the storage account. Globally unique, 3-24 chars, lowercase alphanumeric only.')
@maxLength(24)
param storageAccountNameOverride string = ''

@description('Name for the data collection endpoint.')
param dceNameOverride string = ''

@description('Name for the data collection rule.')
param dcrNameOverride string = ''

@description('Name for the collector function app and its hosting plan (the plan gets a -plan suffix).')
param collectorAppNameOverride string = ''

@description('Name for the rotator function app and its hosting plan (the plan gets a -plan suffix).')
param rotatorAppNameOverride string = ''

@description('Name for the alert action group.')
param actionGroupNameOverride string = ''

@description('''
Name of the custom Log Analytics table for ISC events. Must end in _CL. The DCR
stream name is derived from it, so changing this after first deployment orphans
the previous table rather than renaming it.
''')
param eventTableName string = 'SailPointISC_CL'

@description('Name of the Key Vault secret holding the collector credential.')
param collectorSecretNameOverride string = ''

@description('Name of the Key Vault secret holding the rotator credential.')
param rotatorSecretNameOverride string = ''

@description('''
Interactive (hot) retention for the ISC event table, in days.
Personal data: this figure must match the retention agreed in the ROPA/DPIA.
''')
@minValue(4)
@maxValue(730)
param eventRetentionInDays int = 90

@description('Total retention including the cheaper long-term tier, in days. Must be >= eventRetentionInDays.')
@minValue(4)
@maxValue(4383)
param eventTotalRetentionInDays int = 365

@description('''
Retention for the pipeline's own operational telemetry, in days. This workspace
holds no ISC event data, so it is not subject to the ROPA/DPIA retention figure.
''')
@minValue(30)
@maxValue(730)
param opsRetentionInDays int = 30

@description('How often the collector polls ISC, as an NCRONTAB expression. Default: every 5 minutes.')
param collectorSchedule string = '0 */5 * * * *'

@description('How often the rotator checks whether rotation is due, as an NCRONTAB expression. Default: 03:00 every Monday. Whether it actually rotates on a given check depends on logic.DEFAULT_MAX_AGE, not this schedule alone.')
param rotatorSchedule string = '0 0 3 * * 1'

@description('Email address for pipeline health alerts. Empty disables alerting.')
param alertEmailAddress string = ''

@description('Tags applied to every resource.')
param tags object = {}

// ---------------------------------------------------------------------------
// Naming
// ---------------------------------------------------------------------------

var suffix = uniqueString(resourceGroup().id, workloadName, environment)
var namePrefix = '${workloadName}-${environment}'

var workspaceName = empty(workspaceNameOverride) ? 'log-${namePrefix}' : workspaceNameOverride
var opsWorkspaceName = empty(opsWorkspaceNameOverride) ? 'log-${namePrefix}-ops' : opsWorkspaceNameOverride
var appInsightsName = empty(appInsightsNameOverride) ? 'appi-${namePrefix}' : appInsightsNameOverride
var keyVaultName = empty(keyVaultNameOverride) ? take('kv-${workloadName}${environment}${suffix}', 24) : keyVaultNameOverride
var storageAccountName = empty(storageAccountNameOverride) ? take('st${workloadName}${environment}${suffix}', 24) : storageAccountNameOverride
var dceName = empty(dceNameOverride) ? 'dce-${namePrefix}' : dceNameOverride
var dcrName = empty(dcrNameOverride) ? 'dcr-${namePrefix}' : dcrNameOverride
var collectorAppName = empty(collectorAppNameOverride) ? 'func-${namePrefix}-collector' : collectorAppNameOverride
var rotatorAppName = empty(rotatorAppNameOverride) ? 'func-${namePrefix}-rotator' : rotatorAppNameOverride
var collectorPlanName = empty(collectorAppNameOverride) ? 'asp-${namePrefix}-collector' : '${collectorAppNameOverride}-plan'
var rotatorPlanName = empty(rotatorAppNameOverride) ? 'asp-${namePrefix}-rotator' : '${rotatorAppNameOverride}-plan'
var actionGroupName = empty(actionGroupNameOverride) ? 'ag-${namePrefix}' : actionGroupNameOverride

var tableName = eventTableName
var streamName = 'Custom-${tableName}'
var collectorDeploymentContainerName = 'app-package-collector'
var rotatorDeploymentContainerName = 'app-package-rotator'

// Two separate ISC credentials, not one shared between both apps. The
// collector's is search-only; the rotator mints and rotates both, using its
// own manage-scoped credential -- the collector's is never capable of
// managing PATs at all, closing the privilege gap recorded in
// docs/threat-model.md.
var collectorCredentialSecretName = empty(collectorSecretNameOverride) ? 'isc-api-credential-collector' : collectorSecretNameOverride
var rotatorCredentialSecretName = empty(rotatorSecretNameOverride) ? 'isc-api-credential-rotator' : rotatorSecretNameOverride

var allTags = union(tags, {
  workload: workloadName
  environment: environment
  dataClassification: 'personal-data'
})

// ---------------------------------------------------------------------------
// Normalised event schema
//
// Declared once. The DCR stream uses lowercase type names ('datetime'); the
// Log Analytics table API uses camelCase ('dateTime'). Mapping between them
// here keeps the two definitions from drifting apart, which is a failure mode
// that surfaces only at ingestion time as silently dropped columns.
// ---------------------------------------------------------------------------

var normalisedColumns = [
  { name: 'TimeGenerated', type: 'datetime' }
  { name: 'EventId', type: 'string' }
  { name: 'EventName', type: 'string' }
  { name: 'EventAction', type: 'string' }
  { name: 'EventType', type: 'string' }
  { name: 'EventStatus', type: 'string' }
  { name: 'ActorName', type: 'string' }
  { name: 'ActorId', type: 'string' }
  { name: 'TargetName', type: 'string' }
  { name: 'TargetId', type: 'string' }
  { name: 'TargetType', type: 'string' }
  { name: 'SourceName', type: 'string' }
  { name: 'Application', type: 'string' }
  { name: 'IpAddress', type: 'string' }
  { name: 'TrackingNumber', type: 'string' }
  { name: 'TechnicalName', type: 'string' }
  { name: 'Details', type: 'string' }
  { name: 'Attributes', type: 'dynamic' }
  { name: 'RawEvent', type: 'string' }
]

var tableColumns = map(normalisedColumns, c => {
  name: c.name
  type: c.type == 'datetime' ? 'dateTime' : c.type
})

// ---------------------------------------------------------------------------
// Sentinel workspace + custom table
// ---------------------------------------------------------------------------

// Resource group holding the event workspace: this one unless the adopter
// pointed at an existing workspace elsewhere.
var workspaceResourceGroupName = useExistingWorkspace && !empty(existingWorkspaceResourceGroup)
  ? existingWorkspaceResourceGroup
  : resourceGroup().name

// Resolved by resourceId() rather than an `existing` resource reference, so
// the template never needs to read an existing workspace's properties -- it
// only needs its id, for the DCR destination and the alert scope.
var eventWorkspaceResourceId = useExistingWorkspace
  ? resourceId(
      subscription().subscriptionId,
      workspaceResourceGroupName,
      'Microsoft.OperationalInsights/workspaces',
      existingWorkspaceName
    )
  : createdWorkspace.id

resource createdWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = if (!useExistingWorkspace) {
  name: workspaceName
  location: location
  tags: allTags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: eventRetentionInDays
    features: {
      // Resource-context-only access breaks visibility for custom tables
      // ingested via DCR: rows have no per-row monitored-resource association
      // for that model to check against, so even workspace Owner returned zero
      // rows despite ingestion succeeding -- discovered when the collector's
      // confirmed-successful run (711 events, 204s from the DCE) was
      // unqueryable. Standard workspace-level RBAC is correct for this
      // single-purpose pipeline workspace.
      enableLogAccessUsingOnlyResourcePermissions: false
    }
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

// The table (and Sentinel onboarding) go through a module so they can target
// the workspace's own resource group, which may not be this one when an
// existing workspace is reused. See infra/modules/event-table.bicep.
module eventTable 'modules/event-table.bicep' = {
  name: 'deploy-isc-event-table'
  scope: resourceGroup(workspaceResourceGroupName)
  params: {
    workspaceName: useExistingWorkspace ? existingWorkspaceName : workspaceName
    tableName: tableName
    columns: tableColumns
    retentionInDays: eventRetentionInDays
    totalRetentionInDays: eventTotalRetentionInDays
    enableSentinel: enableSentinel
  }
  dependsOn: [
    createdWorkspace
  ]
}

// ---------------------------------------------------------------------------
// Ingestion path: data collection endpoint + rule
// ---------------------------------------------------------------------------

resource dce 'Microsoft.Insights/dataCollectionEndpoints@2023-03-11' = {
  name: dceName
  location: location
  tags: allTags
  properties: {
    networkAcls: {
      publicNetworkAccess: 'Enabled'
    }
  }
}

resource dcr 'Microsoft.Insights/dataCollectionRules@2023-03-11' = {
  name: dcrName
  location: location
  tags: allTags
  properties: {
    dataCollectionEndpointId: dce.id
    streamDeclarations: {
      '${streamName}': {
        columns: normalisedColumns
      }
    }
    destinations: {
      logAnalytics: [
        {
          workspaceResourceId: eventWorkspaceResourceId
          name: 'sentinelWorkspace'
        }
      ]
    }
    dataFlows: [
      {
        streams: [streamName]
        destinations: ['sentinelWorkspace']
        transformKql: 'source'
        outputStream: streamName
      }
    ]
  }
  dependsOn: [
    eventTable
  ]
}

// ---------------------------------------------------------------------------
// Storage
//
// allowSharedKeyAccess is false: this removes the account key as a credential
// entirely. Both function apps therefore reach storage with their managed
// identity, including for the Flex Consumption deployment container.
// ---------------------------------------------------------------------------

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  tags: allTags
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    allowSharedKeyAccess: false
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Allow'
    }
  }
}

resource blobServices 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {}
}

// One container per app. A shared container previously caused each app's
// Flex Consumption "deployment package" pointer to resolve to the same blob,
// so publishing one app silently overwrote the other's running code --
// discovered when the collector started executing the rotator's function
// after both had been published.
resource collectorDeploymentContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobServices
  name: collectorDeploymentContainerName
  properties: {
    publicAccess: 'None'
  }
}

resource rotatorDeploymentContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobServices
  name: rotatorDeploymentContainerName
  properties: {
    publicAccess: 'None'
  }
}

resource stateContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobServices
  name: 'collector-state'
  properties: {
    publicAccess: 'None'
  }
}

// ---------------------------------------------------------------------------
// Key Vault
//
// No secret is created here. The ISC credential is seeded once by CLI after
// deployment and is thereafter owned by the rotator. A @secure() parameter
// would place it in deployment history, which outlives the credential.
// ---------------------------------------------------------------------------

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  tags: allTags
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    enablePurgeProtection: true
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Allow'
    }
  }
}

// ---------------------------------------------------------------------------
// Operational telemetry
//
// A separate workspace, deliberately. The pipeline's own logs are platform
// telemetry, not security data, and the SOC's Sentinel workspace stays clean:
// everything landing there bills at Sentinel rates, inherits Sentinel retention,
// and shows up in the SOC's content and hunting surface. Function traces have no
// business in any of that.
//
// This workspace is NOT onboarded to Sentinel and carries no ISC event data, so
// it sits outside the ROPA/DPIA scope that governs the event table.
// ---------------------------------------------------------------------------

resource opsWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = if (createOpsWorkspace && empty(existingOpsWorkspaceName)) {
  name: opsWorkspaceName
  location: location
  tags: union(allTags, {
    dataClassification: 'operational'
  })
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: opsRetentionInDays
    features: {
      // Resource-context-only access breaks visibility for custom tables
      // ingested via DCR: rows have no per-row monitored-resource association
      // for that model to check against, so even workspace Owner returned zero
      // rows despite ingestion succeeding -- discovered when the collector's
      // confirmed-successful run (711 events, 204s from the DCE) was
      // unqueryable. Standard workspace-level RBAC is correct for this
      // single-purpose pipeline workspace.
      enableLogAccessUsingOnlyResourcePermissions: false
    }
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

// Where App Insights stores its data: the dedicated ops workspace when one is
// created, otherwise the event workspace. Falling back to the event workspace
// keeps the deployment to a single workspace for adopters who do not want a
// second one -- at the cost of pipeline telemetry sharing that workspace's
// billing rate, retention and hunting surface.
// Three-way choice, in precedence order:
//   1. an ops workspace the adopter already runs (existingOpsWorkspaceName)
//   2. a dedicated ops workspace created here (createOpsWorkspace)
//   3. the event workspace itself, keeping the deployment to one workspace
var useExistingOpsWorkspace = !empty(existingOpsWorkspaceName)

var opsWorkspaceResourceGroupName = !empty(existingOpsWorkspaceResourceGroup)
  ? existingOpsWorkspaceResourceGroup
  : resourceGroup().name

var appInsightsWorkspaceResourceId = useExistingOpsWorkspace
  ? resourceId(
      subscription().subscriptionId,
      opsWorkspaceResourceGroupName,
      'Microsoft.OperationalInsights/workspaces',
      existingOpsWorkspaceName
    )
  : (createOpsWorkspace ? opsWorkspace.id : eventWorkspaceResourceId)

// Local auth is disabled, so the function apps authenticate telemetry with
// their managed identity rather than an instrumentation key.
resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  tags: allTags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: appInsightsWorkspaceResourceId
    DisableLocalAuth: true
    IngestionMode: 'LogAnalytics'
  }
}

// ---------------------------------------------------------------------------
// Hosting plans (Flex Consumption)
//
// One serverfarm per app: Flex Consumption allows exactly one site per plan
// ("There can only be one site per Flex Consumption serverfarm"), discovered
// the hard way when a shared plan rejected the second function app at deploy
// time. what-if does not catch this -- it is a Microsoft.Web business rule,
// not something the ARM template graph encodes.
// ---------------------------------------------------------------------------

resource collectorPlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: collectorPlanName
  location: location
  tags: allTags
  kind: 'functionapp'
  sku: {
    name: 'FC1'
    tier: 'FlexConsumption'
  }
  properties: {
    reserved: true
  }
}

resource rotatorPlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: rotatorPlanName
  location: location
  tags: allTags
  kind: 'functionapp'
  sku: {
    name: 'FC1'
    tier: 'FlexConsumption'
  }
  properties: {
    reserved: true
  }
}

// ---------------------------------------------------------------------------
// Function apps
//
// Two apps, two system-assigned identities. The collector may only read the
// credential; only the rotator may write it. Merging them would give the
// internet-facing poller the ability to mint ISC credentials.
// ---------------------------------------------------------------------------

var commonAppSettings = [
  {
    name: 'AzureWebJobsStorage__accountName'
    value: storage.name
  }
  {
    name: 'AzureWebJobsStorage__credential'
    value: 'managedidentity'
  }
  {
    name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
    value: appInsights.properties.ConnectionString
  }
  {
    name: 'APPLICATIONINSIGHTS_AUTHENTICATION_STRING'
    value: 'Authorization=AAD'
  }
  {
    name: 'KEY_VAULT_URI'
    value: keyVault.properties.vaultUri
  }
  {
    name: 'ISC_BASE_URL'
    value: iscBaseUrl
  }
]

resource collectorApp 'Microsoft.Web/sites@2023-12-01' = {
  name: collectorAppName
  location: location
  tags: allTags
  kind: 'functionapp,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: collectorPlan.id
    httpsOnly: true
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storage.properties.primaryEndpoints.blob}${collectorDeploymentContainerName}'
          authentication: {
            type: 'SystemAssignedIdentity'
          }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: 40
        instanceMemoryMB: 2048
      }
      runtime: {
        name: 'python'
        version: '3.11'
      }
    }
    siteConfig: {
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      appSettings: union(commonAppSettings, [
        {
          name: 'CREDENTIAL_SECRET_NAME'
          value: collectorCredentialSecretName
        }
        {
          name: 'DCE_ENDPOINT'
          value: dce.properties.logsIngestion.endpoint
        }
        {
          name: 'DCR_IMMUTABLE_ID'
          value: dcr.properties.immutableId
        }
        {
          name: 'DCR_STREAM_NAME'
          value: streamName
        }
        {
          name: 'STATE_STORAGE_ACCOUNT'
          value: storage.name
        }
        {
          name: 'STATE_CONTAINER_NAME'
          value: stateContainer.name
        }
        {
          name: 'COLLECTOR_SCHEDULE'
          value: collectorSchedule
        }
      ])
    }
  }
  dependsOn: [
    collectorDeploymentContainer
  ]
}

resource rotatorApp 'Microsoft.Web/sites@2023-12-01' = {
  name: rotatorAppName
  location: location
  tags: allTags
  kind: 'functionapp,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: rotatorPlan.id
    httpsOnly: true
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storage.properties.primaryEndpoints.blob}${rotatorDeploymentContainerName}'
          authentication: {
            type: 'SystemAssignedIdentity'
          }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: 1
        instanceMemoryMB: 2048
      }
      runtime: {
        name: 'python'
        version: '3.11'
      }
    }
    siteConfig: {
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      appSettings: union(commonAppSettings, [
        {
          name: 'COLLECTOR_CREDENTIAL_SECRET_NAME'
          value: collectorCredentialSecretName
        }
        {
          name: 'ROTATOR_CREDENTIAL_SECRET_NAME'
          value: rotatorCredentialSecretName
        }
        {
          name: 'ROTATOR_SCHEDULE'
          value: rotatorSchedule
        }
      ])
    }
  }
  dependsOn: [
    rotatorDeploymentContainer
  ]
}

// ---------------------------------------------------------------------------
// RBAC
// ---------------------------------------------------------------------------

var roleIds = {
  keyVaultSecretsUser: '4633458b-17de-408a-b874-0445c86b69e6'
  keyVaultSecretsOfficer: 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'
  storageBlobDataOwner: 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
  storageQueueDataContributor: '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
  monitoringMetricsPublisher: '3913510d-42f4-4e42-8a64-420c390055eb'
}

// Collector: read the credential only.
resource collectorKeyVaultRead 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: keyVault
  name: guid(keyVault.id, collectorApp.id, roleIds.keyVaultSecretsUser)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.keyVaultSecretsUser)
    principalId: collectorApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Rotator: write the credential. Deliberately not granted to the collector.
resource rotatorKeyVaultWrite 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: keyVault
  name: guid(keyVault.id, rotatorApp.id, roleIds.keyVaultSecretsOfficer)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.keyVaultSecretsOfficer)
    principalId: rotatorApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Storage: required because shared key access is disabled.
resource collectorStorageBlob 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, collectorApp.id, roleIds.storageBlobDataOwner)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.storageBlobDataOwner)
    principalId: collectorApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource collectorStorageQueue 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, collectorApp.id, roleIds.storageQueueDataContributor)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.storageQueueDataContributor)
    principalId: collectorApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource rotatorStorageBlob 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, rotatorApp.id, roleIds.storageBlobDataOwner)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.storageBlobDataOwner)
    principalId: rotatorApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource rotatorStorageQueue 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, rotatorApp.id, roleIds.storageQueueDataContributor)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.storageQueueDataContributor)
    principalId: rotatorApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Ingestion: only the collector may publish to the DCR.
resource collectorDcrPublish 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: dcr
  name: guid(dcr.id, collectorApp.id, roleIds.monitoringMetricsPublisher)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.monitoringMetricsPublisher)
    principalId: collectorApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Telemetry: App Insights local auth is disabled, so both apps need this.
resource collectorAppInsightsPublish 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: appInsights
  name: guid(appInsights.id, collectorApp.id, roleIds.monitoringMetricsPublisher)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.monitoringMetricsPublisher)
    principalId: collectorApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource rotatorAppInsightsPublish 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: appInsights
  name: guid(appInsights.id, rotatorApp.id, roleIds.monitoringMetricsPublisher)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.monitoringMetricsPublisher)
    principalId: rotatorApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// ---------------------------------------------------------------------------
// Alerting
//
// A silent collector is a security failure, not just an availability one: the
// governance layer stops being watched and nothing complains. These rules make
// silence noisy.
// ---------------------------------------------------------------------------

var alertingEnabled = !empty(alertEmailAddress)

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = if (alertingEnabled) {
  name: actionGroupName
  location: 'global'
  tags: allTags
  properties: {
    groupShortName: take(workloadName, 12)
    enabled: true
    emailReceivers: [
      {
        name: 'primary'
        emailAddress: alertEmailAddress
        useCommonAlertSchema: true
      }
    ]
  }
}

resource collectorSilentAlert 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = if (alertingEnabled) {
  name: 'alert-${namePrefix}-collector-silent'
  location: location
  tags: allTags
  properties: {
    displayName: 'ISC collector has ingested no events'
    description: 'No SailPoint ISC audit events reached Sentinel in the last 3 hours. The identity governance layer is unmonitored until this clears.'
    severity: 1
    enabled: true
    evaluationFrequency: 'PT30M'
    windowSize: 'PT3H'
    scopes: [eventWorkspaceResourceId]
    criteria: {
      allOf: [
        {
          query: '${tableName} | summarize Events = count()'
          timeAggregation: 'Total'
          metricMeasureColumn: 'Events'
          operator: 'LessThanOrEqual'
          threshold: 0
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    autoMitigate: true
    actions: {
      actionGroups: [actionGroup.id]
    }
  }
  dependsOn: [
    eventTable
  ]
}

resource functionFailureAlert 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = if (alertingEnabled) {
  name: 'alert-${namePrefix}-function-failures'
  location: location
  tags: allTags
  properties: {
    displayName: 'ISC pipeline function raised exceptions'
    description: 'The collector or rotator logged exceptions. A failed rotation must be investigated before the ISC credential expires.'
    severity: 2
    enabled: true
    evaluationFrequency: 'PT15M'
    windowSize: 'PT1H'
    scopes: [appInsights.id]
    criteria: {
      allOf: [
        {
          // Classic schema name, deliberately: this alert's scope is the App
          // Insights *component* (appInsights.id), and Azure's alerting engine
          // translates classic names (exceptions, traces, requests) to the
          // underlying App-prefixed workspace tables (AppExceptions, ...) for
          // that scope type. Direct workspace queries (e.g.
          // `az monitor log-analytics query --workspace <id>`) need the
          // App-prefixed names instead -- confirmed the hard way when "fixing"
          // this to AppExceptions broke ARM template validation outright
          // ("Failed to resolve table or column expression named
          // 'AppExceptions'"), since that name doesn't resolve through this
          // scope type.
          query: 'exceptions | summarize Failures = count()'
          timeAggregation: 'Total'
          metricMeasureColumn: 'Failures'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    autoMitigate: true
    actions: {
      actionGroups: [actionGroup.id]
    }
  }
}

// ---------------------------------------------------------------------------
// Outputs
//
// No secret, and no value from which one could be derived.
// ---------------------------------------------------------------------------

output workspaceName string = useExistingWorkspace ? existingWorkspaceName : workspaceName
output workspaceResourceId string = eventWorkspaceResourceId
output workspaceResourceGroup string = workspaceResourceGroupName
output opsWorkspaceName string = useExistingOpsWorkspace ? existingOpsWorkspaceName : (createOpsWorkspace ? opsWorkspaceName : '')
output opsWorkspaceResourceId string = appInsightsWorkspaceResourceId
output appInsightsName string = appInsights.name
output keyVaultName string = keyVault.name
output collectorCredentialSecretName string = collectorCredentialSecretName
output rotatorCredentialSecretName string = rotatorCredentialSecretName
output storageAccountName string = storage.name
output collectorAppName string = collectorApp.name
output rotatorAppName string = rotatorApp.name
output collectorPrincipalId string = collectorApp.identity.principalId
output rotatorPrincipalId string = rotatorApp.identity.principalId
output dceLogsIngestionEndpoint string = dce.properties.logsIngestion.endpoint
output dcrImmutableId string = dcr.properties.immutableId
output tableName string = tableName
