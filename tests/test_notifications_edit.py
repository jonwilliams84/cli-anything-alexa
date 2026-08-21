"""Behavioural tests for the notification EDIT surface (core/notifications.py).

The edit half — ``pause`` / ``resume`` / ``reschedule`` / ``snooze`` — is the
only place this harness rewrites a record Amazon already owns, so the rules
that keep it safe are pinned here:

* an edit is a **whole-record** PUT built from the record Amazon returned, not
  a hand-rolled minimal body (dropping fields silently loses recurrence);
* a reminder's LOCAL ``originalDate``/``originalTime`` move with ``alarmTime``,
  in the owning Echo's timezone;
* an ambiguous label is refused, never resolved by "first match";
* a timer cannot be rescheduled or snoozed;
* the write is **verified by re-reading**, and an unreadable verify reports
  ``ok: None`` rather than failure.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_anything.alexa.core import notifications as notif


def _run(coro):
    return asyncio.run(coro)


# 2026-01-01T07:00:00Z
ALARM_MS = 1767250800000
NOW_MS = 1767247200000  # one hour earlier


def _alarm(**over):
    record = {
        "notificationIndex": "alarm-1",
        "type": "Alarm",
        "status": "ON",
        "alarmTime": ALARM_MS,
        "originalDate": "2026-01-01",
        "originalTime": "07:00:00.000",
        "originalLabel": "Wake up",
        "deviceSerialNumber": "SN1",
        "deviceType": "DT1",
        "rRuleData": {"byWeekDays": ["MO"]},
    }
    record.update(over)
    return record


def _timer(**over):
    record = {
        "notificationIndex": "timer-1",
        "type": "Timer",
        "status": "ON",
        "remainingTime": 60000,
        "timerLabel": "Pasta",
        "deviceSerialNumber": "SN1",
        "deviceType": "DT1",
    }
    record.update(over)
    return record


def _fake_api(records, *, put_response=None, after=None):
    """Fake ``AlexaAPI``: the notifications read + the PUT edit."""
    fake = MagicMock()
    reads = [list(records)] if after is None else [list(records), list(after)]
    fake.get_notifications = AsyncMock(
        side_effect=lambda _login: list(reads.pop(0) if reads else [])
    )
    fake.set_notifications = AsyncMock(return_value=put_response)
    fake.get_device_preferences = AsyncMock(
        return_value={
            "devicePreferences": [{"deviceSerialNumber": "SN1", "timeZoneId": "Europe/London"}]
        }
    )
    fake.get_devices = AsyncMock(return_value=[{"serialNumber": "SN1", "accountName": "Kitchen"}])
    return fake


# ── id / label / status helpers ──────────────────────────────────────────


def test_notification_id_prefers_notification_index_then_id():
    assert notif.notification_id({"notificationIndex": "a", "id": "b"}) == "a"
    assert notif.notification_id({"id": "b"}) == "b"


def test_notification_id_of_a_non_record_is_none():
    assert notif.notification_id(None) is None
    assert notif.notification_id("alarm-1") is None
    assert notif.notification_id({}) is None


def test_notification_label_reads_whichever_label_field_the_type_uses():
    assert notif.notification_label(_alarm()) == "Wake up"
    assert notif.notification_label(_timer()) == "Pasta"
    assert notif.notification_label({"reminderLabel": "Pills"}) == "Pills"
    assert notif.notification_label(None) is None


@pytest.mark.parametrize("word", ["on", "ON", " resume ", "enabled", "active"])
def test_normalize_status_accepts_the_on_synonyms(word):
    assert notif.normalize_status(word) == "ON"


@pytest.mark.parametrize("word", ["off", "OFF", "pause", "paused", "disabled"])
def test_normalize_status_accepts_the_off_synonyms(word):
    assert notif.normalize_status(word) == "OFF"


@pytest.mark.parametrize("word", ["", None, "maybe", "1"])
def test_normalize_status_refuses_anything_else(word):
    with pytest.raises(ValueError, match="status must be on or off"):
        notif.normalize_status(word)


# ── resolution ───────────────────────────────────────────────────────────


def test_find_notifications_matches_an_exact_id_first():
    other = _alarm(notificationIndex="alarm-2", originalLabel="alarm-1")
    hits = notif.find_notifications([_alarm(), other], "alarm-1")
    assert [notif.notification_id(h) for h in hits] == ["alarm-1"]


def test_find_notifications_falls_back_to_an_exact_then_normalized_label():
    assert notif.find_notifications([_alarm()], "Wake up")
    assert notif.find_notifications([_alarm()], "  wake   UP ")


def test_find_notifications_ignores_non_dict_entries_and_empty_targets():
    assert notif.find_notifications(["nope", None, _alarm()], "") == []
    assert notif.find_notifications(None, "alarm-1") == []


def test_find_notifications_survives_a_record_with_no_label_at_all():
    """A labelless record must compare as "" rather than blowing up the scan."""
    bare = {"notificationIndex": "alarm-9", "type": "Alarm"}
    assert notif.find_notifications([bare, _alarm()], "wake up") == [_alarm()]


def test_notification_choices_skips_junk_and_records_with_no_id():
    choices = notif.notification_choices([_alarm(), "junk", {"type": "Alarm"}, _timer()])
    assert choices == ["alarm-1 (Wake up)", "timer-1 (Pasta)"]


def test_notification_choices_falls_back_to_the_bare_id_when_unlabelled():
    assert notif.notification_choices([{"id": "alarm-9"}]) == ["alarm-9"]


def test_resolve_notification_returns_the_single_match():
    assert notif.resolve_notification([_alarm(), _timer()], "Pasta")["type"] == "Timer"


def test_resolve_notification_refuses_an_ambiguous_label_and_lists_ids():
    twin = _alarm(notificationIndex="alarm-2")
    with pytest.raises(ValueError, match="matches 2 notifications") as err:
        notif.resolve_notification([_alarm(), twin], "Wake up")
    assert "alarm-1" in str(err.value) and "alarm-2" in str(err.value)


def test_resolve_notification_names_the_alternatives_when_nothing_matches():
    with pytest.raises(ValueError, match="no notification matching 'nope'") as err:
        notif.resolve_notification([_alarm()], "nope")
    assert "alarm-1 (Wake up)" in str(err.value)


def test_resolve_notification_says_so_when_the_account_has_none():
    with pytest.raises(ValueError, match="no alarms, timers or reminders"):
        notif.resolve_notification([], "anything")


# ── local wall-clock fields ──────────────────────────────────────────────


def test_local_fields_uses_the_named_timezone():
    # 2026-07-01T12:00:00Z is 13:00 in London (BST).
    fields = notif.local_fields(1782907200000, "Europe/London")
    assert fields == {
        "originalDate": "2026-07-01",
        "originalTime": "13:00:00.000",
        "tz": "Europe/London",
    }


def test_local_fields_falls_back_to_utc_and_says_so():
    for tz in (None, "", "Mars/Olympus_Mons"):
        fields = notif.local_fields(1782907200000, tz)
        assert fields["tz"] == "UTC"
        assert fields["originalTime"] == "12:00:00.000"


# ── payload builders ─────────────────────────────────────────────────────


def test_build_status_update_keeps_every_other_field_of_the_record():
    payload = notif.build_status_update(_alarm(), "off")
    assert payload["status"] == "OFF"
    assert payload["rRuleData"] == {"byWeekDays": ["MO"]}
    assert payload["alarmTime"] == ALARM_MS


def test_build_status_update_does_not_mutate_the_source_record():
    record = _alarm()
    notif.build_status_update(record, "off")
    assert record["status"] == "ON"


def test_build_status_update_refuses_an_empty_record():
    with pytest.raises(ValueError, match="empty notification record"):
        notif.build_status_update({}, "off")


def test_build_reschedule_moves_the_local_fields_with_the_alarm_time():
    # 2026-01-02T08:30:00Z
    new_ms = 1767342600000
    payload = notif.build_reschedule(_alarm(), new_ms, "Europe/London")
    assert payload["alarmTime"] == new_ms
    assert payload["originalDate"] == "2026-01-02"
    assert payload["originalTime"] == "08:30:00.000"


def test_build_reschedule_does_not_invent_local_fields_the_record_lacked():
    record = _alarm()
    del record["originalDate"]
    del record["originalTime"]
    payload = notif.build_reschedule(record, ALARM_MS + 60000, "Europe/London")
    assert "originalDate" not in payload
    assert "originalTime" not in payload


def test_build_reschedule_refuses_a_timer():
    with pytest.raises(ValueError, match="no alarmTime to move"):
        notif.build_reschedule(_timer(), ALARM_MS)


def test_build_reschedule_refuses_a_nonsense_time_and_an_empty_record():
    with pytest.raises(ValueError, match="positive epoch-ms"):
        notif.build_reschedule(_alarm(), 0)
    with pytest.raises(ValueError, match="empty notification record"):
        notif.build_reschedule({}, ALARM_MS)


def test_snooze_measures_from_the_alarm_time_when_it_is_still_ahead():
    assert notif.snooze_epoch_ms(_alarm(), 9, NOW_MS) == ALARM_MS + 9 * 60_000


def test_snooze_measures_from_now_when_the_alarm_has_already_fired():
    already = _alarm(alarmTime=NOW_MS - 60_000)
    assert notif.snooze_epoch_ms(already, 10, NOW_MS) == NOW_MS + 10 * 60_000


def test_snooze_handles_a_record_with_no_usable_alarm_time():
    assert notif.snooze_epoch_ms({"alarmTime": None}, 1, NOW_MS) == NOW_MS + 60_000
    assert notif.snooze_epoch_ms({"alarmTime": "soon"}, 1, NOW_MS) == NOW_MS + 60_000


def test_snooze_defaults_to_now_when_no_clock_is_injected():
    with patch("cli_anything.alexa.core.notifications.time.time", return_value=1767247200.0):
        assert notif.snooze_epoch_ms({}, 1) == NOW_MS + 60_000


@pytest.mark.parametrize("bad", [0, -5, "abc", None, float("nan"), float("inf")])
def test_snooze_refuses_a_non_positive_or_non_numeric_span(bad):
    with pytest.raises(ValueError, match="snooze minutes"):
        notif.snooze_epoch_ms(_alarm(), bad, NOW_MS)


def test_build_snooze_defaults_to_amazons_nine_minutes():
    payload = notif.build_snooze(_alarm(), now_ms=NOW_MS)
    assert payload["alarmTime"] == ALARM_MS + notif.DEFAULT_SNOOZE_MINUTES * 60_000


def test_build_snooze_refuses_a_timer():
    with pytest.raises(ValueError, match="cannot be snoozed"):
        notif.build_snooze(_timer(), 5)


# ── diff / rendering / verify ────────────────────────────────────────────


def test_change_summary_reports_only_what_moved():
    before = _alarm()
    after = notif.build_status_update(before, "off")
    assert notif.change_summary(before, after) == {"status": {"from": "ON", "to": "OFF"}}


def test_change_summary_of_an_identical_record_is_empty():
    assert notif.change_summary(_alarm(), _alarm()) == {}


def test_render_epoch_ms_is_timezone_aware_utc():
    assert notif.render_epoch_ms(ALARM_MS) == "2026-01-01T07:00:00+00:00"


@pytest.mark.parametrize("bad", [None, 0, -1, "later"])
def test_render_epoch_ms_of_a_missing_value_is_none(bad):
    assert notif.render_epoch_ms(bad) is None


def test_verify_status_is_none_when_the_record_could_not_be_reread():
    assert notif.verify_status(None, {"status": "OFF"}) is None


def test_verify_status_compares_every_expected_field():
    assert notif.verify_status({"status": "OFF"}, {"status": "OFF"}) is True
    assert notif.verify_status({"status": "ON"}, {"status": "OFF"}) is False


# ── live operations ──────────────────────────────────────────────────────


def test_fetch_notifications_returns_raw_records_and_drops_junk():
    fake = _fake_api([_alarm(), "junk", None])
    with patch("alexapy.AlexaAPI", fake):
        records = _run(notif.fetch_notifications(MagicMock()))
    assert [r["notificationIndex"] for r in records] == ["alarm-1"]


def test_list_notifications_returns_display_rows_not_raw_records():
    fake = _fake_api([_alarm()])
    with patch("alexapy.AlexaAPI", fake):
        rows = _run(notif.list_notifications(MagicMock()))
    assert rows == [
        {
            "id": "alarm-1",
            "type": "Alarm",
            "status": "ON",
            "label": "Wake up",
            "deviceSerial": "SN1",
            "alarmTime": ALARM_MS,
            "remaining": None,
        }
    ]


def test_show_notification_returns_the_row_plus_the_raw_record():
    fake = _fake_api([_alarm()])
    with patch("alexapy.AlexaAPI", fake):
        row = _run(notif.show_notification(MagicMock(), "Wake up"))
    assert row["id"] == "alarm-1"
    assert row["alarmTimeUtc"] == "2026-01-01T07:00:00+00:00"
    assert row["raw"]["rRuleData"] == {"byWeekDays": ["MO"]}


def test_plan_update_for_a_pause_diffs_the_status_without_writing():
    fake = _fake_api([_alarm()])
    with patch("alexapy.AlexaAPI", fake):
        plan = _run(notif.plan_update(MagicMock(), "alarm-1", status="off"))
    assert plan["change"] == {"status": {"from": "ON", "to": "OFF"}}
    assert plan["payload"]["status"] == "OFF"
    fake.set_notifications.assert_not_awaited()


def test_plan_update_for_a_reschedule_uses_the_devices_own_timezone():
    fake = _fake_api([_alarm()])
    with patch("alexapy.AlexaAPI", fake):
        plan = _run(notif.plan_update(MagicMock(), "alarm-1", at_epoch_ms=1782907200000))
    assert plan["tz"] == "Europe/London"
    assert plan["payload"]["originalTime"] == "13:00:00.000"


def test_plan_update_falls_back_to_utc_when_preferences_cannot_be_read():
    fake = _fake_api([_alarm()])
    fake.get_device_preferences = AsyncMock(side_effect=RuntimeError("throttled"))
    with patch("alexapy.AlexaAPI", fake):
        plan = _run(notif.plan_update(MagicMock(), "alarm-1", at_epoch_ms=1782907200000))
    assert plan["tz"] == "UTC"
    assert plan["payload"]["originalTime"] == "12:00:00.000"


def test_plan_update_falls_back_to_utc_for_a_record_with_no_device():
    fake = _fake_api([_alarm(deviceSerialNumber=None)])
    with patch("alexapy.AlexaAPI", fake):
        plan = _run(notif.plan_update(MagicMock(), "alarm-1", snooze_minutes=5, now_ms=NOW_MS))
    assert plan["tz"] == "UTC"
    fake.get_device_preferences.assert_not_awaited()


def test_plan_update_for_a_snooze_moves_the_alarm_time():
    fake = _fake_api([_alarm()])
    with patch("alexapy.AlexaAPI", fake):
        plan = _run(notif.plan_update(MagicMock(), "alarm-1", snooze_minutes=9, now_ms=NOW_MS))
    assert plan["change"]["alarmTime"]["to"] == ALARM_MS + 9 * 60_000
    assert plan["alarmTimeUtc"] == "2026-01-01T07:09:00+00:00"


def test_plan_update_with_no_change_requested_is_refused():
    fake = _fake_api([_alarm()])
    with patch("alexapy.AlexaAPI", fake), pytest.raises(ValueError, match="nothing to change"):
        _run(notif.plan_update(MagicMock(), "alarm-1"))


def test_plan_update_refuses_rescheduling_a_timer_before_any_write():
    fake = _fake_api([_timer()])
    with patch("alexapy.AlexaAPI", fake), pytest.raises(ValueError, match="no alarmTime to move"):
        _run(notif.plan_update(MagicMock(), "timer-1", at_epoch_ms=ALARM_MS))
    fake.set_notifications.assert_not_awaited()


def test_apply_update_verifies_by_rereading_what_amazon_holds():
    paused = _alarm(status="OFF")
    fake = _fake_api([_alarm()], after=[paused])
    with patch("alexapy.AlexaAPI", fake):
        login = MagicMock()
        plan = _run(notif.plan_update(login, "alarm-1", status="off"))
        result = _run(notif.apply_update(login, plan))
    assert result["ok"] is True
    assert result["status"] == "OFF"
    assert result["note"] is None
    fake.set_notifications.assert_awaited_once()
    assert fake.set_notifications.await_args.args[1]["status"] == "OFF"


def test_apply_update_reports_false_when_amazon_kept_the_old_value():
    fake = _fake_api([_alarm()], after=[_alarm()])
    with patch("alexapy.AlexaAPI", fake):
        login = MagicMock()
        plan = _run(notif.plan_update(login, "alarm-1", status="off"))
        result = _run(notif.apply_update(login, plan))
    assert result["ok"] is False


def test_apply_update_reports_unknown_not_failure_when_the_verify_read_dies():
    fake = _fake_api([_alarm()])
    with patch("alexapy.AlexaAPI", fake):
        login = MagicMock()
        plan = _run(notif.plan_update(login, "alarm-1", status="off"))
        fake.get_notifications = AsyncMock(side_effect=RuntimeError("rate exceeded"))
        result = _run(notif.apply_update(login, plan))
    assert result["ok"] is None
    assert "verify" in result["note"]


def test_apply_update_reports_unknown_when_the_record_vanished_from_the_reread():
    fake = _fake_api([_alarm()], after=[])
    with patch("alexapy.AlexaAPI", fake):
        login = MagicMock()
        plan = _run(notif.plan_update(login, "alarm-1", status="off"))
        result = _run(notif.apply_update(login, plan))
    assert result["ok"] is None


def test_apply_update_refuses_a_plan_with_no_payload():
    with pytest.raises(ValueError, match="no edit payload"):
        _run(notif.apply_update(MagicMock(), {"id": "alarm-1"}))


def test_set_status_plans_and_applies_in_one_call():
    fake = _fake_api([_alarm()], after=[_alarm(status="OFF")])
    with patch("alexapy.AlexaAPI", fake):
        result = _run(notif.set_status(MagicMock(), "alarm-1", "off"))
    assert result["ok"] is True


def test_reschedule_plans_and_applies_in_one_call():
    moved = _alarm(alarmTime=1782907200000, originalDate="2026-07-01", originalTime="13:00:00.000")
    fake = _fake_api([_alarm()], after=[moved])
    with patch("alexapy.AlexaAPI", fake):
        result = _run(notif.reschedule(MagicMock(), "alarm-1", 1782907200000))
    assert result["ok"] is True
    assert result["alarmTimeUtc"] == "2026-07-01T12:00:00+00:00"


def test_snooze_plans_and_applies_in_one_call():
    """Default snooze, with the clock pinned before the alarm so it moves from
    the alarm's own time (the verify compares every field the plan changed)."""
    snoozed = _alarm(alarmTime=ALARM_MS + 9 * 60_000, originalTime="07:09:00.000")
    fake = _fake_api([_alarm()], after=[snoozed])
    with (
        patch("alexapy.AlexaAPI", fake),
        patch("cli_anything.alexa.core.notifications.time.time", return_value=NOW_MS / 1000),
    ):
        result = _run(notif.snooze(MagicMock(), "alarm-1"))
    assert result["ok"] is True
    assert result["alarmTimeUtc"] == "2026-01-01T07:09:00+00:00"
