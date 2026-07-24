# Deployment runbook

Everything needed to take this from an empty resource group to a running
collector. Follow it top to bottom.

Commands below use two shell variables you set once. `rg-iscsiem-prd` and
`uksouth` are examples — substitute your own:

```bash
RG=rg-iscsiem-prd
LOCATION=uksouth
```

**Decide up front whether this deployment creates its own Log Analytics
workspace or uses one you already run** — see step 2. If you already have
Sentinel, you almost certainly want to reuse that workspace.

---

## 0. Before you start

### Prerequisites

| Tool | Check |
|---|---|
| Azure CLI ≥ 2.60 | `az version` |
| Bicep CLI ≥ 0.30 | `az bicep version` (install: `az bicep install`) |
| Azure Functions Core Tools v4 | `func --version` |
| Python 3.11 | `python3 --version` |

### Permissions you need

- **Contributor** on the target resource group — creates the resources.
- **User Access Administrator** (or Owner) on the resource group — the template
  creates nine role assignments; Contributor alone will fail on those.
- **Key Vault Secrets Officer** on the vault, *temporarily*, for the one-time
  credential seed in step 4. Remove it afterwards.

### What you need to hand

- The ISC tenant base URL, e.g. `https://acme.api.identitynow.com`.
- **Two** ISC Personal Access Tokens (each a client ID + secret), both created
  under one dedicated service identity (see step 4a), one per app:
  - one scoped **`sp:search:read` only** — the collector's;
  - one scoped **`sp:my-personal-access-tokens:manage` only** — the rotator's.
    This one never calls the search API; it authenticates the calls that mint
    and revoke both apps' replacement PATs.

  Note the scope is `sp:my-personal-access-tokens:manage`, not
  `sp:oauth-client:manage`: OAuth-client management is unavailable to any
  non-interactive caller regardless of scope. See "Constraints that look like
  bugs" below.
- Your agreed retention figure. ISC events are personal data; do not accept the
  default without checking it against your own retention position.

### Sign in

```bash
az login --tenant <your-tenant-id>
az account set --subscription <your-subscription-id>

# Confirm you are where you think you are before deploying anything.
az account show --query "{subscription:name, id:id, tenant:tenantId}" -o json
```

Confirm Flex Consumption exists in your region before going further — the whole
hosting model depends on it:

```bash
az functionapp list-flexconsumption-locations --output table
```

If your region is absent, stop and reconsider the region rather than silently
falling back to another one: ISC event data is personal data, and where it is
processed is usually a documented part of your DPIA / data residency position.

### Resource providers

A subscription that has never deployed these resource types before will not have
the providers registered, and the failure only surfaces mid-deployment:
`Microsoft.OperationsManagement` breaks Sentinel onboarding, and
`Microsoft.AlertsManagement` breaks the alert rules. Check and register before
running `deployment group create`, not after:

```bash
for ns in Microsoft.OperationsManagement Microsoft.AlertsManagement Microsoft.SecurityInsights; do
  state=$(az provider show --namespace "$ns" --query registrationState -o tsv)
  echo "$ns: $state"
  [ "$state" != "Registered" ] && az provider register --namespace "$ns"
done
```

Registration is asynchronous and typically takes a few minutes. Poll before
proceeding:

```bash
az provider show --namespace Microsoft.OperationsManagement --query registrationState -o tsv
az provider show --namespace Microsoft.AlertsManagement --query registrationState -o tsv
```

---

## 1. Validate the template

Never skip this. Nothing here mutates Azure.

```bash
az bicep build --file infra/main.bicep --stdout > /dev/null   # compiles
az bicep lint  --file infra/main.bicep                        # strict rules
python -m pytest                                              # collector + rotator logic
```

---

## 2. Preview the deployment

Copy the example parameters and fill in the placeholders:

```bash
cp infra/main.bicepparam infra/prd.bicepparam
# edit at minimum: environment, iscBaseUrl, alertEmailAddress
```

`.gitignore` keeps every parameter file except `main.bicepparam` out of git, so
your real tenant values stay local. Nothing in a parameter file is ever a
secret — there is deliberately no parameter through which the ISC credential
could be passed — but the ISC hostname and alert addresses identify your
organisation, so they are treated as yours to publish or not.

### Choose your workspace layout

Three decisions, all in the parameter file. The defaults create everything, which
suits a greenfield subscription; most established Sentinel users will change the
first one.

| Parameter | Default | Set it when |
|---|---|---|
| `useExistingWorkspace` | `false` | You already run Sentinel and want ISC events in that workspace. Also set `existingWorkspaceName`, and `existingWorkspaceResourceGroup` if it lives in a different resource group. |
| `createOpsWorkspace` | `true` | Leave `true` to keep the pipeline's own logs out of your Sentinel workspace. Set `false` to skip the second workspace and send function telemetry to the event workspace instead — cheaper and simpler, but pipeline noise then shares Sentinel's billing rate, retention and hunting surface. |
| `existingOpsWorkspaceName` | *(unset)* | You already run a central platform-logs workspace and want the pipeline's telemetry there. Takes precedence over `createOpsWorkspace` — nothing new is created. Add `existingOpsWorkspaceResourceGroup` if it is in a different resource group. |
| `enableSentinel` | `true` | Leave `true` in nearly all cases; onboarding is idempotent, so it is safe on an already-onboarded workspace. Set `false` only if you want the events in plain Log Analytics with no Sentinel. |

Two workspaces are in play and they are chosen independently: the **event**
workspace holding `SailPointISC_CL`, and the **ops** workspace holding the
pipeline's own telemetry. Either can be created or reused:

| | Create new | Reuse existing | Collapse into the other |
|---|---|---|---|
| **Event workspace** | default | `useExistingWorkspace` | n/a |
| **Ops workspace** | default | `existingOpsWorkspaceName` | `createOpsWorkspace = false` |

Reusing an existing workspace requires it to be in the **same subscription** as
this deployment. Cross-subscription needs the table and Sentinel onboarding
deployed separately at that subscription's scope.

### Naming

Every resource name is derived from `workloadName` and `environment` — for
example `func-iscsiem-prd-collector`. Storage and Key Vault additionally get a
hash of the resource group id appended, because their names must be globally
unique.

If you have a naming standard to meet, each can be set individually. All are
optional; leaving one unset keeps the derived name.

| Parameter | Derived default | Notes |
|---|---|---|
| `workspaceNameOverride` | `log-<workload>-<env>` | Ignored when `useExistingWorkspace` is true |
| `opsWorkspaceNameOverride` | `log-<workload>-<env>-ops` | Ignored unless this deployment creates the ops workspace |
| `appInsightsNameOverride` | `appi-<workload>-<env>` | |
| `keyVaultNameOverride` | `kv-<workload><env><hash>` | Globally unique, ≤24 chars. Overriding makes uniqueness your problem |
| `storageAccountNameOverride` | `st<workload><env><hash>` | Globally unique, ≤24 chars, lowercase alphanumeric only |
| `dceNameOverride` | `dce-<workload>-<env>` | |
| `dcrNameOverride` | `dcr-<workload>-<env>` | |
| `collectorAppNameOverride` | `func-<workload>-<env>-collector` | Its hosting plan becomes `<name>-plan` |
| `rotatorAppNameOverride` | `func-<workload>-<env>-rotator` | Its hosting plan becomes `<name>-plan` |
| `actionGroupNameOverride` | `ag-<workload>-<env>` | Only deployed when `alertEmailAddress` is set |
| `eventTableName` | `SailPointISC_CL` | Must end in `_CL`. The DCR stream name derives from it |
| `collectorSecretNameOverride` | `isc-api-credential-collector` | **Changing this changes the bootstrap commands in step 4b** |
| `rotatorSecretNameOverride` | `isc-api-credential-rotator` | **Changing this changes the bootstrap commands in step 4b** |

Two of these have consequences beyond cosmetics:

- **`eventTableName`** changed after first deployment does not rename the
  existing table. The old one keeps its data and stops receiving events; the new
  one starts empty. Detections written against the old name go quiet.
- **The secret name overrides** are read by the function apps at runtime *and*
  written by hand during bootstrap. Step 4b writes to
  `isc-api-credential-collector` and `isc-api-credential-rotator` by name — if
  you override them, use your names in those commands, or the apps will start
  and find no credential.

### Preview

```bash
az group create --name "$RG" --location "$LOCATION"   # first time only

az deployment group what-if \
  --resource-group "$RG" \
  --template-file infra/main.bicep \
  --parameters infra/prd.bicepparam
```

**Read the output properly.** On a first deployment expect ~25 resources
created (fewer if you reuse a workspace or skip the ops one) and nothing
modified or deleted. Anything under `~ Modify` or `- Delete` on a *first* run
means something already exists that the template expects to own — investigate
before proceeding.

On re-runs, `~ Modify` entries against the function apps are largely
`what-if` noise: it cannot resolve `reference()` expressions ahead of
deployment, so it reports unchanged properties as changing. Read the specific
property deltas rather than the summary counts.

Known things Azure Policy may block, all of which surface here rather than
mid-deployment:

- `publicNetworkAccess: Enabled` on Storage or Key Vault. This template has no
  private-networking option to fall back on — see "Network posture" below — so a
  tenant that denies public access needs private endpoints added before this
  will deploy.
- Missing mandatory tags — extend the `tags` parameter.

> ### ⚠️ Key Vault purge protection makes this hard to un-deploy
>
> The vault is created with soft-delete **and purge protection**, which by
> design cannot be disabled or bypassed. If you delete the resource group and
> redeploy with the same `workloadName`, `environment` and resource group name,
> the vault name is regenerated identically, collides with the still
> soft-deleted vault, and **the deployment fails** for up to 90 days.
>
> This matters most for anyone evaluating the template — deploy, tear down,
> redeploy is exactly the natural thing to do. Options if you hit it:
>
> - Redeploy with a different `environment` or `workloadName` (changes the
>   derived name), or set `keyVaultNameOverride` to something new.
> - Recover the soft-deleted vault instead of creating a new one:
>   `az keyvault recover --name <vault> --location <region>`, then redeploy.
> - List what is pending deletion: `az keyvault list-deleted -o table`.
>
> Purge protection is deliberate: it is what stops an attacker who reaches the
> subscription from destroying the credential store and the audit trail with
> it. The redeploy friction is the cost of that property, not an oversight.

---

## 3. Deploy

Gated on human approval. Run it yourself; do not automate this step.

`$DEPLOY` is used only within this step. Later steps (4–8) re-derive what they
need — vault name, function app names — from `$RG` instead, so they work in a
fresh shell without carrying `$DEPLOY` forward:

```bash
RG=rg-iscsiem-prd
DEPLOY="iscsiem-$(date +%Y%m%d-%H%M)"

az deployment group create \
  --resource-group "$RG" \
  --name "$DEPLOY" \
  --template-file infra/main.bicep \
  --parameters infra/prd.bicepparam
```

Confirm the outputs landed:

```bash
az deployment group show --resource-group "$RG" --name "$DEPLOY" \
  --query properties.outputs --output json
```

> **Placeholders in this runbook use `<angle-brackets>`.** Replace the whole
> token, brackets included, before running — a literal `<name>` left in a
> command makes zsh try to read from a file called `name` (`<` is input
> redirection) and fails confusingly. Anything already written as `"$VAR"` is a
> real shell variable, not a placeholder; leave it as-is.

---

## 4. Bootstrap the ISC credentials (one time only)

This is the only moment a human handles a secret, and the only reason the
temporary Secrets Officer grant exists.

> **The secret never enters the template, a parameter file, git, or deployment
> history.** There is deliberately no `@secure()` parameter for it. Deployment
> history is readable by anyone with Reader on the resource group and is
> retained long after the credential is gone.

**Two PATs are seeded here, one per app.** The collector and rotator hold
*separate* ISC credentials (`isc-api-credential-collector` /
`isc-api-credential-rotator`), each scoped to only what that app needs. Both
are created under the **same** service identity in the same login session, and
both are seeded here so the collector has a working credential from the start
rather than waiting on the first rotation.

| Key Vault secret | Scope | Used by |
|---|---|---|
| `isc-api-credential-collector` | `sp:search:read` | Collector, for `/v2025/search/events` |
| `isc-api-credential-rotator` | `sp:my-personal-access-tokens:manage` | Rotator, to mint/delete PATs (its own and the collector's) |

Both are seeded with `type: "pat"`, which marks them as human-created seeds:
the first rotation migrates *both* off these onto rotator-minted credentials
(see step 6). Keeping them separate is what makes the collector's credential
structurally incapable of managing PATs — it was never issued that scope. See
`docs/threat-model.md`, "Design decisions that follow".

This is two genuinely different pieces of work — creating the credentials on
the **ISC side**, then seeding them on the **Azure side** — done by different
tools, in this order.

### 4a. Create the ISC-side identity and two PATs

**Don't create these PATs under your own personal admin account.** A PAT
inherits the *full* access of whichever ISC identity creates it, not just the
scopes you request on it — see `docs/threat-model.md`, "The rotator's
credential inherits its ISC identity's access". Tie them to your own account
and the credentials' fate is entangled with yours — your access reviews, your
role changes, your departure — and every action they take shows up in ISC's
audit trail as you, not as the integration.

1. **Create a dedicated, non-human service identity** via ISC's local/flat-file
   source (not synced from HR/directory). It needs enough capability to search
   events *and* to manage its own personal access tokens, and no more — check
   your console for the minimum level rather than defaulting to full Org Admin,
   which just relocates the "too much privilege" problem instead of fixing it.
   - **Email**: not functionally needed (this identity never logs in day to
     day), but the field may be required by your Identity Profile's schema
     regardless. If your tenant enforces MFA via email OTP with no exemption
     path, you'll need a real, deliverable inbox for the sign-in below to
     succeed — check your MFA policy *before* deciding a placeholder address is
     fine.
2. **Set a password for it.** As a local/flat-file identity, an admin can
   typically set an initial password directly from its identity page, rather
   than relying on a self-service signup flow.
3. **Log in as it once**, in a private/incognito browser session so it doesn't
   tangle with your own logged-in session. Most ISC consoles don't expose an
   admin-facing "generate a PAT on behalf of another user" action — PATs are
   self-service by design, so this one-time login is the fallback, not a
   workaround for a missing feature.
4. Go to its own **Preferences → Personal Access Tokens** and generate **two**
   tokens, each with a single scope:
   - one scoped **`sp:search:read`** only — this becomes the collector's
     credential;
   - one scoped **`sp:my-personal-access-tokens:manage`** only — this becomes
     the rotator's credential. It never calls the search API itself; it only
     authenticates the calls that mint and delete both apps' replacement PATs.

   Record each token's client ID and secret — the secret is shown only once,
   at creation.
5. Log out. You shouldn't need to log in as this identity again — from here
   the rotator manages both PATs' lifecycles entirely via the API.

### 4b. Seed both into Key Vault

**First, set these two variables** — every block below uses them, and they're
derived from the resource group (a stable constant) rather than `$DEPLOY`
(which is gone the moment you open a new terminal). Run this in whatever shell
you're seeding from:

```bash
RG=rg-iscsiem-prd
VAULT=$(az keyvault list -g "$RG" --query "[0].name" -o tsv)
echo "Vault: $VAULT"    # sanity check — must not be empty
```

If `$VAULT` prints empty, stop — `$RG` isn't pointing at the right resource
group, and every command below will fail with `--scope can't be an empty
string` or `The HSM 'None' not found`. Those errors mean an unset variable
upstream, never a permissions problem.

Grant yourself write access to the vault, temporarily:

```bash
az role assignment create \
  --role "Key Vault Secrets Officer" \
  --assignee "$(az ad signed-in-user show --query id -o tsv)" \
  --scope "$(az keyvault show --name "$VAULT" --query id -o tsv)"
```

If you already hold Secrets Officer on this vault (re-running the runbook, say),
this is a no-op — skip it.

Seed both PATs. Read each from a prompt rather than an argument so it never
reaches your shell history. `read -p` means "read from a coprocess" in zsh,
not "show a prompt" as it does in bash — use the `?prompt` form below, which
works in both.

**Collector PAT** (the `sp:search:read` one):

```bash
read -rs "ISC_CLIENT_ID?Collector PAT client ID: ";     echo
read -rs "ISC_CLIENT_SECRET?Collector PAT client secret: "; echo

az keyvault secret set \
  --vault-name "$VAULT" \
  --name isc-api-credential-collector \
  --value "$(jq -nc \
      --arg id "$ISC_CLIENT_ID" \
      --arg secret "$ISC_CLIENT_SECRET" \
      '{type:"pat", clientId:$id, clientSecret:$secret, createdAt:(now|todate)}')" \
  --output none

unset ISC_CLIENT_ID ISC_CLIENT_SECRET
```

**Rotator PAT** (the `sp:my-personal-access-tokens:manage` one):

```bash
read -rs "ISC_CLIENT_ID?Rotator PAT client ID: ";     echo
read -rs "ISC_CLIENT_SECRET?Rotator PAT client secret: "; echo

az keyvault secret set \
  --vault-name "$VAULT" \
  --name isc-api-credential-rotator \
  --value "$(jq -nc \
      --arg id "$ISC_CLIENT_ID" \
      --arg secret "$ISC_CLIENT_SECRET" \
      '{type:"pat", clientId:$id, clientSecret:$secret, createdAt:(now|todate)}')" \
  --output none

unset ISC_CLIENT_ID ISC_CLIENT_SECRET
```

Then **remove your write access**. Only the rotator should hold Secrets Officer:

```bash
az role assignment delete \
  --role "Key Vault Secrets Officer" \
  --assignee "$(az ad signed-in-user show --query id -o tsv)" \
  --scope "$(az keyvault show --name "$VAULT" --query id -o tsv)"
```

---

## 5. Deploy the function code

Derived from the resource group, so this works in a fresh shell too:

```bash
RG=rg-iscsiem-prd
COLLECTOR=$(az functionapp list -g "$RG" \
  --query "[?ends_with(name, 'collector')].name | [0]" -o tsv)
ROTATOR=$(az functionapp list -g "$RG" \
  --query "[?ends_with(name, 'rotator')].name | [0]" -o tsv)

(cd collector      && func azure functionapp publish "$COLLECTOR" --python)
(cd rotator        && func azure functionapp publish "$ROTATOR"   --python)
```

Both apps deploy with managed identity to a storage account where
`allowSharedKeyAccess` is `false`. If publish fails with an authorisation error,
the role assignments have not propagated yet — wait five minutes and retry
rather than re-enabling shared keys.

With both PATs seeded (step 4) and the code now deployed, the collector starts
ingesting on its next timer tick (within 5 minutes). Step 6 then migrates both
apps off their human-created seed PATs; step 7 confirms the end state.

---

## 6. First rotation (migrate off the seed PATs)

Both credentials are currently the human-created seed PATs from step 4 — they
passed through a browser session and your shell. Get off them promptly rather
than leaving them live: the first rotation replaces *both* with fresh,
rotator-minted credentials that no human has ever handled. Watch it.

The rotator normally checks weekly (`rotatorSchedule`) and only replaces a
credential once it passes 30 days old (`logic.DEFAULT_MAX_AGE`) — but a seed
PAT (`type: "pat"`) is always replaced on sight, regardless of age, precisely
so this first run migrates off it immediately.

Timer-triggered functions have no `az functionapp function invoke` — that
command doesn't exist — so trigger it via the admin API directly:

```bash
RG=rg-iscsiem-prd
ROTATOR=$(az functionapp list -g "$RG" \
  --query "[?ends_with(name, 'rotator')].name | [0]" -o tsv)
MASTER_KEY=$(az functionapp keys list --name "$ROTATOR" --resource-group "$RG" \
  --query masterKey -o tsv)

curl -s -w "\nHTTP %{http_code}\n" -X POST \
  "https://$ROTATOR.azurewebsites.net/admin/functions/rotate?code=$MASTER_KEY" \
  -H "Content-Type: application/json" -d '{"input": ""}'
```

A `202` means the invocation was accepted; it does not mean it succeeded. Check
the Azure Portal's Function App → Monitor → `rotate` invocation log directly
rather than relying on KQL alone — see "Troubleshooting" below for the exact
table-naming gotcha that makes ad-hoc App Insights queries misleading if you
try that route instead.

Expected log sequence on this first run, in this order. Both credentials are
minted, verified, and persisted *before* either old seed is deleted, so a
failure partway through never strands a role without a working credential:

```
Rotating collector credential (type pat).
Minted new collector PAT <id>.
New collector credential verified.
New collector credential persisted to Key Vault.
Rotating rotator's own credential (type pat).
Minted new rotator PAT <id>.
New rotator credential verified.
New rotator credential persisted to Key Vault.
Old collector PAT <id> deleted.
Old rotator PAT <id> deleted.
```

Then confirm the collector picks up its new credential on its **next scheduled
run** without redeployment. It reads Key Vault at runtime precisely so that it
does; if it does not, something has reintroduced a Key Vault app-setting
reference, which caches for up to 24 hours.

If a delete line instead reads `Rotation succeeded but the old <role> PAT <id>
could not be deleted`, the new credential is already live and safe — just
revoke the old one manually in the ISC console.

Once the rotation has succeeded, **revoke the two original seed PATs in the
ISC console** if the automatic delete didn't (see the line above), and
consider the human-handled credentials fully retired.

---

## 7. Verify

**Collector is running:**

```bash
az functionapp log tail --name "$COLLECTOR" --resource-group "$RG"
```

Expect `Ingested N events across M page(s)` within one schedule interval
(5 minutes by default).

Historical traces live in the ops workspace (`log-<name>-<env>-ops`) by
default, or in the event workspace if you set `createOpsWorkspace = false`.
Either way, query it directly and use the **App-prefixed table names**
(`AppTraces`, not `traces`) — see "Troubleshooting" below for why:

```kusto
AppTraces
| where Message contains "Ingested"
| project TimeGenerated, Message
| order by TimeGenerated desc
```

**Data is arriving.** In the Sentinel workspace, allow 5–15 minutes for a
brand-new custom table on first ingestion:

```kusto
SailPointISC_CL
| take 10
```

**Normalisation is working** — if these are blank, the DCR stream and the
collector's column mapping have drifted apart:

```kusto
SailPointISC_CL
| where isnotempty(ActorName)
| summarize Events = count() by EventType, EventStatus
```

**The raw payload survived:**

```kusto
SailPointISC_CL
| extend Raw = parse_json(RawEvent)
| project TimeGenerated, EventName, ActorName, Raw
| take 5
```

**Checkpoint integrity.** Restart the collector and confirm the event count does
not fall — re-ingestion is acceptable, gaps are not.

---

## 8. Decommissioning

Deleting the resource group does **not** clean up the ISC side. The credentials
the rotator minted are ISC objects, not Azure ones: they stay live and valid in
your tenant, owned by the service identity, with nothing left watching them.
Work in this order, because step 1 needs data that step 2 destroys.

**1. Record the live credential IDs, then revoke them in ISC.** The client IDs
are not secrets, so reading them is safe:

```bash
RG=rg-iscsiem-prd
VAULT=$(az keyvault list -g "$RG" --query "[0].name" -o tsv)
for n in isc-api-credential-collector isc-api-credential-rotator; do
  echo "$n: $(az keyvault secret show --vault-name "$VAULT" --name "$n" \
    --query value -o tsv | jq -r .clientId)"
done
```

You need Key Vault Secrets User on the vault to run that. Revoke both PATs in
the ISC console, then delete the service identity if it exists only for this
pipeline.

**2. Delete the resource group.**

```bash
az group delete --name "$RG" --yes
```

**3. Expect the Key Vault to linger.** Purge protection keeps it soft-deleted
for 90 days and it cannot be purged early — see the callout in step 2. It costs
nothing, but the name stays reserved, so a same-name redeploy inside that window
fails. `az keyvault list-deleted -o table` shows what is pending.

**If you reused an existing workspace** (`useExistingWorkspace = true`), the
`SailPointISC_CL` table and its data survive in that workspace — they were never
part of this resource group. Delete the table separately if you want the events
gone, and check your retention obligations before you do.

---

## Rotation design — why the order is what it is

The rotator manages two credentials independently — its own
(`sp:my-personal-access-tokens:manage`) and the collector's
(`sp:search:read`) — but only ever authenticates with its own current
credential, since the collector's is never capable of creating or deleting a
PAT. For each credential that's due in a given run:

```
1. mint the NEW credential (authenticated as the rotator's OWN current one)
2. VERIFY the new one actually works
3. persist it to Key Vault
4. only then delete the OLD one for that role
```

Critically, **both credentials complete steps 1-3 before either credential's
step 4 runs.** If the collector is due and the rotator's own credential is
also due in the same cycle, both new credentials are minted, verified, and
persisted first; only then are the two old ones deleted. This is what
guarantees a failure partway through never leaves a role without a working
credential — not even the role that already succeeded, because its old
credential hasn't been touched yet.

Each step exists because of the failure it prevents:

- **Verify before persist.** ISC will happily issue a PAT that lacks the scope
  to do what it's for. Persisting an unverified credential replaces a working
  one with a broken one, and that role stops working.
- **Persist before delete.** If the process dies between them, the worst case
  is an orphaned PAT. Reversed, the worst case is no valid credential anywhere
  for that role and a manual bootstrap under pressure.
- **Both credentials' deletes wait until both new credentials are persisted.**
  A failure while rotating the *second* credential in a cycle must not risk
  the *first* one's already-deleted old credential — so nothing is deleted
  until every mint this cycle needed has succeeded.
- **A failed delete does not fail the rotation.** The new credential is already
  live; reporting failure would be inaccurate and would invite a retry that
  mints yet another PAT.
- **Any failure leaves a working credential in place, for every role.** That is
  the property to preserve if you change this code. A dead pipeline means the
  identity governance layer is unmonitored, which is exactly the state an
  attacker operating inside ISC would want.
- **A failure in one credential's rotation aborts the whole cycle.** If the
  collector's rotation fails partway, the rotator's own rotation does not run
  either, even if also due. This is deliberately simpler than making the two
  independent: both credentials remain valid, the next weekly check retries
  both, and — if you configured `alertEmailAddress` — the failure alert fires.
  The cost of the simplicity is a delayed rotation, never a broken one.

---

## Design notes and deliberate choices

**Why noisy SDK logging is suppressed in code, not `host.json`.** The Azure SDKs
log one entry per HTTP request/response and per token acquisition at INFO. On a
function making several calls per run that dominates ops workspace ingestion —
the large majority of trace volume. Both apps therefore raise
`azure.core.pipeline.policies.http_logging_policy` and `azure.identity` to
WARNING in `function_app.py`, while `host.json` stays at
`"default": "Information"`.

The obvious alternative — a `host.json` `logLevel` category filter such as
`"default": "Warning"` with `"isc": "Information"` — **does not work for Python
Functions.** Application logs and Azure SDK logs both surface under the same
Functions host logging category, so the filter cannot distinguish them: it
suppresses `Ingested N events`, `Rotating credential` and `Minted new PAT` along
with the SDK noise, removing exactly the telemetry needed to confirm a rotation
happened. Suppressing the named SDK loggers in code targets only the noisy
source.

**Why the two apps sample telemetry differently.** `collector/host.json`
enables Application Insights adaptive sampling with
`"excludedTypes": "Request;Exception"`; `rotator/host.json` disables sampling
entirely. The collector runs 288 times a day, so sampling is a sensible guard
against a busy tenant flooding the workspace — and requests and exceptions are
excluded, so invocation records and failures are never sampled away. The rotator
runs weekly and produces a handful of lines per run, all of which matter; there
is nothing to sample. The asymmetry is deliberate, not drift.

**Why two function apps.** The collector holds Key Vault Secrets *User*; only
the rotator holds Secrets *Officer*. Merging them to save a resource would give
the frequently-running, network-exposed poller the ability to mint ISC
credentials. See `docs/threat-model.md` (A3 vs A4).

**Why `allowSharedKeyAccess: false`.** A storage account key is a bearer
credential that cannot be scoped, cheaply rotated, or attributed to a principal.
Removing it eliminates the category. This is why both apps carry Storage Blob
Data Owner and Queue Data Contributor.

**Why runtime Key Vault reads.** Key Vault references in app settings cache for
up to 24 hours. After a rotation the cache would serve a deleted credential and
collection would stop silently — an availability failure in an audit trail,
which is a security failure.

**Why App Insights has local auth disabled.** It removes the instrumentation key
as another bearer credential; both apps publish telemetry with their managed
identity instead.

**Why the collector's checkpoint advances per acknowledged chunk.** Chunks are
written in ascending `(created, id)` order, and the checkpoint moves only behind
a write the ingestion API has acknowledged. A mid-run failure resumes from the
last acknowledged chunk. Re-ingestion is a nuisance; a gap is an evidential
failure nobody notices until it matters.

**Two workspaces by default.** The Sentinel workspace holds ISC event data and
nothing else; the pipeline's own telemetry — function traces, exceptions,
dependency calls — goes to a separate `-ops` workspace that is *not* onboarded
to Sentinel. Four reasons:

- Everything in a Sentinel workspace bills at Sentinel rates on top of
  ingestion. Function traces are voluminous and worthless as security data.
- Pipeline logs would inherit the event workspace's retention, which is set from
  a personal-data retention position. Operational logs have no business being
  governed by that, and padding a personal-data workspace with unrelated
  telemetry weakens the argument that its contents are all necessary.
- They would appear in the SOC's hunting and content surface, adding noise to
  the place people look during an incident.
- Access differs: whoever operates the pipeline needs to read its telemetry, and
  that should not imply access to identity governance event data.

The ops workspace has its own retention (`opsRetentionInDays`, default 30 days)
and carries `dataClassification: operational` rather than `personal-data`.

You do not have to let the template create it. `existingOpsWorkspaceName`
points Application Insights at an operational workspace you already run, which
is usually the right answer if you have a central platform-logs workspace —
the separation above is preserved without a third workspace to manage.

Setting `createOpsWorkspace = false` instead collapses the two, which is a
reasonable trade if you would rather run one workspace than two — it just moves
pipeline telemetry under the event workspace's billing, retention and access
model.

---

## Timing and schedules

Every interval in the pipeline, what it controls, and where to change it. Note
that some live in Bicep parameters and some are Python constants — changing the
latter needs a republish (`func azure functionapp publish`), not a redeploy.

**Schedules are NCRONTAB, which has six fields starting with seconds** — not the
five-field cron you may be used to. `0 */5 * * * *` is every five minutes;
the same string read as standard cron would mean something else entirely.

| Setting | Default | Where | Controls |
|---|---|---|---|
| `collectorSchedule` | `0 */5 * * * *` (every 5 min) | Bicep parameter | How often ISC is polled. The main cost/latency lever |
| `rotatorSchedule` | `0 0 3 * * 1` (03:00 Mondays) | Bicep parameter | How often rotation is *considered* — not how often it happens |
| `DEFAULT_MAX_AGE` | 30 days | `rotator/logic.py` | Credential age at which rotation actually occurs |
| `PAT_HARD_EXPIRY` | 45 days | `rotator/logic.py` | `expirationDate` stamped on each minted PAT — an ISC-side backstop |
| `DEFAULT_LOOKBACK` | 24 hours | `collector/function_app.py` | How far back a first run, or one with an unreadable checkpoint, reaches |
| `MAX_PAGES_PER_RUN` | 200 pages | `collector/function_app.py` | Caps a single run so a large backfill cannot pin the function. Hitting it is not data loss — the checkpoint simply resumes next run |
| `accessTokenValiditySeconds` | 3600 | `rotator/function_app.py` | Lifetime of access tokens ISC issues *for* a minted PAT — not the PAT's own lifetime |

Alert windows are currently fixed in `main.bicep` rather than parameterised:
the "collector silent" rule evaluates every 30 minutes over a 3-hour window,
and the function-failure rule every 15 minutes over 1 hour.

### The rotation invariant

`rotatorSchedule` only decides how often the question is asked;
`DEFAULT_MAX_AGE` decides the answer. A weekly schedule with a 30-day max age
means a credential is replaced roughly monthly, on the first Monday after it
turns 30 days old.

The gap between `DEFAULT_MAX_AGE` and `PAT_HARD_EXPIRY` is the safety margin,
and it must comfortably exceed the check interval:

```
   day 0        day 30              day 45
   |            |                   |
   mint         rotation due        ISC expires the PAT
                └── 15-day margin ──┘
                    (≥ 2 weekly checks can fail)
```

If you shorten that margin, a single missed check can let ISC's own expiry stop
collection before the rotator replaces the credential. Two unit tests enforce
the relationship — `DEFAULT_MAX_AGE < PAT_HARD_EXPIRY`, and at least seven days
between them — so tightening it without thinking will fail the build rather
than fail in production.

If you shorten `rotatorSchedule` to daily, you get faster recovery from a
failed rotation at no real cost: a check that finds nothing due exits
immediately without touching ISC.

---

## Decisions you need to make

Defaults that are deliberately conservative rather than universally right.
Review each before treating a deployment as production-ready.

- **Retention.** `eventRetentionInDays` / `eventTotalRetentionInDays` default to
  90/365. ISC events identify named individuals, so in most jurisdictions this
  figure needs to match a documented retention position (in the UK, your
  ROPA/DPIA) rather than a template default.
- **Network posture — public endpoints only.** Key Vault, Storage and the data
  collection endpoint are reachable over public endpoints, protected by Entra
  RBAC rather than network controls. On consumption-class hosting, outbound IPs
  are shared and variable, so IP-restricting them to the function is not
  meaningful either; disabled storage shared keys and least-privilege RBAC carry
  the load.

  **Private networking is not implemented.** Adding it means a VNet with a
  delegated subnet, private endpoints and private DNS zones for Key Vault,
  Storage and the DCE, and VNet integration on both Flex Consumption apps.
  Simply disabling public access without those would sever the functions from
  their own credential store, checkpoint and ingestion path. If your tenant
  requires private networking, treat that as work to do before adopting this.
- **Workspace layout.** Whether ISC events land in your existing Sentinel
  workspace or a new one, and whether pipeline telemetry gets its own workspace.
  See "Choose your workspace layout" in step 2.
- **Collection scope.** The collector currently retrieves all events the search
  API returns for the window. Narrowing the query reduces ingestion cost but
  also reduces what you can detect on later — and you cannot retrospectively
  query events you never collected.

## Constraints that look like bugs

Read this before "correcting" anything below — each of these looks like an
oversight and is deliberate. Three of the four fail *silently*: the pipeline
keeps reporting success while quietly doing the wrong thing.

- **`enableLogAccessUsingOnlyResourcePermissions` must stay `false`.** Setting
  it `true` makes custom DCR-ingested tables unqueryable even for a subscription
  Owner, because their rows carry no per-row monitored-resource association for
  that access model to check against. Ingestion still returns `204`, so the
  symptom is "the table is empty" rather than "access denied".
- **`host.json` must stay at `"default": "Information"` with no category
  override.** Narrowing it to `"Warning"` to cut Azure SDK noise also silences
  the pipeline's own status lines, because application and SDK logs share one
  host logging category in Python Functions. Suppress the named SDK loggers in
  `function_app.py` instead — see "Design notes" above.
- **The `functionFailureAlert` query says `exceptions`, not `AppExceptions`.**
  Both names are correct, for different scopes: the alert is scoped to the
  Application Insights *component*, which resolves classic schema names, while a
  direct KQL query against the underlying workspace needs the App-prefixed ones
  (`AppTraces`, `AppExceptions`, `AppRequests`). This one fails loudly —
  changing the alert to `AppExceptions` is rejected at template validation.
- **The rotator mints PATs, not OAuth API Clients.** `POST /v2025/oauth-clients`
  returns 403 for *any* non-interactive caller, whatever its scope, because
  `client_credentials`-grant tokens carry no associated user and that endpoint
  requires one. `POST /personal-access-tokens/v1` is what ISC supports for
  non-interactive credential creation. See `docs/threat-model.md`.

One more, on the ISC side rather than in this repo: **ISC scopes are `sp:`, not
`idn:`.** SailPoint's documentation uses `idn:`-prefixed examples freely, but
the scopes this pipeline needs — `sp:search:read` and
`sp:my-personal-access-tokens:manage` — are in the `sp:` namespace. There is no
`idn:search:read`. Check your tenant's own scope list rather than the docs.

---

## Troubleshooting

Ordered roughly by where in the deployment you would hit them.

### Deploying the template

| Symptom | Cause | Fix |
|---|---|---|
| `VaultAlreadyExists`, or the deployment fails on Key Vault after a previous teardown | Purge protection keeps the old vault soft-deleted for 90 days, and the derived name is regenerated identically | See the purge-protection callout in step 2 — recover the vault, or change `environment` / `keyVaultNameOverride` |
| `MissingSubscriptionRegistration` | `Microsoft.OperationsManagement` or `Microsoft.AlertsManagement` not registered on the subscription | Register them (step 0) and redeploy. Registration is asynchronous and can take several minutes |
| `Cannot change the site <app> to the App Service Plan <plan> due to hosting constraints` | Flex Consumption cannot move an existing site between plans in place, even though `what-if` predicts a safe `Modify` | Delete the site *and* its role assignments — they carry the old identity's `principalId` and cannot be updated in place — then redeploy |
| Deployment fails creating role assignments | Contributor alone is insufficient | You need User Access Administrator or Owner on the resource group (step 0) |

### Publishing the function code

| Symptom | Cause | Fix |
|---|---|---|
| `func publish` authorisation error | Role assignments have not propagated yet | Wait five minutes and retry. Do **not** re-enable storage shared keys to work around it |
| `func publish` reports success, but `az functionapp function list` shows nothing (or a stale function) | Incomplete package upload, most often after a `serverFarmId` or storage-path change forced a Flex Consumption `Recreate`. Compare the reported archive size between attempts | Republish with `--build remote --verbose`, then verify with `az functionapp function list` rather than `func`'s own summary |
| One app starts running the other app's function | Both apps' `functionAppConfig.deployment.storage.value` resolve to the same blob, so whichever publishes last overwrites the other | Each app needs its own container (`app-package-collector` / `app-package-rotator`). Compare `az resource show --query properties.functionAppConfig.deployment.storage.value` on both — they must differ |
| `required app setting X is not set` | Code published before the template created the settings | Deploy the template first, then republish |

### Collecting events

| Symptom | Cause | Fix |
|---|---|---|
| Collector returns 401 from ISC | PAT wrong, expired, revoked, or not scoped `sp:search:read` | Confirm `isc-api-credential-collector` parses as JSON with non-empty `clientId`/`clientSecret`, and that the PAT is still live in ISC |
| Collector returns 404 from ISC | Wrong host. The API lives on `<tenant>.api.identitynow.com`, not the UI host | Correct `iscBaseUrl` and redeploy |
| Ingestion returns 403 | Collector identity lacks Monitoring Metrics Publisher on the DCR | Confirm the role assignment exists at DCR scope |
| Ingestion returns `204` but `SailPointISC_CL` stays empty | Usually `enableLogAccessUsingOnlyResourcePermissions: true` on the workspace — see "Constraints that look like bugs". Allow 5–15 minutes for a brand-new custom table on first ingestion before assuming a fault | Set it `false`, and check the workspace `features` block for drift |
| Table populated, but normalised columns are blank | The DCR stream and the collector's column mapping have drifted | Compare `normalisedColumns` in `main.bicep` with `normalise_event`; `test_emitted_columns_match_the_dcr_schema_exactly` guards this |
| "Collector silent" alert fires | Genuinely no events for 3 hours, or the collector is failing | Check the function is actually running before assuming ISC is quiet |

### Rotating credentials

| Symptom | Cause | Fix |
|---|---|---|
| Rotation logs `could not be deleted` | The replacement is already live and persisted; only the revoke of the old PAT failed | Revoke the old PAT manually in ISC. The rotation itself succeeded — do not re-run it |
| Rotation returns 403 minting a PAT | The rotator's PAT is not scoped `sp:my-personal-access-tokens:manage`, or its ISC identity lacks the capability | Check the scope on the rotator's PAT. Note this is *not* `sp:oauth-client:manage` — see "Constraints that look like bugs" |

### Reading the pipeline's own telemetry

| Symptom | Cause | Fix |
|---|---|---|
| KQL against `traces` / `exceptions` fails with `Failed to resolve table or column expression` | Workspace-based App Insights stores data under App-prefixed names; classic names resolve only through the App Insights compatibility layer | Query `AppTraces` / `AppExceptions` / `AppRequests` against the workspace. Do not apply the same change to component-scoped alert rules — see "Constraints that look like bugs" |
| The function is demonstrably running, but `AppTraces` shows nothing at all — not even `isc.*` lines | A `host.json` `logLevel` category override | Restore `"default": "Information"` — see "Constraints that look like bugs" |
