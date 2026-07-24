"""Timer-triggered ISC credential rotator.

Manages TWO separate ISC credentials, independently:

  * the collector's, scoped sp:search:read only
  * the rotator's own, scoped sp:my-personal-access-tokens:manage only

Only the rotator's own credential can create or delete PATs -- the collector's
never carries that capability. This closes the gap the earlier
single-shared-credential design left open (see docs/threat-model.md,
"Residual risks"): the collector's Key Vault read access no longer implies a
credential capable of managing PATs, because that credential never has the
scope to do so. It is why the rotator authenticates with its OWN current
credential to mint BOTH replacements -- the collector's credential is never
capable of authenticating that call itself.

THE ORDERING PER CREDENTIAL IS LOAD-BEARING. For each credential rotated in a
given run:

    1. mint the NEW credential (authenticated as the rotator's OWN current
       credential)
    2. VERIFY the new credential actually works
    3. persist the new credential to Key Vault
    4. only then delete the OLD credential for that role

A failure at any step must leave a working credential in place for that role.
Moving delete earlier -- or persisting before verifying -- creates a window in
which a role has no working credential, which is precisely the condition an
attacker inside ISC would want. Both credentials' steps 1-3 complete before
either credential's step 4 runs, so a fully successful run never deletes an
old credential until every new one this run needed is safely persisted.

Bootstrap seeds only ONE PAT: the rotator's own, scoped
sp:my-personal-access-tokens:manage. There is no separate collector seed --
the rotator's first run mints the collector's very first working credential,
same as every rotation after. The collector has no credential, and will error,
until that first run completes. See infra/README.md, step 7.

This app holds Key Vault Secrets *Officer*. The collector deliberately does
not.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Callable

import azure.functions as func
import requests
from azure.core.exceptions import ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

import logic

LOGGER = logging.getLogger("isc.rotator")

# The Azure SDKs log one entry per HTTP request/response and per token
# acquisition at INFO, which dominates ops-workspace ingestion on a function
# that makes several calls per run. Raise those loggers to WARNING at the
# source, so the host can stay at INFO and our own isc.* logs plus per-run
# execution records remain visible. Doing this in code rather than host.json is
# deliberate: application and SDK logs share one Functions host logging
# category, so a host.json category filter suppresses both or neither.
for _noisy_logger in (
    "azure.core.pipeline.policies.http_logging_policy",
    "azure.identity",
):
    logging.getLogger(_noisy_logger).setLevel(logging.WARNING)

HTTP_TIMEOUT = (10, 60)

app = func.FunctionApp()


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"required app setting {name} is not set")
    return value


# ---------------------------------------------------------------------------
# ISC operations
# ---------------------------------------------------------------------------


def get_access_token(base_url: str, client_id: str, client_secret: str) -> str:
    response = requests.post(
        f"{base_url.rstrip('/')}/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=HTTP_TIMEOUT,
    )
    if response.status_code != 200:
        # Never log the body: it can echo credential material.
        raise RuntimeError(f"ISC token request failed with HTTP {response.status_code}")

    token = response.json().get("access_token")
    if not token:
        raise RuntimeError("ISC token response contained no access_token")
    return token


def _format_expiration(moment: datetime) -> str:
    utc = moment.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def create_personal_access_token(
    base_url: str, token: str, description: str, scope: list[str], now: datetime
) -> dict[str, str]:
    """Mint a new ISC Personal Access Token with the given scope.

    Originally this minted an OAuth API Client via POST /v2025/oauth-clients.
    That call 403s unconditionally: tokens issued via the client_credentials
    grant -- the only grant type available to a non-interactive caller -- have
    no associated user, and ISC's admin APIs (oauth-client management among
    them) require one. Scope cannot fix this; it is a grant-type mismatch, not
    a permissions gap. PATs, minted via POST /personal-access-tokens/v1, are
    the mechanism ISC actually supports for non-interactive credential
    creation.

    expirationDate is set to logic.PAT_HARD_EXPIRY, well beyond
    logic.DEFAULT_MAX_AGE, so our own age-based rotation is what actually
    replaces this credential in normal operation -- ISC's expiry is a backstop
    for if this function ever silently stops running, not the primary
    control.
    """
    response = requests.post(
        f"{base_url.rstrip('/')}/personal-access-tokens/v1",
        json={
            "name": description,
            "scope": scope,
            "accessTokenValiditySeconds": 3600,
            "expirationDate": _format_expiration(now + logic.PAT_HARD_EXPIRY),
        },
        headers={"Authorization": f"Bearer {token}"},
        timeout=HTTP_TIMEOUT,
    )
    if response.status_code not in (200, 201):
        raise RuntimeError(
            f"ISC PAT creation failed with HTTP {response.status_code}: "
            f"{response.text[:500]}"
        )

    body = response.json()
    client_id = body.get("id") or body.get("clientId")
    client_secret = body.get("secret") or body.get("clientSecret")
    if not client_id or not client_secret:
        raise RuntimeError("ISC PAT response contained no id/secret")

    return {"clientId": client_id, "clientSecret": client_secret}


def verify_search_credential(base_url: str, client_id: str, client_secret: str) -> None:
    """Prove the collector's new credential can both authenticate and read events.

    A token grant alone is not proof: a PAT can be issued and still lack the
    scope to query the audit trail. Verifying the actual call is what makes it
    safe to delete the old credential afterwards.
    """
    token = get_access_token(base_url, client_id, client_secret)

    response = requests.post(
        f"{base_url.rstrip('/')}/v2025/search/events",
        params={"count": "false", "limit": "1"},
        json={"indices": ["events"], "query": {"query": "*"}, "sort": ["created"]},
        headers={"Authorization": f"Bearer {token}"},
        timeout=HTTP_TIMEOUT,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"new collector credential failed verification: search returned "
            f"HTTP {response.status_code}. Old credential left in place."
        )


def verify_manage_credential(base_url: str, client_id: str, client_secret: str) -> None:
    """Prove the rotator's own new credential can authenticate.

    Weaker than verify_search_credential: this credential's whole purpose is
    minting future PATs, and there is no safe, side-effect-free way to prove
    that capability without either consuming a real rotation slot or creating
    a throwaway ISC resource. A successful token exchange is real evidence the
    minted clientId/clientSecret pair is genuinely valid and enabled -- the
    same first check every credential in this system has to pass before
    anything else about it is trusted.
    """
    get_access_token(base_url, client_id, client_secret)


def delete_personal_access_token(base_url: str, token: str, pat_id: str) -> None:
    response = requests.delete(
        f"{base_url.rstrip('/')}/personal-access-tokens/v1/{pat_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=HTTP_TIMEOUT,
    )
    if response.status_code not in (200, 204, 404):
        raise RuntimeError(
            f"failed to delete old PAT {pat_id}: HTTP {response.status_code}"
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _rotate_one(
    *,
    vault: SecretClient,
    base_url: str,
    old_rotator_token: str,
    secret_name: str,
    scope: list[str],
    description: str,
    now: datetime,
    verify: Callable[[str, str, str], None],
    role: str,
) -> None:
    """Mint, verify, and persist a replacement credential for one role.

    Deliberately does not delete the old one -- the caller does that only
    after every credential this run needed has been minted, verified, and
    persisted, so a later failure for a *different* role can never leave
    *this* role without a working credential.
    """
    new_credential = create_personal_access_token(
        base_url, old_rotator_token, description, scope, now
    )
    LOGGER.info("Minted new %s PAT %s.", role, new_credential["clientId"])

    verify(base_url, new_credential["clientId"], new_credential["clientSecret"])
    LOGGER.info("New %s credential verified.", role)

    vault.set_secret(
        secret_name,
        logic.build_secret_payload(
            new_credential["clientId"],
            new_credential["clientSecret"],
            credential_type=logic.CREDENTIAL_TYPE_ROTATED_PAT,
            created_at=now,
        ),
    )
    LOGGER.info("New %s credential persisted to Key Vault.", role)


@app.timer_trigger(
    schedule="%ROTATOR_SCHEDULE%",
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
def rotate(timer: func.TimerRequest) -> None:
    base_url = _require("ISC_BASE_URL")
    vault_uri = _require("KEY_VAULT_URI")
    collector_secret_name = _require("COLLECTOR_CREDENTIAL_SECRET_NAME")
    rotator_secret_name = _require("ROTATOR_CREDENTIAL_SECRET_NAME")

    azure_credential = DefaultAzureCredential()
    now = datetime.now(timezone.utc)

    with SecretClient(vault_url=vault_uri, credential=azure_credential) as vault:
        # The rotator's own secret is the one thing bootstrapped by hand; if
        # it is missing, that is a genuine setup error, not a state to guess
        # past -- parse_secret_payload(None) raises, which is correct here.
        rotator_current = logic.parse_secret_payload(
            vault.get_secret(rotator_secret_name).value
        )

        # The collector's secret does not exist until this function's first
        # successful run creates it. Missing is expected on that first run,
        # not an error -- there is simply no prior collector credential to
        # weigh an age against yet.
        try:
            collector_current = logic.parse_secret_payload(
                vault.get_secret(collector_secret_name).value
            )
            collector_due = logic.should_rotate(collector_current, now)
        except ResourceNotFoundError:
            LOGGER.info("No collector credential exists yet; this run will create it.")
            collector_current = None
            collector_due = True

        rotator_due = logic.should_rotate(rotator_current, now)

        if not collector_due and not rotator_due:
            LOGGER.info("Both credentials are within their maximum age; nothing to do.")
            return

        # Only the rotator's OWN current credential can authenticate a PAT
        # creation or deletion call -- the collector's never carries
        # sp:my-personal-access-tokens:manage. This is why both replacements,
        # even the collector's, are minted using this one token.
        old_rotator_token = get_access_token(
            base_url, rotator_current["clientId"], rotator_current["clientSecret"]
        )

        if collector_due:
            LOGGER.info(
                "Rotating collector credential (type %s).",
                collector_current["type"] if collector_current else "none",
            )
            _rotate_one(
                vault=vault,
                base_url=base_url,
                old_rotator_token=old_rotator_token,
                secret_name=collector_secret_name,
                scope=["sp:search:read"],
                description=f"sentinel-collector-{now.strftime('%Y%m%dT%H%M%S')}",
                now=now,
                verify=verify_search_credential,
                role="collector",
            )

        if rotator_due:
            LOGGER.info("Rotating rotator's own credential (type %s).", rotator_current["type"])
            _rotate_one(
                vault=vault,
                base_url=base_url,
                old_rotator_token=old_rotator_token,
                secret_name=rotator_secret_name,
                scope=["sp:my-personal-access-tokens:manage"],
                description=f"sentinel-rotator-{now.strftime('%Y%m%dT%H%M%S')}",
                now=now,
                verify=verify_manage_credential,
                role="rotator",
            )

        # ---- ONLY NOW delete old credentials -----------------------------
        # Every new credential this run needed is already persisted, for
        # every role. A failure here is recoverable and must not fail the
        # rotation: leaving an orphaned PAT is a smaller problem than
        # reporting a successful rotation that did not happen. Both deletes
        # use old_rotator_token, still valid for the rest of this invocation
        # regardless of what has since been minted or persisted.
        if collector_due and collector_current is not None:
            old_id = collector_current["clientId"]
            try:
                delete_personal_access_token(base_url, old_rotator_token, old_id)
                LOGGER.info("Old collector PAT %s deleted.", old_id)
            except Exception:
                LOGGER.exception(
                    "Rotation succeeded but the old collector PAT %s could not "
                    "be deleted. Revoke it manually in ISC.",
                    old_id,
                )

        if rotator_due:
            old_id = rotator_current["clientId"]
            try:
                delete_personal_access_token(base_url, old_rotator_token, old_id)
                LOGGER.info("Old rotator PAT %s deleted.", old_id)
            except Exception:
                LOGGER.exception(
                    "Rotation succeeded but the old rotator PAT %s could not "
                    "be deleted. Revoke it manually in ISC.",
                    old_id,
                )
