"""Pure logic for the SailPoint ISC collector.

Everything in this module is side-effect free and dependency-free so it can be
unit tested without Azure or ISC. The Azure Functions entry point in
``__init__.py`` supplies the I/O.

The correctness properties that matter here, all of which were defects in the
original implementation:

* Pagination actually advances (``search_after`` from the last sort key).
* The checkpoint never moves past data that failed to ingest.
* No code path discards events in order to catch up.
* Events sharing the checkpoint's exact timestamp are de-duplicated by id
  rather than skipped by moving the window forward, which would lose them.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Iterator, Sequence

# The Logs Ingestion API accepts 1 MB uncompressed per call. Leave headroom for
# the JSON array envelope and multi-byte characters in names.
MAX_CHUNK_BYTES = 900_000

# Upper bound on events per call. Well inside the size limit for typical events,
# and keeps a single failure from costing a large re-fetch.
MAX_CHUNK_EVENTS = 1_000

# Sort key for the ISC search API. ``created`` alone is not unique -- ISC
# routinely emits several events in the same millisecond -- so id breaks ties
# and makes deep pagination stable.
SORT_KEYS = ["created", "id"]


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------


def parse_iso8601(value: str) -> datetime:
    """Parse an ISC timestamp into a timezone-aware UTC datetime.

    ISC emits ``2024-05-01T12:34:56.789Z``. Python's ``fromisoformat`` accepts
    ``Z`` only from 3.11, and we normalise to UTC regardless so that comparisons
    between checkpoint and event timestamps are never naive-vs-aware.
    """
    if not value:
        raise ValueError("empty timestamp")

    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"

    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_iso8601(value: datetime) -> str:
    """Render a datetime in the millisecond-precision UTC form ISC expects."""
    utc = value.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------------
# Checkpoint
#
# Shape: {"lastCreated": "<iso8601>", "seenIds": ["<id>", ...]}
#
# ``seenIds`` holds only the ids whose ``created`` equals ``lastCreated``. The
# next run queries inclusively from ``lastCreated`` and drops those ids. This is
# what stops the same-millisecond boundary from either duplicating events (query
# inclusive, no filter) or losing them (query exclusive).
# ---------------------------------------------------------------------------


def initial_checkpoint(now: datetime, lookback: timedelta) -> dict[str, Any]:
    """Checkpoint for a tenant with no stored state: start ``lookback`` ago."""
    return {"lastCreated": format_iso8601(now - lookback), "seenIds": []}


def load_checkpoint(
    stored: str | bytes | None, now: datetime, lookback: timedelta
) -> dict[str, Any]:
    """Deserialise a stored checkpoint, falling back to a fresh one.

    A malformed or absent checkpoint starts the window ``lookback`` ago and
    re-fetches. It must never silently jump the window forward to "now" -- the
    original implementation did, discarding a day of events every time it
    tripped.
    """
    if stored is None:
        return initial_checkpoint(now, lookback)

    try:
        if isinstance(stored, bytes):
            stored = stored.decode("utf-8")
        parsed = json.loads(stored)
    except (ValueError, UnicodeDecodeError):
        return initial_checkpoint(now, lookback)

    if not isinstance(parsed, dict):
        return initial_checkpoint(now, lookback)

    last_created = parsed.get("lastCreated")
    if not isinstance(last_created, str):
        return initial_checkpoint(now, lookback)

    try:
        parse_iso8601(last_created)
    except ValueError:
        return initial_checkpoint(now, lookback)

    seen = parsed.get("seenIds")
    if not isinstance(seen, list):
        seen = []

    return {
        "lastCreated": last_created,
        "seenIds": [str(item) for item in seen],
    }


def advance_checkpoint(
    checkpoint: dict[str, Any], events: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    """Return the checkpoint after ``events`` have been *successfully ingested*.

    Call this only once the write has been acknowledged. ``events`` are the raw
    ISC events, assumed sorted ascending by (created, id).

    If the batch's newest timestamp equals the existing ``lastCreated``, the
    seen-id set accumulates rather than replaces -- otherwise ids carried over
    from the previous run would be forgotten and re-ingested.
    """
    if not events:
        return dict(checkpoint)

    newest = max(parse_iso8601(event["created"]) for event in events)
    newest_text = format_iso8601(newest)

    ids_at_newest = {
        str(event["id"])
        for event in events
        if parse_iso8601(event["created"]) == newest
    }

    if newest_text == checkpoint.get("lastCreated"):
        ids_at_newest |= set(checkpoint.get("seenIds", []))

    return {"lastCreated": newest_text, "seenIds": sorted(ids_at_newest)}


def filter_already_seen(
    events: Iterable[dict[str, Any]], checkpoint: dict[str, Any]
) -> list[dict[str, Any]]:
    """Drop events already ingested at the checkpoint's exact timestamp.

    Only events at precisely ``lastCreated`` are candidates for removal. Anything
    newer is kept. Anything older is also kept: it would mean ISC delivered an
    event late, and dropping it would be silent loss.
    """
    seen = set(checkpoint.get("seenIds", []))
    if not seen:
        return list(events)

    boundary = parse_iso8601(checkpoint["lastCreated"])

    kept: list[dict[str, Any]] = []
    for event in events:
        at_boundary = parse_iso8601(event["created"]) == boundary
        if at_boundary and str(event["id"]) in seen:
            continue
        kept.append(event)
    return kept


# ---------------------------------------------------------------------------
# Query construction
# ---------------------------------------------------------------------------


def build_search_body(
    checkpoint: dict[str, Any], search_after: Sequence[Any] | None = None
) -> dict[str, Any]:
    """Build the POST body for ``/v2025/search/events``.

    The range is inclusive of ``lastCreated`` so that same-millisecond events are
    re-offered and then filtered by id. Excluding the boundary would be a silent
    loss of every event sharing that millisecond.
    """
    body: dict[str, Any] = {
        "indices": ["events"],
        "query": {
            "query": f'created:[{checkpoint["lastCreated"]} TO *]',
        },
        "sort": SORT_KEYS,
    }
    if search_after:
        body["searchAfter"] = list(search_after)
    return body


def next_search_after(events: Sequence[dict[str, Any]]) -> list[Any] | None:
    """Sort key of the last event in a page, for the following request.

    Returning ``None`` means there is nothing to page from, which the caller must
    treat as end-of-results. The original implementation rebuilt the same request
    every iteration and so never advanced past page one.
    """
    if not events:
        return None
    last = events[-1]
    return [last["created"], last["id"]]


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    """Coerce to a string column value, mapping absent/null to empty string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def normalise_event(event: dict[str, Any]) -> dict[str, Any]:
    """Map a raw ISC event onto the DCR's column shape.

    ``RawEvent`` carries the untouched ISC payload so that a detection engineer
    is never blocked by a mapping decision made here, and so a schema change at
    the ISC end degrades to "the normalised column is empty" rather than
    "the data is gone".
    """
    actor = event.get("actor") or {}
    target = event.get("target") or {}

    return {
        "TimeGenerated": format_iso8601(parse_iso8601(event["created"])),
        "EventId": _text(event.get("id")),
        "EventName": _text(event.get("name")),
        "EventAction": _text(event.get("action")),
        "EventType": _text(event.get("type")),
        "EventStatus": _text(event.get("status")),
        "ActorName": _text(actor.get("name")),
        "ActorId": _text(actor.get("id")),
        "TargetName": _text(target.get("name")),
        "TargetId": _text(target.get("id")),
        "TargetType": _text(target.get("type")),
        "SourceName": _text(event.get("sourceName")),
        "Application": _text(event.get("application")),
        "IpAddress": _text(event.get("ipAddress")),
        "TrackingNumber": _text(event.get("trackingNumber")),
        "TechnicalName": _text(event.get("technicalName")),
        "Details": _text(event.get("details")),
        "Attributes": event.get("attributes") or {},
        "RawEvent": json.dumps(event, separators=(",", ":"), sort_keys=True),
    }


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


def _encoded_size(record: dict[str, Any]) -> int:
    return len(json.dumps(record, separators=(",", ":")).encode("utf-8"))


def chunk_records(
    records: Sequence[dict[str, Any]],
    max_bytes: int = MAX_CHUNK_BYTES,
    max_events: int = MAX_CHUNK_EVENTS,
) -> Iterator[list[dict[str, Any]]]:
    """Split records into request-sized batches, preserving order.

    Order matters: the caller advances the checkpoint per acknowledged chunk, so
    reordering would let a later event's timestamp mask an earlier failure.

    A single record larger than ``max_bytes`` is still yielded, alone. Dropping it
    would be silent loss, and the ingestion API rejecting it is the louder and
    more honest outcome.
    """
    batch: list[dict[str, Any]] = []
    batch_bytes = 0

    for record in records:
        size = _encoded_size(record)

        if batch and (batch_bytes + size > max_bytes or len(batch) >= max_events):
            yield batch
            batch = []
            batch_bytes = 0

        batch.append(record)
        batch_bytes += size

    if batch:
        yield batch
