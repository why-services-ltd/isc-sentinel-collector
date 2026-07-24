using './main.bicep'

// Example parameters. Copy this file, then edit your copy:
//
//   cp infra/main.bicepparam infra/prd.bicepparam
//
// .gitignore keeps every parameter file except this one out of git, so your
// copy stays local.
//
// This file contains no secret and must never contain one. The ISC credentials
// are seeded straight into Key Vault out-of-band and thereafter owned by the
// rotator — there is deliberately no parameter through which one could be
// passed. See infra/README.md, step 4.
//
// Parameters that are commented out are optional. Uncomment only what you want
// to change — anything left commented falls back to the default declared in
// main.bicep. Where a commented line shows a value, it is either that default
// (for example `createOpsWorkspace = true`) or an illustrative example (for
// example a workspace name); the comment above each says which.

// ---------------------------------------------------------------------------
// Required
// ---------------------------------------------------------------------------

// dev | tst | prd. Combined with workloadName to derive resource names.
param environment = 'prd'

// Your ISC tenant's API host. Note the `.api.` — the UI host will 404.
param iscBaseUrl = 'https://REPLACE-ME.api.identitynow.com'

// ---------------------------------------------------------------------------
// Placement and naming
// ---------------------------------------------------------------------------

// Prefix for every derived resource name, 3-12 characters.
param workloadName = 'iscsiem'

// Must support Flex Consumption — check with:
//   az functionapp list-flexconsumption-locations -o table
param location = 'uksouth'

// ---------------------------------------------------------------------------
// Workspace layout — the "bring your own" switches
//
// Defaults create everything, which suits a greenfield subscription. If you
// already run Sentinel you almost certainly want useExistingWorkspace = true.
// See infra/README.md, "Choose your workspace layout".
// ---------------------------------------------------------------------------

// Send ISC events to a workspace you already own instead of creating one.
// param useExistingWorkspace = false

// Required when useExistingWorkspace is true.
// param existingWorkspaceName = 'my-existing-sentinel-workspace'

// Only needed if that workspace is in a different resource group from this
// deployment. Must be in the same subscription.
// param existingWorkspaceResourceGroup = 'rg-security-logging'

// Onboard the event workspace to Microsoft Sentinel. Idempotent, so it is safe
// to leave true against an already-onboarded workspace. false = plain Log
// Analytics with no Sentinel.
// param enableSentinel = true

// Where the pipeline's own telemetry (function traces, exceptions, dependency
// calls) goes. Three choices, in precedence order:
//
//   1. An ops workspace you already run — set existingOpsWorkspaceName below.
//      Typical if you have a central platform-logs workspace.
//   2. A dedicated one created here — the default, createOpsWorkspace = true.
//   3. The event workspace itself — set createOpsWorkspace = false. Cheapest
//      and simplest, but puts function logs under the event workspace's
//      billing, retention and access model.

// param createOpsWorkspace = true

// Reuse an ops workspace you already run. Takes precedence over
// createOpsWorkspace: nothing new is created. Same subscription only.
// param existingOpsWorkspaceName = 'log-platform-shared'

// Only needed if that workspace is in a different resource group.
// param existingOpsWorkspaceResourceGroup = 'rg-platform-logging'

// ---------------------------------------------------------------------------
// Retention
// ---------------------------------------------------------------------------

// ISC events are personal data. Match these to your own documented retention
// position rather than accepting the defaults.
param eventRetentionInDays = 90
param eventTotalRetentionInDays = 365

// The pipeline's own telemetry: no ISC event data, so not subject to the
// retention position above. Applies only when this deployment creates the ops
// workspace — ignored if you reuse an existing one or set
// createOpsWorkspace = false, since then its retention is not ours to set.
param opsRetentionInDays = 30

// ---------------------------------------------------------------------------
// Schedules
//
// NCRONTAB: six fields starting with seconds, not five-field cron.
// How often the rotator *checks* is set here; how old a credential must be
// before it is actually replaced is DEFAULT_MAX_AGE in rotator/logic.py.
// See infra/README.md, "Timing and schedules".
// ---------------------------------------------------------------------------

param collectorSchedule = '0 */5 * * * *'   // every 5 minutes
param rotatorSchedule = '0 0 3 * * 1'       // 03:00 every Monday

// ---------------------------------------------------------------------------
// Alerting
// ---------------------------------------------------------------------------

// Empty deploys no alert rules at all. A collector that stops silently means
// the identity governance layer is unwatched — set this before the pipeline
// carries anything you rely on.
param alertEmailAddress = ''

// ---------------------------------------------------------------------------
// Tags
// ---------------------------------------------------------------------------

param tags = {
  owner: 'security-operations'
  costCentre: 'REPLACE-ME'
  service: 'sailpoint-isc-telemetry'
}

// ---------------------------------------------------------------------------
// Name overrides
//
// Every resource name is derived from workloadName and environment. Set any of
// these only if you have a naming standard to meet. Storage and Key Vault names
// are globally unique and capped at 24 characters; the derived defaults append
// a hash of the resource group id to stay unique, so overriding them makes
// uniqueness your problem.
// ---------------------------------------------------------------------------

// Values below are illustrative, not defaults. Leaving one commented keeps the
// derived name.

// param workspaceNameOverride = 'log-sentinel-prod'
// param opsWorkspaceNameOverride = 'log-platform-prod'
// param appInsightsNameOverride = 'appi-isc-collector'
// param keyVaultNameOverride = 'kv-isc-collector-prd'
// param storageAccountNameOverride = 'stisccollectorprd'
// param dceNameOverride = 'dce-isc-collector'
// param dcrNameOverride = 'dcr-isc-collector'
// param collectorAppNameOverride = 'func-isc-collector'
// param rotatorAppNameOverride = 'func-isc-rotator'
// param actionGroupNameOverride = 'ag-isc-collector'

// Name of the custom Log Analytics table. Must end in _CL. Changing it after
// first deployment orphans the previous table rather than renaming it — the
// old table keeps its data and stops receiving new events.
// param eventTableName = 'SailPointISC_CL'

// Key Vault secret names holding each ISC credential. If you change these, the
// bootstrap commands in infra/README.md step 4b must use the new names too —
// they write to these secrets by name, and the apps will not find a credential
// stored under any other name.
// param collectorSecretNameOverride = 'isc-cred-collector'
// param rotatorSecretNameOverride = 'isc-cred-rotator'
