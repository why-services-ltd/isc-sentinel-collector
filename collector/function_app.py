"""Timer-triggered collector: SailPoint ISC audit events -> Sentinel.

All decision-making lives in ``logic.py`` and is unit tested. This module is
deliberately thin: credentials, HTTP, blob state, and ingestion.

The invariant that governs the whole flow: **the checkpoint never advances past
data that failed to ingest.** Chunks are written in ascending (created, id)
order and the checkpoint moves only behind an acknowledged write. A failure
persists progress up to the last acknowledged chunk and re-raises, so the next
run resumes from there. Re-fetching is acceptable; silent loss is not.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import azure.functions as func
import requests
from azure.core.exceptions import ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient
from azure.monitor.ingestion import LogsIngestionClient
from azure.storage.blob import BlobServiceClient

# The deployment package root is this directory, so logic.py is a top-level
# module at runtime. The test suite imports it as ``collector.logic`` from the
# repository root instead.
import logic

LOGGER = logging.getLogger("isc.collector")

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

CHECKPOINT_BLOB_NAME = "checkpoint.json"

# How far back a first run (or an unreadable checkpoint) reaches. Generous on
# purpose: re-ingesting a day costs money, missing an hour costs evidence.
DEFAULT_LOOKBACK = timedelta(hours=24)

# Guard against an unbounded backfill pinning the function on a cold tenant.
# Hitting this is not data loss -- the checkpoint simply resumes next run.
MAX_PAGES_PER_RUN = 200

HTTP_TIMEOUT = (10, 60)  # (connect, read) seconds

app = func.FunctionApp()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"required app setting {name} is not set")
    return value


# ---------------------------------------------------------------------------
# ISC credential and authentication
# ---------------------------------------------------------------------------


def read_isc_credential(credential) -> dict[str, str]:
    """Fetch the ISC credential from Key Vault at runtime.

    Deliberately *not* a Key Vault app-setting reference: those cache for up to
    24 hours, so the first collector run after a rotation would authenticate with
    a credential the rotator has already deleted, and collection would stop
    silently until the cache expired.
    """
    vault_uri = _require("KEY_VAULT_URI")
    secret_name = _require("CREDENTIAL_SECRET_NAME")

    with SecretClient(vault_url=vault_uri, credential=credential) as client:
        secret = client.get_secret(secret_name)

    try:
        payload = json.loads(secret.value)
    except ValueError as exc:
        raise RuntimeError(
            f"secret {secret_name} is not valid JSON; expected "
            '{"clientId": ..., "clientSecret": ...}'
        ) from exc

    client_id = payload.get("clientId")
    client_secret = payload.get("clientSecret")
    if not client_id or not client_secret:
        raise RuntimeError(f"secret {secret_name} is missing clientId/clientSecret")

    return {"clientId": client_id, "clientSecret": client_secret}


def get_access_token(base_url: str, isc_credential: dict[str, str]) -> str:
    """Exchange the ISC client credential for a bearer token.

    ISC personal access tokens and OAuth clients both use the client_credentials
    grant, so this is unchanged by the PAT-to-OAuth migration.
    """
    response = requests.post(
        f"{base_url.rstrip('/')}/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": isc_credential["clientId"],
            "client_secret": isc_credential["clientSecret"],
        },
        timeout=HTTP_TIMEOUT,
    )
    if response.status_code != 200:
        # Never log the body: it echoes credential material on some errors.
        raise RuntimeError(f"ISC token request failed with HTTP {response.status_code}")

    token = response.json().get("access_token")
    if not token:
        raise RuntimeError("ISC token response contained no access_token")
    return token


# ---------------------------------------------------------------------------
# Checkpoint persistence
# ---------------------------------------------------------------------------


def _checkpoint_blob(credential):
    account = _require("STATE_STORAGE_ACCOUNT")
    container = _require("STATE_CONTAINER_NAME")
    service = BlobServiceClient(
        account_url=f"https://{account}.blob.core.windows.net",
        credential=credential,
    )
    return service.get_blob_client(container=container, blob=CHECKPOINT_BLOB_NAME)


def load_checkpoint(credential, now: datetime) -> dict:
    blob = _checkpoint_blob(credential)
    try:
        stored = blob.download_blob().readall()
    except ResourceNotFoundError:
        LOGGER.info("No checkpoint found; starting %s back.", DEFAULT_LOOKBACK)
        stored = None
    return logic.load_checkpoint(stored, now, DEFAULT_LOOKBACK)


def save_checkpoint(credential, checkpoint: dict) -> None:
    blob = _checkpoint_blob(credential)
    blob.upload_blob(
        json.dumps(checkpoint, separators=(",", ":")).encode("utf-8"),
        overwrite=True,
    )


# ---------------------------------------------------------------------------
# ISC search
# ---------------------------------------------------------------------------


def search_page(
    base_url: str, token: str, body: dict, page_size: int = 250
) -> list[dict]:
    response = requests.post(
        f"{base_url.rstrip('/')}/v2025/search/events",
        params={"count": "false", "limit": str(page_size)},
        json=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=HTTP_TIMEOUT,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"ISC search failed with HTTP {response.status_code}: {response.text[:500]}"
        )

    events = response.json()
    if not isinstance(events, list):
        raise RuntimeError("ISC search returned an unexpected payload shape")
    return events


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


@app.timer_trigger(
    schedule="%COLLECTOR_SCHEDULE%",
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
def collect_isc_events(timer: func.TimerRequest) -> None:
    if timer.past_due:
        LOGGER.warning("Collector timer is past due; running now.")

    base_url = _require("ISC_BASE_URL")
    stream_name = _require("DCR_STREAM_NAME")
    dce_endpoint = _require("DCE_ENDPOINT")
    dcr_immutable_id = _require("DCR_IMMUTABLE_ID")

    credential = DefaultAzureCredential()
    now = datetime.now(timezone.utc)

    isc_credential = read_isc_credential(credential)
    token = get_access_token(base_url, isc_credential)

    checkpoint = load_checkpoint(credential, now)
    LOGGER.info("Starting from checkpoint %s", checkpoint["lastCreated"])

    ingestion = LogsIngestionClient(endpoint=dce_endpoint, credential=credential)

    search_after = None
    pages = 0
    ingested = 0
    persisted = dict(checkpoint)

    try:
        while pages < MAX_PAGES_PER_RUN:
            body = logic.build_search_body(checkpoint, search_after)
            events = search_page(base_url, token, body)
            pages += 1

            if not events:
                break

            cursor = logic.next_search_after(events)

            # Boundary de-duplication applies only to the first page; later pages
            # are already past the checkpoint by construction.
            candidates = (
                logic.filter_already_seen(events, checkpoint)
                if search_after is None
                else events
            )

            if candidates:
                records = [logic.normalise_event(item) for item in candidates]

                # chunk_records preserves order, so records[i] corresponds to
                # candidates[i] and a running offset maps an acknowledged chunk
                # back to the raw events it came from.
                offset = 0
                for chunk in logic.chunk_records(records):
                    ingestion.upload(
                        rule_id=dcr_immutable_id,
                        stream_name=stream_name,
                        logs=chunk,
                    )
                    # Only now is it safe to move the checkpoint.
                    acknowledged = candidates[offset : offset + len(chunk)]
                    offset += len(chunk)
                    persisted = logic.advance_checkpoint(persisted, acknowledged)
                    save_checkpoint(credential, persisted)
                    ingested += len(chunk)

            if cursor is None:
                break
            search_after = cursor

        else:
            LOGGER.warning(
                "Stopped at the %s-page cap; remaining events resume next run.",
                MAX_PAGES_PER_RUN,
            )

    except Exception:
        # persisted already reflects only acknowledged writes. Re-raise so the
        # failure surfaces in App Insights, and fires the failure alert if one
        # is configured (alertEmailAddress).
        LOGGER.exception(
            "Collector failed after %s events; checkpoint held at %s",
            ingested,
            persisted["lastCreated"],
        )
        raise

    LOGGER.info(
        "Ingested %s events across %s page(s); checkpoint now %s",
        ingested,
        pages,
        persisted["lastCreated"],
    )
