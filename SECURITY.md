# Security policy

## Reporting a vulnerability

**Please do not open a public issue for a security vulnerability.**

Use GitHub's private vulnerability reporting on this repository:
**Security → Report a vulnerability**. That opens a private advisory visible
only to the maintainers, and lets a fix be prepared before anything is
disclosed.

Useful things to include: what an attacker could achieve, which component is
affected (`collector`, `rotator`, `infra/main.bicep`), and whether it is
reachable in a default deployment or only under a particular parameter
combination.

## Scope

In scope:

- Anything that exposes an ISC credential, or lets one be read by a principal
  that should not have it.
- Anything that lets the collector's search-scoped credential mint, modify or
  revoke tokens.
- Anything that causes silent loss of audit events — a gap in an audit trail
  that no alert fires on is a security failure, not just an availability one.
- Privilege escalation through the deployed Azure resources or their RBAC.
- A rotation path that can leave the pipeline with no working credential.

Out of scope:

- Vulnerabilities in SailPoint ISC, Microsoft Sentinel, or the Azure platform
  itself — report those to the respective vendor.
- Cost or volume of ingested data.
- Deployments that have deliberately weakened the defaults (for example
  granting the collector's identity Key Vault Secrets *Officer*, or reusing one
  ISC credential for both apps). Please do say so if a default makes such a
  weakening easy to do by accident.

## Security model

The assumptions this project is built on, and the reasoning behind each
trade-off, are in [`docs/threat-model.md`](docs/threat-model.md). Read it before
concluding that something is a bug — several apparently redundant choices are
deliberate, and some known residual risks are documented rather than fixed.

Two properties are worth stating plainly, because a change that breaks either
is a vulnerability even if everything still appears to work:

1. **No ISC credential ever enters the template, a parameter file, git, or Azure
   deployment history.** Deployment history outlives the credential and is
   readable by anyone with Reader on the resource group.
2. **The ingestion checkpoint never advances past data that failed to ingest.**
   Re-ingesting duplicate events is acceptable; a silent gap is not.

## Supported versions

This is a reference implementation maintained on a best-effort basis. Fixes are
applied to `main`; there are no long-lived release branches or backports.
