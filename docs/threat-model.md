# Threat model — SailPoint ISC → Sentinel collector

## Why ISC is Tier 0

SailPoint Identity Security Cloud decides who has access to what across the
estate. An actor who controls ISC does not need to exploit anything downstream:
they request access, approve it themselves, and the audit trail records a
legitimate, approved grant. Detection therefore has to happen *in the
governance layer*, because by the time the effect is visible in a downstream
system it looks authorised.

This makes two things true:

1. ISC audit events are high-value SOC telemetry.
2. This pipeline is itself Tier 0 infrastructure. Its credential can read the
   audit trail; its rotator can mint new ISC credentials. Compromise of the
   pipeline is compromise of the evidence.

## Assets

| Asset | Sensitivity | Where it lives |
|---|---|---|
| ISC API credentials (two scoped PATs) | Critical | Key Vault secrets, rotator-owned |
| ISC audit events | Personal data (GDPR / UK GDPR and equivalents) | Sentinel custom table `SailPointISC_CL` |
| Collector checkpoint | Integrity-critical | Blob in the function's storage account |
| Ingestion path (DCE/DCR) | Integrity-critical | Azure Monitor |

## Adversaries we care about

**A1 — External attacker with stolen ISC credentials.** Uses ISC to grant
themselves standing access. Detected by the events collected below.

**A2 — Malicious or compromised insider with ISC admin.** Same capability,
but legitimate-looking. Relies on the audit trail being complete and tamper-
evident — which is why checkpoint integrity matters as much as collection.

**A3 — Attacker who reaches the collector's Azure identity.** Can read the
collector's ISC credential from Key Vault and read the event stream. Cannot mint
ISC credentials — that credential is scoped `sp:search:read` and has no
token-management rights, and the identity holds only Key Vault Secrets *User*,
so it can neither use nor reach the rotator's credential. Cannot rewrite
already-ingested logs.

**A4 — Attacker who reaches the rotator's identity.** Can mint ISC credentials
and write to Key Vault. This is the worst case in the Azure half of the system,
and the reason the rotator is a separate app with a separate identity and a
minimal deployment surface.

## Design decisions that follow

- **Split identities.** Collector holds Key Vault Secrets *User*; only the
  rotator holds Secrets *Officer*. Merging the two apps to save a resource
  would hand A3 the capabilities of A4.
- **Two ISC credentials, one per app.** The collector's PAT is scoped
  `sp:search:read`; the rotator's is scoped
  `sp:my-personal-access-tokens:manage`. The rotator authenticates as itself to
  mint and revoke PATs for *both* roles, so the collector's credential is
  structurally incapable of creating or deleting a token — not by policy, but
  because it was never issued the scope. Without this split, A3's Key Vault
  read access would yield a credential that could mint further ISC credentials,
  which is A4's capability.
- **Rotation mints PATs, not OAuth API Clients.** Tokens issued under the
  `client_credentials` grant — the only grant available to a non-interactive
  scheduled function — carry no associated user, and ISC's admin APIs
  (OAuth-client management among them) require one. `POST /v2025/oauth-clients`
  therefore returns 403 for any caller of this kind regardless of scope; it is a
  grant-type constraint, not a permissions shortfall.
  `POST /personal-access-tokens/v1` is the endpoint ISC supports for
  non-interactive credential creation.
- **No shared key on storage** (`allowSharedKeyAccess: false`). A storage
  account key is a bearer credential that cannot be scoped, rotated cheaply, or
  attributed to a principal. Removing it eliminates the class.
- **No secret in the template, params, git, or deployment history.** Deployment
  history is readable by anyone with reader on the resource group and is
  retained beyond the life of the secret. The ISC credential is seeded once by
  CLI and thereafter owned by the rotator.
- **Pipeline telemetry is kept out of the Sentinel workspace by default.** The
  functions' own logs go to a separate operational workspace that is not
  onboarded to Sentinel. Reading pipeline telemetry should not imply access to
  identity governance event data, and the personal-data retention position on
  the Sentinel workspace stays defensible if nothing but ISC events lands there.
  Setting `createOpsWorkspace = false` collapses the two, trading that
  separation for one less workspace — a reasonable choice, but it puts
  operational logs under the event workspace's access model and retention.
- **Runtime Key Vault reads, not app-setting references.** Key Vault references
  in app settings cache for up to 24 hours. After a rotation that cache serves a
  deleted credential and collection stops silently — an availability failure in
  the audit trail, which is a security failure.
- **Rotation ordering is load-bearing.** Authenticate with old → mint new →
  verify new works → persist to Key Vault → only then delete old. Any failure
  must leave a working credential in place. Reordering so the delete precedes
  verification creates a window where the pipeline is dead and no one is
  watching the governance layer.
- **Never advance the checkpoint past a failed write.** Re-ingesting a
  duplicate event is a nuisance; losing one is an evidential gap that will not
  be noticed until it is needed. No code path may drop events to catch up.

## What we collect and why

| Signal | Detects |
|---|---|
| Admin capability grants | Privilege escalation inside ISC itself (A1, A2) |
| API client / PAT creation | Persistence — a second credential that outlives the intrusion |
| Source delete-threshold changes | Preparation for mass deprovisioning or audit-trail damage |
| Leaver deprovisioning failures | Standing access that should have been revoked |
| Authentication events | Credential stuffing, impossible travel, MFA anomalies |
| Role / access profile changes | Broadening entitlements ahead of a request |

## Residual risks

- **Privilege is concentrated in the rotator's credential.** Splitting the two
  ISC credentials removes the collector's ability to manage tokens, but it does
  not remove that ability from the system — it consolidates it. Anyone who
  obtains the rotator's PAT can mint further ISC credentials for the identity it
  belongs to. This is mitigated by holding it behind the more restrictive Azure
  identity (Secrets *Officer*, one low-frequency app, minimal surface) rather
  than eliminated, because a credential that cannot mint its own replacement
  cannot self-rotate.
- **The rotator's credential inherits its ISC identity's access.** A PAT carries
  the full access of the identity that created it, not merely the scopes
  requested on it. Creating the seed PATs under a dedicated, non-human service
  identity with the minimum admin capability — rather than a named
  administrator's account — is what bounds this. Doing it under a personal
  admin account silently grants the pipeline that person's entire authority and
  ties its lifecycle to their employment.
- **Seed credentials pass through human hands exactly once.** The two seed PATs
  are created in a browser and pasted into a shell during bootstrap. They are
  the only credentials in the system's life that a human ever sees. The first
  rotation replaces both with rotator-minted credentials no human has handled,
  which is why it is a bootstrap step rather than an optional tidy-up.
- **ISC-side expiry is a backstop, not the control.** Minted PATs carry a
  45-day `expirationDate` (`PAT_HARD_EXPIRY` in `rotator/logic.py`), set
  deliberately beyond the 30-day `DEFAULT_MAX_AGE` at which the rotator replaces
  them. If the rotator stops running, ISC expiry eventually halts collection
  rather than leaving a credential valid indefinitely. Whether that is *noticed*
  depends on alerting: the "collector silent" rule is only deployed when
  `alertEmailAddress` is set, and it is empty in the shipped example. Without
  it, an expired credential stops the audit trail with nothing raising a hand.
  If `DEFAULT_MAX_AGE` and `PAT_HARD_EXPIRY` are ever brought close together, a
  single missed weekly check becomes an outage.
- **Everything is reached over public endpoints.** Key Vault, Storage and the
  data collection endpoint accept traffic from the internet, gated by Entra
  RBAC rather than by network position. On consumption-class hosting outbound
  IPs are shared and variable, so IP allow-listing would not meaningfully
  narrow this either. Disabled storage shared keys and least-privilege RBAC are
  what actually bound it. Private networking — VNet integration plus private
  endpoints for all three — is not implemented; adopters who need it should
  plan that work before relying on this pipeline.
- **Personal data.** ISC events identify named individuals, so this pipeline
  moves personal data into Sentinel by design. Both the retention period and the
  lawful basis for ingesting it need to be covered by whatever record your
  jurisdiction requires — in the UK, the ROPA and DPIA. Retention is a template
  parameter (`eventRetentionInDays` / `eventTotalRetentionInDays`) precisely so
  it can be set to an agreed figure rather than inherited from a default.
