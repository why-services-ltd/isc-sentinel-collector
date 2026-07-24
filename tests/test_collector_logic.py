"""Unit tests for the collector's pure logic.

Bias of this suite: the properties under test are the ones whose failure loses
audit evidence. Duplicate ingestion is a nuisance and is asserted loosely;
dropped events are asserted hard.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from collector import logic


def event(event_id: str, created: str, **extra) -> dict:
    return {"id": event_id, "created": created, **extra}


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------


class TestTimestamps:
    def test_parses_zulu_suffix(self):
        parsed = logic.parse_iso8601("2024-05-01T12:34:56.789Z")
        assert parsed == datetime(2024, 5, 1, 12, 34, 56, 789000, tzinfo=timezone.utc)

    def test_parses_explicit_offset_and_normalises_to_utc(self):
        parsed = logic.parse_iso8601("2024-05-01T13:34:56.789+01:00")
        assert parsed == datetime(2024, 5, 1, 12, 34, 56, 789000, tzinfo=timezone.utc)

    def test_naive_timestamp_is_treated_as_utc_not_local(self):
        # A naive timestamp interpreted as local time would shift the window by
        # the host's offset and silently skip or duplicate an hour of events.
        parsed = logic.parse_iso8601("2024-05-01T12:34:56.789")
        assert parsed.tzinfo == timezone.utc
        assert parsed.hour == 12

    def test_round_trip_is_stable(self):
        original = "2024-05-01T12:34:56.789Z"
        assert logic.format_iso8601(logic.parse_iso8601(original)) == original

    def test_format_pads_milliseconds(self):
        moment = datetime(2024, 5, 1, 0, 0, 0, 5000, tzinfo=timezone.utc)
        assert logic.format_iso8601(moment) == "2024-05-01T00:00:00.005Z"

    def test_empty_timestamp_raises(self):
        with pytest.raises(ValueError):
            logic.parse_iso8601("")


# ---------------------------------------------------------------------------
# Checkpoint loading
# ---------------------------------------------------------------------------


class TestLoadCheckpoint:
    now = datetime(2024, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    lookback = timedelta(hours=1)

    def test_missing_checkpoint_starts_a_lookback_ago(self):
        loaded = logic.load_checkpoint(None, self.now, self.lookback)
        assert loaded["lastCreated"] == "2024-05-01T11:00:00.000Z"
        assert loaded["seenIds"] == []

    @pytest.mark.parametrize(
        "corrupt",
        [
            "not json at all",
            "[]",
            "null",
            '{"seenIds": []}',
            '{"lastCreated": 12345}',
            '{"lastCreated": "not-a-timestamp"}',
        ],
        ids=[
            "garbage",
            "wrong-type-list",
            "null",
            "missing-timestamp",
            "non-string-timestamp",
            "unparseable-timestamp",
        ],
    )
    def test_corrupt_checkpoint_rewinds_rather_than_skipping_forward(self, corrupt):
        # The original defect: a bad checkpoint reset the window to "now",
        # discarding everything in between. Re-fetching is acceptable; loss is not.
        loaded = logic.load_checkpoint(corrupt, self.now, self.lookback)
        assert logic.parse_iso8601(loaded["lastCreated"]) < self.now

    def test_accepts_bytes(self):
        payload = json.dumps(
            {"lastCreated": "2024-05-01T10:00:00.000Z", "seenIds": ["a"]}
        ).encode("utf-8")
        loaded = logic.load_checkpoint(payload, self.now, self.lookback)
        assert loaded["lastCreated"] == "2024-05-01T10:00:00.000Z"
        assert loaded["seenIds"] == ["a"]

    def test_invalid_utf8_rewinds(self):
        loaded = logic.load_checkpoint(b"\xff\xfe\x00", self.now, self.lookback)
        assert logic.parse_iso8601(loaded["lastCreated"]) < self.now

    def test_non_list_seen_ids_is_coerced(self):
        loaded = logic.load_checkpoint(
            '{"lastCreated": "2024-05-01T10:00:00.000Z", "seenIds": "oops"}',
            self.now,
            self.lookback,
        )
        assert loaded["seenIds"] == []


# ---------------------------------------------------------------------------
# Checkpoint advancement
# ---------------------------------------------------------------------------


class TestAdvanceCheckpoint:
    def test_empty_batch_leaves_checkpoint_untouched(self):
        before = {"lastCreated": "2024-05-01T10:00:00.000Z", "seenIds": ["a"]}
        assert logic.advance_checkpoint(before, []) == before

    def test_advances_to_newest_timestamp(self):
        before = {"lastCreated": "2024-05-01T10:00:00.000Z", "seenIds": []}
        after = logic.advance_checkpoint(
            before,
            [
                event("a", "2024-05-01T10:00:01.000Z"),
                event("b", "2024-05-01T10:00:02.000Z"),
            ],
        )
        assert after["lastCreated"] == "2024-05-01T10:00:02.000Z"
        assert after["seenIds"] == ["b"]

    def test_records_every_id_sharing_the_newest_millisecond(self):
        after = logic.advance_checkpoint(
            {"lastCreated": "2024-05-01T09:00:00.000Z", "seenIds": []},
            [
                event("a", "2024-05-01T10:00:00.000Z"),
                event("b", "2024-05-01T10:00:00.000Z"),
                event("c", "2024-05-01T10:00:00.000Z"),
            ],
        )
        assert after["lastCreated"] == "2024-05-01T10:00:00.000Z"
        assert after["seenIds"] == ["a", "b", "c"]

    def test_accumulates_ids_when_batch_sits_on_the_existing_boundary(self):
        # Two runs land on the same millisecond. Replacing rather than unioning
        # would forget run one's ids and re-ingest them.
        before = {"lastCreated": "2024-05-01T10:00:00.000Z", "seenIds": ["a"]}
        after = logic.advance_checkpoint(
            before, [event("b", "2024-05-01T10:00:00.000Z")]
        )
        assert after["seenIds"] == ["a", "b"]

    def test_does_not_carry_stale_ids_past_the_boundary(self):
        before = {"lastCreated": "2024-05-01T10:00:00.000Z", "seenIds": ["a"]}
        after = logic.advance_checkpoint(
            before, [event("b", "2024-05-01T10:00:05.000Z")]
        )
        assert after["seenIds"] == ["b"]

    def test_unordered_batch_still_yields_the_maximum(self):
        after = logic.advance_checkpoint(
            {"lastCreated": "2024-05-01T09:00:00.000Z", "seenIds": []},
            [
                event("b", "2024-05-01T10:00:05.000Z"),
                event("a", "2024-05-01T10:00:01.000Z"),
            ],
        )
        assert after["lastCreated"] == "2024-05-01T10:00:05.000Z"


# ---------------------------------------------------------------------------
# Boundary de-duplication
# ---------------------------------------------------------------------------


class TestFilterAlreadySeen:
    def test_no_seen_ids_keeps_everything(self):
        events = [event("a", "2024-05-01T10:00:00.000Z")]
        assert logic.filter_already_seen(events, {"lastCreated": "2024-05-01T10:00:00.000Z", "seenIds": []}) == events

    def test_drops_only_seen_ids_at_the_boundary(self):
        checkpoint = {"lastCreated": "2024-05-01T10:00:00.000Z", "seenIds": ["a"]}
        kept = logic.filter_already_seen(
            [
                event("a", "2024-05-01T10:00:00.000Z"),  # seen, boundary -> drop
                event("b", "2024-05-01T10:00:00.000Z"),  # unseen, boundary -> keep
                event("c", "2024-05-01T10:00:01.000Z"),  # newer -> keep
            ],
            checkpoint,
        )
        assert [item["id"] for item in kept] == ["b", "c"]

    def test_keeps_a_seen_id_that_reappears_at_a_different_timestamp(self):
        # Same id, later timestamp. Dropping on id alone would lose a real event.
        checkpoint = {"lastCreated": "2024-05-01T10:00:00.000Z", "seenIds": ["a"]}
        kept = logic.filter_already_seen(
            [event("a", "2024-05-01T10:00:01.000Z")], checkpoint
        )
        assert [item["id"] for item in kept] == ["a"]

    def test_keeps_late_arriving_older_events(self):
        # ISC delivering an event out of order must not cause it to be discarded.
        checkpoint = {"lastCreated": "2024-05-01T10:00:00.000Z", "seenIds": ["a"]}
        kept = logic.filter_already_seen(
            [event("z", "2024-05-01T09:59:00.000Z")], checkpoint
        )
        assert [item["id"] for item in kept] == ["z"]

    def test_round_trip_across_two_runs_neither_loses_nor_duplicates(self):
        boundary = "2024-05-01T10:00:00.000Z"
        run_one = [event("a", boundary), event("b", boundary)]

        checkpoint = logic.advance_checkpoint(
            {"lastCreated": "2024-05-01T09:00:00.000Z", "seenIds": []}, run_one
        )

        # The inclusive query re-offers both, plus a genuinely new third.
        run_two_offered = [
            event("a", boundary),
            event("b", boundary),
            event("c", boundary),
        ]
        kept = logic.filter_already_seen(run_two_offered, checkpoint)
        assert [item["id"] for item in kept] == ["c"]


# ---------------------------------------------------------------------------
# Query construction and pagination
# ---------------------------------------------------------------------------


class TestSearchBody:
    checkpoint = {"lastCreated": "2024-05-01T10:00:00.000Z", "seenIds": []}

    def test_range_is_inclusive_of_the_checkpoint(self):
        body = logic.build_search_body(self.checkpoint)
        assert body["query"]["query"] == "created:[2024-05-01T10:00:00.000Z TO *]"

    def test_sorts_by_created_then_id_for_stable_paging(self):
        assert logic.build_search_body(self.checkpoint)["sort"] == ["created", "id"]

    def test_omits_search_after_on_the_first_page(self):
        assert "searchAfter" not in logic.build_search_body(self.checkpoint)

    def test_includes_search_after_on_later_pages(self):
        body = logic.build_search_body(self.checkpoint, ["2024-05-01T10:00:00.000Z", "a"])
        assert body["searchAfter"] == ["2024-05-01T10:00:00.000Z", "a"]


class TestNextSearchAfter:
    def test_empty_page_signals_end_of_results(self):
        assert logic.next_search_after([]) is None

    def test_uses_the_last_event_sort_key(self):
        page = [
            event("a", "2024-05-01T10:00:00.000Z"),
            event("b", "2024-05-01T10:00:01.000Z"),
        ]
        assert logic.next_search_after(page) == ["2024-05-01T10:00:01.000Z", "b"]

    def test_cursor_actually_advances_between_pages(self):
        # The original defect: every iteration rebuilt an identical request, so
        # pagination looped on page one forever.
        first = [event("a", "2024-05-01T10:00:00.000Z")]
        second = [event("b", "2024-05-01T10:00:01.000Z")]
        assert logic.next_search_after(first) != logic.next_search_after(second)


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


class TestNormaliseEvent:
    raw = {
        "id": "evt-1",
        "created": "2024-05-01T10:00:00.789Z",
        "name": "Admin capability granted",
        "action": "IDENTITY_UPDATE",
        "type": "SYSTEM_CONFIG",
        "status": "PASSED",
        "actor": {"name": "alice", "id": "id-alice"},
        "target": {"name": "bob", "id": "id-bob", "type": "IDENTITY"},
        "ipAddress": "203.0.113.10",
        "trackingNumber": "trk-1",
        "technicalName": "IdentityUpdatePassed",
        "details": "granted ORG_ADMIN",
        "attributes": {"capability": "ORG_ADMIN"},
    }

    def test_maps_scalar_columns(self):
        row = logic.normalise_event(self.raw)
        assert row["EventId"] == "evt-1"
        assert row["EventName"] == "Admin capability granted"
        assert row["ActorName"] == "alice"
        assert row["TargetName"] == "bob"
        assert row["TargetType"] == "IDENTITY"
        assert row["IpAddress"] == "203.0.113.10"
        assert row["EventStatus"] == "PASSED"

    def test_time_generated_comes_from_created(self):
        assert logic.normalise_event(self.raw)["TimeGenerated"] == "2024-05-01T10:00:00.789Z"

    def test_raw_event_round_trips_the_untouched_payload(self):
        row = logic.normalise_event(self.raw)
        assert json.loads(row["RawEvent"]) == self.raw

    def test_missing_nested_objects_do_not_raise(self):
        row = logic.normalise_event({"id": "e", "created": "2024-05-01T10:00:00.000Z"})
        assert row["ActorName"] == ""
        assert row["TargetName"] == ""
        assert row["Attributes"] == {}

    def test_null_nested_objects_do_not_raise(self):
        row = logic.normalise_event(
            {
                "id": "e",
                "created": "2024-05-01T10:00:00.000Z",
                "actor": None,
                "target": None,
                "attributes": None,
            }
        )
        assert row["ActorName"] == ""
        assert row["Attributes"] == {}

    def test_non_string_scalar_is_serialised_not_dropped(self):
        row = logic.normalise_event(
            {"id": "e", "created": "2024-05-01T10:00:00.000Z", "details": {"k": "v"}}
        )
        assert json.loads(row["Details"]) == {"k": "v"}

    def test_emitted_columns_match_the_dcr_schema_exactly(self):
        # Guards against drift between this mapping and infra/main.bicep. A column
        # the DCR does not declare is dropped silently at ingestion.
        expected = {
            "TimeGenerated", "EventId", "EventName", "EventAction", "EventType",
            "EventStatus", "ActorName", "ActorId", "TargetName", "TargetId",
            "TargetType", "SourceName", "Application", "IpAddress",
            "TrackingNumber", "TechnicalName", "Details", "Attributes", "RawEvent",
        }
        assert set(logic.normalise_event(self.raw)) == expected


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


class TestChunkRecords:
    def test_no_records_yields_no_chunks(self):
        assert list(logic.chunk_records([])) == []

    def test_small_batch_is_a_single_chunk(self):
        records = [{"EventId": str(i)} for i in range(10)]
        assert list(logic.chunk_records(records)) == [records]

    def test_splits_on_the_event_count_limit(self):
        records = [{"EventId": str(i)} for i in range(25)]
        chunks = list(logic.chunk_records(records, max_events=10))
        assert [len(chunk) for chunk in chunks] == [10, 10, 5]

    def test_splits_on_the_byte_limit(self):
        records = [{"payload": "x" * 100} for _ in range(10)]
        chunks = list(logic.chunk_records(records, max_bytes=400))
        assert len(chunks) > 1
        for chunk in chunks:
            encoded = len(json.dumps(chunk, separators=(",", ":")).encode("utf-8"))
            assert encoded <= 400 + 50  # envelope overhead

    def test_oversized_record_is_emitted_alone_not_dropped(self):
        records = [{"payload": "x" * 5000}]
        chunks = list(logic.chunk_records(records, max_bytes=100))
        assert chunks == [records]

    def test_every_record_survives_chunking(self):
        records = [{"EventId": str(i), "payload": "y" * 50} for i in range(97)]
        chunks = list(logic.chunk_records(records, max_bytes=500, max_events=7))
        flattened = [record for chunk in chunks for record in chunk]
        assert flattened == records

    def test_order_is_preserved(self):
        # Chunks are acknowledged in order and the checkpoint advances per chunk,
        # so reordering would let a later timestamp mask an earlier failure.
        records = [{"EventId": str(i)} for i in range(50)]
        chunks = list(logic.chunk_records(records, max_events=6))
        flattened = [record["EventId"] for chunk in chunks for record in chunk]
        assert flattened == [str(i) for i in range(50)]

    def test_multibyte_characters_are_measured_in_bytes_not_characters(self):
        records = [{"name": "é" * 100} for _ in range(5)]
        chunks = list(logic.chunk_records(records, max_bytes=300))
        assert len(chunks) > 1
