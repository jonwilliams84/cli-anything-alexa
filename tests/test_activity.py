"""Behavioural tests for core/activity.py — Alexa voice history.

The pure layer (timestamp rendering, window computation, limit validation, the
two feed flatteners, filtering, the last-command row and the clear summary) is
tested directly.  The live wrappers are exercised against a fake ``AlexaAPI`` so
the query parameters actually sent, and the order of validation vs. network, are
pinned without a live account.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_anything.alexa.core import activity


def _run(coro):
    return asyncio.run(coro)


DEVICES = [
    {"serialNumber": "SN1", "accountName": "Kitchen Echo"},
    {"serialNumber": "SN2", "accountName": "Study Dot"},
]


def _privacy_record(
    serial="SN1",
    summary="turn off the kitchen lights",
    response="OK",
    when=1_750_000_000_000,
    utterance_type="GENERAL",
):
    return {
        "deviceSerialNumber": serial,
        "description": {"summary": summary},
        "alexaResponse": response,
        "creationTimestamp": when,
        "utteranceType": utterance_type,
    }


# ── format_timestamp ────────────────────────────────────────────────────


def test_format_timestamp_renders_utc_aware_iso():
    """Epoch ms are UTC; a naive fromtimestamp would re-read them locally."""
    rendered = activity.format_timestamp(1_750_000_000_000)
    assert rendered == datetime(2025, 6, 15, 15, 6, 40, tzinfo=timezone.utc).isoformat()
    assert rendered.endswith("+00:00")


def test_format_timestamp_accepts_a_numeric_string():
    assert activity.format_timestamp("1750000000000") == activity.format_timestamp(
        1_750_000_000_000
    )


@pytest.mark.parametrize("bad", [None, "", "yesterday", object(), [], {}])
def test_format_timestamp_returns_none_for_unparseable_values(bad):
    assert activity.format_timestamp(bad) is None


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_format_timestamp_returns_none_for_nan_and_inf(bad):
    assert activity.format_timestamp(bad) is None


@pytest.mark.parametrize("absurd", [1e30, -1e30])
def test_format_timestamp_survives_absurd_epochs(absurd):
    """A junk epoch must degrade to None, never raise OverflowError/OSError."""
    assert activity.format_timestamp(absurd) is None


# ── history_window ──────────────────────────────────────────────────────


def test_history_window_ends_now_and_starts_hours_earlier():
    now = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
    start, end = activity.history_window(6, now=now)
    assert end == int(now.timestamp() * 1000)
    assert start == int((now - timedelta(hours=6)).timestamp() * 1000)


@pytest.mark.parametrize("unspecified", [None, ""])
def test_history_window_defaults_to_the_documented_span(unspecified):
    now = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
    start, end = activity.history_window(unspecified, now=now)
    expected = now - timedelta(hours=activity.DEFAULT_HISTORY_HOURS)
    assert start == int(expected.timestamp() * 1000)


def test_history_window_accepts_a_string_and_a_fraction():
    now = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
    assert activity.history_window("2", now=now) == activity.history_window(2.0, now=now)
    start, end = activity.history_window(0.5, now=now)
    assert end - start == 30 * 60 * 1000


def test_history_window_treats_a_naive_now_as_utc():
    aware = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
    naive = aware.replace(tzinfo=None)  # what a caller who forgot the tz passes in
    assert activity.history_window(1, now=naive) == activity.history_window(1, now=aware)


def test_history_window_defaults_now_to_the_current_utc_time():
    before = datetime.now(tz=timezone.utc).timestamp() * 1000
    start, end = activity.history_window(1)
    assert end >= int(before) - 1000
    assert end - start == 3_600_000


@pytest.mark.parametrize("bad", ["soon", object()])
def test_history_window_rejects_non_numbers(bad):
    with pytest.raises(ValueError, match="hours must be a number"):
        activity.history_window(bad)


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_history_window_rejects_nan_and_inf(bad):
    with pytest.raises(ValueError, match="hours must be a number"):
        activity.history_window(bad)


@pytest.mark.parametrize("bad", [0, -1, -0.5])
def test_history_window_rejects_non_positive_spans(bad):
    with pytest.raises(ValueError, match="greater than 0"):
        activity.history_window(bad)


# ── normalize_limit ─────────────────────────────────────────────────────


@pytest.mark.parametrize("unspecified", [None, ""])
def test_normalize_limit_defaults(unspecified):
    assert activity.normalize_limit(unspecified) == activity.DEFAULT_HISTORY_LIMIT
    assert activity.normalize_limit(unspecified, default=50) == 50


@pytest.mark.parametrize(("given", "expected"), [(1, 1), ("5", 5), (200, 200)])
def test_normalize_limit_accepts_positive_whole_numbers(given, expected):
    assert activity.normalize_limit(given) == expected


@pytest.mark.parametrize("bad", ["lots", object(), 1.5, None if False else "3.5"])
def test_normalize_limit_rejects_non_whole_numbers(bad):
    with pytest.raises(ValueError, match="whole number"):
        activity.normalize_limit(bad)


@pytest.mark.parametrize("bad", [0, -1, "-5"])
def test_normalize_limit_rejects_zero_and_negative(bad):
    with pytest.raises(ValueError, match="at least 1"):
        activity.normalize_limit(bad)


# ── history_rows ────────────────────────────────────────────────────────


def test_history_rows_flattens_a_turn_and_names_the_device():
    (row,) = activity.history_rows([_privacy_record()], DEVICES)
    assert row == {
        "time": activity.format_timestamp(1_750_000_000_000),
        "device": "Kitchen Echo",
        "utterance": "turn off the kitchen lights",
        "response": "OK",
        "type": "GENERAL",
    }


def test_history_rows_falls_back_to_the_serial_when_the_device_is_unknown():
    (row,) = activity.history_rows([_privacy_record(serial="SN9")], DEVICES)
    assert row["device"] == "SN9"


def test_history_rows_accepts_a_plain_string_description():
    (row,) = activity.history_rows([{"description": "  hello  "}])
    assert row["utterance"] == "hello"


@pytest.mark.parametrize("empty", [None, [], ()])
def test_history_rows_of_nothing_is_an_empty_list(empty):
    assert activity.history_rows(empty) == []


def test_history_rows_skips_junk_without_losing_the_good_rows():
    """One malformed record must not cost the other 19."""
    rows = activity.history_rows(["nope", None, 42, _privacy_record()], DEVICES)
    assert len(rows) == 1
    assert rows[0]["utterance"] == "turn off the kitchen lights"


@pytest.mark.parametrize("description", [None, 7, [], ("a",)])
def test_history_rows_survives_a_record_with_no_usable_description(description):
    (row,) = activity.history_rows([{"description": description, "creationTimestamp": 1}])
    assert row["utterance"] is None
    assert row["time"] is not None


@pytest.mark.parametrize("blank", ["", "   ", None, 7, {}])
def test_history_rows_blank_transcript_becomes_none(blank):
    (row,) = activity.history_rows([{"description": {"summary": blank}, "alexaResponse": blank}])
    assert row["utterance"] is None
    assert row["response"] is None


# ── activity_rows ───────────────────────────────────────────────────────


def _legacy(summary="what's the weather", status="SUCCESS", serial="SN2", when=1_750_000_001_000):
    return {
        "description": json.dumps({"summary": summary}),
        "activityStatus": status,
        "sourceDeviceIds": [{"serialNumber": serial, "deviceType": "ECHO"}],
        "creationTimestamp": when,
        "id": "activity-1",
    }


def test_activity_rows_decodes_the_json_encoded_description():
    (row,) = activity.activity_rows([_legacy()], DEVICES)
    assert row == {
        "time": activity.format_timestamp(1_750_000_001_000),
        "device": "Study Dot",
        "utterance": "what's the weather",
        "status": "SUCCESS",
        "id": "activity-1",
    }


def test_activity_rows_accepts_the_enveloped_shape():
    rows = activity.activity_rows({"activities": [_legacy()]}, DEVICES)
    assert [r["id"] for r in rows] == ["activity-1"]


def test_activity_rows_degrades_undecodable_description_to_raw_text():
    (row,) = activity.activity_rows([{"description": " not json at all "}])
    assert row["utterance"] == "not json at all"


def test_activity_rows_accepts_an_already_decoded_description():
    (row,) = activity.activity_rows([{"description": {"summary": "hi"}}])
    assert row["utterance"] == "hi"


def test_activity_rows_json_scalar_description_degrades_to_the_raw_text():
    (row,) = activity.activity_rows([{"description": "12345"}])
    assert row["utterance"] == "12345"


@pytest.mark.parametrize("empty", [None, [], {}, {"activities": None}, "junk"])
def test_activity_rows_of_nothing_is_an_empty_list(empty):
    assert activity.activity_rows(empty) == []


def test_activity_rows_skips_non_dict_items_and_missing_sources():
    rows = activity.activity_rows([None, 1, {"id": "bare"}, {"sourceDeviceIds": "junk"}])
    assert [r["id"] for r in rows] == ["bare", None]
    assert all(r["device"] is None for r in rows)


def test_activity_rows_takes_the_first_source_with_a_serial():
    (row,) = activity.activity_rows(
        [{"sourceDeviceIds": ["junk", {"deviceType": "x"}, {"serialNumber": "SN1"}]}], DEVICES
    )
    assert row["device"] == "Kitchen Echo"


# ── filter_rows ─────────────────────────────────────────────────────────


def _rows():
    return activity.history_rows(
        [
            _privacy_record(summary="turn off the kitchen lights", response="OK"),
            _privacy_record(serial="SN2", summary="what's the weather", response="It is sunny"),
            _privacy_record(serial="SN2", summary=None, utterance_type="DEVICE_ARBITRATION"),
        ],
        DEVICES,
    )


def test_filter_rows_drops_arbitration_noise_by_default():
    rows = activity.filter_rows(_rows())
    assert len(rows) == 2
    assert all(r["type"] != "DEVICE_ARBITRATION" for r in rows)


def test_filter_rows_can_keep_the_noise():
    assert len(activity.filter_rows(_rows(), include_noise=True)) == 3


@pytest.mark.parametrize("needle", ["Study", "study dot", " STUDY "])
def test_filter_rows_matches_a_device_substring_case_insensitively(needle):
    rows = activity.filter_rows(_rows(), device=needle)
    assert [r["utterance"] for r in rows] == ["what's the weather"]


def test_filter_rows_contains_searches_both_halves_of_the_turn():
    assert len(activity.filter_rows(_rows(), contains="kitchen")) == 1
    assert len(activity.filter_rows(_rows(), contains="sunny")) == 1  # Alexa's reply
    assert activity.filter_rows(_rows(), contains="nonsense") == []


def test_filter_rows_combines_device_and_contains():
    assert activity.filter_rows(_rows(), device="Kitchen", contains="weather") == []


@pytest.mark.parametrize("empty", [None, []])
def test_filter_rows_of_nothing_is_an_empty_list(empty):
    assert activity.filter_rows(empty) == []


@pytest.mark.parametrize(("device", "contains"), [("", ""), ("  ", None), (None, "   ")])
def test_filter_rows_blank_filters_are_no_ops(device, contains):
    assert len(activity.filter_rows(_rows(), device=device, contains=contains)) == 2


# ── last_command_row ────────────────────────────────────────────────────


def test_last_command_row_flattens_the_serial_lookup():
    row = activity.last_command_row(
        {"serialNumber": "SN1", "timestamp": 1_750_000_000_000, "summary": " play jazz "}, DEVICES
    )
    assert row == {
        "time": activity.format_timestamp(1_750_000_000_000),
        "device": "Kitchen Echo",
        "serial": "SN1",
        "utterance": "play jazz",
    }


@pytest.mark.parametrize("nothing", [None, "junk", []])
def test_last_command_row_of_nothing_is_an_empty_row_not_an_error(nothing):
    """ "nothing was said recently" is a valid answer, not a failure."""
    assert activity.last_command_row(nothing) == {
        "time": None,
        "device": None,
        "serial": None,
        "utterance": None,
    }


# ── clear_summary ───────────────────────────────────────────────────────


def test_clear_summary_reports_a_clean_clear():
    assert activity.clear_summary(True, 50) == {"requested": 50, "cleared": True}


@pytest.mark.parametrize("refused", [False, None, 0])
def test_clear_summary_reports_a_partial_clear_with_the_app_side_remedy(refused):
    """alexapy returns False when Amazon 404s an entry — never call that clean."""
    summary = activity.clear_summary(refused, 10)
    assert summary["cleared"] is False
    assert "Alexa app" in summary["hint"]


# ── live operations ─────────────────────────────────────────────────────


def _fake_api(**awaits):
    fake_cls = MagicMock()
    fake_cls.get_devices = AsyncMock(return_value=DEVICES)
    for name, value in awaits.items():
        setattr(fake_cls, name, AsyncMock(return_value=value))
    return fake_cls


def test_fetch_history_sends_the_computed_window_and_limit():
    now = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
    fake_cls = _fake_api(get_customer_history_records=[_privacy_record()])
    login = MagicMock()
    with patch("alexapy.AlexaAPI", fake_cls):
        records = _run(activity.fetch_history(login, limit=5, hours=6, now=now))
    start, end = activity.history_window(6, now=now)
    fake_cls.get_customer_history_records.assert_awaited_once_with(
        login, start_time=start, end_time=end, max_record_size=5
    )
    assert records == [_privacy_record()]


def test_voice_history_returns_named_filtered_rows():
    fake_cls = _fake_api(
        get_customer_history_records=[
            _privacy_record(),
            _privacy_record(serial="SN2", summary=None, utterance_type="DEVICE_ARBITRATION"),
        ]
    )
    with patch("alexapy.AlexaAPI", fake_cls):
        rows = _run(activity.voice_history(MagicMock(), limit="5", hours="12"))
    assert [r["device"] for r in rows] == ["Kitchen Echo"]  # arbitration filtered out


def test_voice_history_filters_by_device_and_text():
    fake_cls = _fake_api(
        get_customer_history_records=[
            _privacy_record(),
            _privacy_record(serial="SN2", summary="what's the weather"),
        ]
    )
    with patch("alexapy.AlexaAPI", fake_cls):
        rows = _run(activity.voice_history(MagicMock(), device="Study", contains="weather"))
    assert [r["utterance"] for r in rows] == ["what's the weather"]


def test_voice_history_validates_the_window_before_spending_a_request():
    fake_cls = _fake_api(get_customer_history_records=[])
    with patch("alexapy.AlexaAPI", fake_cls), pytest.raises(ValueError, match="greater than 0"):
        _run(activity.voice_history(MagicMock(), hours=0))
    fake_cls.get_customer_history_records.assert_not_awaited()


def test_voice_history_validates_the_limit_before_spending_a_request():
    fake_cls = _fake_api(get_customer_history_records=[])
    with patch("alexapy.AlexaAPI", fake_cls), pytest.raises(ValueError, match="whole number"):
        _run(activity.voice_history(MagicMock(), limit="many"))
    fake_cls.get_customer_history_records.assert_not_awaited()


def test_activity_records_uses_the_legacy_feed_with_the_requested_count():
    fake_cls = _fake_api(get_activities=[_legacy()])
    login = MagicMock()
    with patch("alexapy.AlexaAPI", fake_cls):
        rows = _run(activity.activity_records(login, limit="3"))
    fake_cls.get_activities.assert_awaited_once_with(login, items=3)
    assert [r["id"] for r in rows] == ["activity-1"]


def test_last_command_names_the_answering_echo():
    fake_cls = _fake_api(
        get_last_device_serial={"serialNumber": "SN2", "summary": "set a timer", "timestamp": 1}
    )
    login = MagicMock()
    with patch("alexapy.AlexaAPI", fake_cls):
        row = _run(activity.last_command(login, limit=7))
    fake_cls.get_last_device_serial.assert_awaited_once_with(login, items=7)
    assert row["device"] == "Study Dot"
    assert row["utterance"] == "set a timer"


def test_last_command_with_no_recent_turn():
    fake_cls = _fake_api(get_last_device_serial=None)
    with patch("alexapy.AlexaAPI", fake_cls):
        row = _run(activity.last_command(MagicMock()))
    assert row == {"time": None, "device": None, "serial": None, "utterance": None}


def test_clear_history_defaults_to_fifty_items():
    fake_cls = _fake_api(clear_history=True)
    login = MagicMock()
    with patch("alexapy.AlexaAPI", fake_cls):
        summary = _run(activity.clear_history(login))
    fake_cls.clear_history.assert_awaited_once_with(login, items=50)
    assert summary == {"requested": 50, "cleared": True}


def test_clear_history_reports_amazons_partial_refusal():
    fake_cls = _fake_api(clear_history=False)
    with patch("alexapy.AlexaAPI", fake_cls):
        summary = _run(activity.clear_history(MagicMock(), items="5"))
    assert summary["requested"] == 5
    assert summary["cleared"] is False
    assert "hint" in summary


def test_clear_history_validates_the_count_before_deleting_anything():
    fake_cls = _fake_api(clear_history=True)
    with patch("alexapy.AlexaAPI", fake_cls), pytest.raises(ValueError, match="at least 1"):
        _run(activity.clear_history(MagicMock(), items=0))
    fake_cls.clear_history.assert_not_awaited()
