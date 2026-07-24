"""Unit tests for the rotator's pure logic.

The ordered network calls in ``function_app.py`` are not covered here -- they
need ISC and are exercised by the staged first run in infra/README.md. What is
covered is every decision that could cause the rotator to either skip a needed
rotation or act on a credential it did not understand.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# rotator/logic.py shares a module name with collector/logic.py, so it is
# loaded under an explicit alias rather than via sys.path.
_PATH = Path(__file__).resolve().parents[1] / "rotator" / "logic.py"
_spec = importlib.util.spec_from_file_location("rotator_logic", _PATH)
logic = importlib.util.module_from_spec(_spec)
sys.modules["rotator_logic"] = logic
_spec.loader.exec_module(logic)


NOW = datetime(2024, 5, 1, 12, 0, 0, tzinfo=timezone.utc)


class TestBuildSecretPayload:
    def test_round_trips_through_parse(self):
        raw = logic.build_secret_payload("cid", "csecret", created_at=NOW)
        parsed = logic.parse_secret_payload(raw)
        assert parsed["clientId"] == "cid"
        assert parsed["clientSecret"] == "csecret"
        assert parsed["type"] == logic.CREDENTIAL_TYPE_ROTATED_PAT

    def test_records_creation_time_so_age_is_knowable(self):
        raw = logic.build_secret_payload("cid", "csecret", created_at=NOW)
        assert json.loads(raw)["createdAt"] == "2024-05-01T12:00:00+00:00"

    def test_defaults_to_rotated_pat_type(self):
        assert json.loads(logic.build_secret_payload("a", "b"))["type"] == "rotated-pat"

    @pytest.mark.parametrize(
        ("client_id", "client_secret"),
        [("", "secret"), ("id", ""), ("", "")],
        ids=["no-id", "no-secret", "neither"],
    )
    def test_refuses_to_serialise_an_incomplete_credential(self, client_id, client_secret):
        # Writing a half-formed credential to Key Vault would break collection
        # at the next run, after the old one had already been deleted.
        with pytest.raises(ValueError):
            logic.build_secret_payload(client_id, client_secret)


class TestParseSecretPayload:
    def test_accepts_bytes(self):
        raw = logic.build_secret_payload("cid", "csecret", created_at=NOW).encode()
        assert logic.parse_secret_payload(raw)["clientId"] == "cid"

    def test_untyped_credential_is_assumed_to_be_the_seed_pat(self):
        # The bootstrap secret is written by CLI and may lack a type. Assuming
        # PAT is the safe default: it triggers migration rather than letting an
        # inherited-access credential sit indefinitely.
        parsed = logic.parse_secret_payload('{"clientId": "a", "clientSecret": "b"}')
        assert parsed["type"] == logic.CREDENTIAL_TYPE_PAT

    @pytest.mark.parametrize(
        "raw",
        [None, "not json", "[]", "null", '{"clientId": "a"}', '{"clientSecret": "b"}', "{}"],
        ids=["none", "garbage", "list", "null", "no-secret", "no-id", "empty"],
    )
    def test_raises_rather_than_guessing(self, raw):
        # A rotator that cannot read the current credential must stop. Guessing
        # risks deleting a working credential it never understood.
        with pytest.raises(ValueError):
            logic.parse_secret_payload(raw)


class TestCredentialAge:
    def test_computes_age_from_created_at(self):
        payload = {"createdAt": "2024-04-01T12:00:00+00:00"}
        assert logic.credential_age(payload, NOW) == timedelta(days=30)

    def test_handles_zulu_suffix(self):
        payload = {"createdAt": "2024-04-01T12:00:00Z"}
        assert logic.credential_age(payload, NOW) == timedelta(days=30)

    def test_naive_timestamp_is_treated_as_utc(self):
        payload = {"createdAt": "2024-04-01T12:00:00"}
        assert logic.credential_age(payload, NOW) == timedelta(days=30)

    @pytest.mark.parametrize(
        "value", [None, "", "not-a-date", 12345], ids=["none", "empty", "garbage", "int"]
    )
    def test_unusable_timestamp_returns_none(self, value):
        assert logic.credential_age({"createdAt": value}, NOW) is None

    def test_missing_key_returns_none(self):
        assert logic.credential_age({}, NOW) is None


class TestShouldRotate:
    def _rotated(self, created_at: str | None) -> dict:
        return {"type": logic.CREDENTIAL_TYPE_ROTATED_PAT, "createdAt": created_at}

    def test_seed_pat_always_rotates_regardless_of_age(self):
        # Migrating off the seed PAT is the purpose of the first run: it
        # inherits the full access of the human who created it.
        fresh_pat = {"type": logic.CREDENTIAL_TYPE_PAT, "createdAt": NOW.isoformat()}
        assert logic.should_rotate(fresh_pat, NOW) is True

    def test_fresh_rotated_pat_does_not_rotate(self):
        assert logic.should_rotate(self._rotated("2024-04-25T12:00:00Z"), NOW) is False

    def test_rotated_pat_past_max_age_rotates(self):
        assert logic.should_rotate(self._rotated("2024-01-01T12:00:00Z"), NOW) is True

    def test_rotates_exactly_at_the_boundary(self):
        boundary = (NOW - logic.DEFAULT_MAX_AGE).isoformat()
        assert logic.should_rotate(self._rotated(boundary), NOW) is True

    def test_does_not_rotate_one_second_before_the_boundary(self):
        just_inside = (NOW - logic.DEFAULT_MAX_AGE + timedelta(seconds=1)).isoformat()
        assert logic.should_rotate(self._rotated(just_inside), NOW) is False

    def test_unknown_age_rotates_rather_than_assuming_freshness(self):
        # Assuming fresh would let a credential drift to expiry and stop
        # collection silently.
        assert logic.should_rotate(self._rotated(None), NOW) is True

    def test_max_age_is_configurable(self):
        payload = self._rotated("2024-04-25T12:00:00Z")  # 6 days old
        assert logic.should_rotate(payload, NOW, max_age=timedelta(days=5)) is True
        assert logic.should_rotate(payload, NOW, max_age=timedelta(days=7)) is False

    def test_default_max_age_leaves_room_before_pat_hard_expiry(self):
        # PAT_HARD_EXPIRY is a backstop, not the primary control. If
        # DEFAULT_MAX_AGE ever crept up to meet it, a single missed weekly
        # check could let ISC's own expiry be the thing that actually bites,
        # rather than our own rotation.
        assert logic.DEFAULT_MAX_AGE < logic.PAT_HARD_EXPIRY

    def test_pat_hard_expiry_leaves_at_least_a_week_of_margin(self):
        # The rotator checks weekly (ROTATOR_SCHEDULE); a single missed check
        # must not be enough to reach PAT_HARD_EXPIRY before the next one.
        assert logic.PAT_HARD_EXPIRY - logic.DEFAULT_MAX_AGE >= timedelta(days=7)
