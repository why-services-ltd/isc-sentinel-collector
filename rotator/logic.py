"""Pure logic for the ISC credential rotator.

Side-effect free and unit tested. The ordered, irreversible steps live in
``function_app.py``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

# Our own rotation cadence: a credential older than this is due for
# replacement. Deliberately well inside PAT_HARD_EXPIRY, so our own logic is
# what normally replaces a credential -- ISC's own expiry is a backstop for if
# the rotator ever silently stops running, not the primary control.
DEFAULT_MAX_AGE = timedelta(days=30)

# ISC-side expirationDate set on every PAT the rotator mints. Must stay
# comfortably longer than DEFAULT_MAX_AGE: the rotator checks on
# ROTATOR_SCHEDULE (weekly), so a single missed check still leaves several
# weeks of margin before ISC's own expiry would be the thing that actually
# bites.
PAT_HARD_EXPIRY = timedelta(days=45)

CREDENTIAL_TYPE_PAT = "pat"
CREDENTIAL_TYPE_ROTATED_PAT = "rotated-pat"


def build_secret_payload(
    client_id: str,
    client_secret: str,
    credential_type: str = CREDENTIAL_TYPE_ROTATED_PAT,
    created_at: datetime | None = None,
) -> str:
    """Serialise a credential for storage in Key Vault.

    ``type`` distinguishes the original, human-created seed PAT
    (``CREDENTIAL_TYPE_PAT``) from everything the rotator has since minted
    (``CREDENTIAL_TYPE_ROTATED_PAT``) -- both are ISC Personal Access Tokens
    and authenticate identically, but only the former unconditionally rotates
    on sight. See ``should_rotate``.
    """
    if not client_id or not client_secret:
        raise ValueError("client_id and client_secret are both required")

    moment = created_at or datetime.now(timezone.utc)
    return json.dumps(
        {
            "type": credential_type,
            "clientId": client_id,
            "clientSecret": client_secret,
            "createdAt": moment.astimezone(timezone.utc).isoformat(),
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def parse_secret_payload(raw: str | bytes | None) -> dict[str, Any]:
    """Deserialise a stored credential.

    Raises rather than returning a default: a rotator that cannot read the
    current credential must stop, not guess. Guessing here risks deleting a
    working credential it never actually understood.
    """
    if raw is None:
        raise ValueError("no credential is stored in Key Vault")

    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")

    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise ValueError("stored credential is not valid JSON") from exc

    if not isinstance(parsed, dict):
        raise ValueError("stored credential is not a JSON object")

    client_id = parsed.get("clientId")
    client_secret = parsed.get("clientSecret")
    if not client_id or not client_secret:
        raise ValueError("stored credential is missing clientId/clientSecret")

    return {
        "type": parsed.get("type", CREDENTIAL_TYPE_PAT),
        "clientId": client_id,
        "clientSecret": client_secret,
        "createdAt": parsed.get("createdAt"),
    }


def credential_age(payload: dict[str, Any], now: datetime) -> timedelta | None:
    """Age of the stored credential, or None if it has no usable timestamp."""
    created_at = payload.get("createdAt")
    if not isinstance(created_at, str) or not created_at:
        return None

    text = created_at.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"

    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    return now.astimezone(timezone.utc) - moment.astimezone(timezone.utc)


def should_rotate(
    payload: dict[str, Any], now: datetime, max_age: timedelta = DEFAULT_MAX_AGE
) -> bool:
    """Decide whether the stored credential is due for replacement.

    The original seed PAT (``CREDENTIAL_TYPE_PAT``) always rotates: migrating
    off it is the point of the rotator's first run, since it inherits the full
    access of the human who created it. A credential the rotator itself minted
    (``CREDENTIAL_TYPE_ROTATED_PAT``) is subject to normal age-based rotation
    instead -- it is still a PAT, but not the one that needs escaping on sight.

    An unknown age also rotates. The alternative -- assuming it is fresh -- risks
    letting a credential silently reach expiry, which stops collection.
    """
    if payload.get("type") == CREDENTIAL_TYPE_PAT:
        return True

    age = credential_age(payload, now)
    if age is None:
        return True

    return age >= max_age
