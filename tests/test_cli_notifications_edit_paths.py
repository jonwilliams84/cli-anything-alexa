"""Behavioural tests for the notification-edit + introspection CLI paths.

Covers `notifications show / pause / resume / reschedule / snooze`,
`auth whoami` and `echos preferences / wifi`.  Every mutating command is held
to the harness-wide contract — **preview by default, act only on --yes**, and
validate BEFORE the login/network call so bad input fails identically either
way — and every assertion is on observable behaviour (exit code, JSON on
stdout, which core coroutine was invoked), never on source text.
"""

from __future__ import annotations

import contextlib
import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli_anything.alexa.alexa_cli import cli
from cli_anything.alexa.core import devices_meta as devices_meta_core
from cli_anything.alexa.core import notifications as notifications_core

PLAN = {
    "id": "alarm-1",
    "type": "Alarm",
    "label": "Wake up",
    "deviceSerial": "SN1",
    "tz": "Europe/London",
    "alarmTimeUtc": "2026-01-01T07:00:00+00:00",
    "change": {"status": {"from": "ON", "to": "OFF"}},
    "payload": {"notificationIndex": "alarm-1", "status": "OFF"},
    "before": {"notificationIndex": "alarm-1", "status": "ON"},
}

APPLIED = {"id": "alarm-1", "ok": True, "status": "OFF", "change": PLAN["change"]}

#: (argv, the change kwarg `plan_update` must be asked for)
EDITS = [
    (["notifications", "pause", "alarm-1"], "status"),
    (["notifications", "resume", "alarm-1"], "status"),
    (["notifications", "reschedule", "alarm-1", "--in", "60"], "at_epoch_ms"),
    (["notifications", "snooze", "alarm-1"], "snooze_minutes"),
]


def _invoke(args, obj=None):
    return CliRunner().invoke(cli, args, obj=obj or {}, catch_exceptions=False)


@contextlib.contextmanager
def _stub_run(*return_values):
    """Patch ``_run`` to answer each call in turn, closing the coroutines."""
    answers = list(return_values)

    def fake_run(_ctx, coro):
        if hasattr(coro, "close"):
            coro.close()
        return answers.pop(0) if answers else None

    with (
        patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()),
        patch("cli_anything.alexa.alexa_cli._run", side_effect=fake_run) as mock_run,
    ):
        yield mock_run


@contextlib.contextmanager
def _stub_core(module, name):
    with patch.object(module, name, MagicMock()) as stub:
        yield stub


# ── the dry-run contract ────────────────────────────────────────────────


@pytest.mark.parametrize(("argv", "_kw"), EDITS, ids=lambda v: str(v)[:40])
def test_edits_preview_the_diff_without_yes(argv, _kw):
    with _stub_run(PLAN):
        result = _invoke(["--json", *argv])
    parsed = json.loads(result.output)
    assert result.exit_code == 0
    assert parsed["dry_run"] is True
    assert parsed["change"] == {"status": {"from": "ON", "to": "OFF"}}
    assert "--yes" in parsed["hint"]


@pytest.mark.parametrize(("argv", "_kw"), EDITS, ids=lambda v: str(v)[:40])
def test_the_dry_run_hides_the_raw_record_and_shows_the_readable_diff(argv, _kw):
    """The whole-record payload is 30 keys of noise; the review is the diff."""
    with _stub_run(PLAN):
        parsed = json.loads(_invoke(["--json", *argv]).output)
    assert "payload" not in parsed
    assert "before" not in parsed
    assert parsed["id"] == "alarm-1"


@pytest.mark.parametrize(("argv", "_kw"), EDITS, ids=lambda v: str(v)[:40])
def test_edits_do_not_apply_anything_without_yes(argv, _kw):
    with _stub_run(PLAN), _stub_core(notifications_core, "apply_update") as apply_stub:
        assert _invoke(["--json", *argv]).exit_code == 0
    apply_stub.assert_not_called()


@pytest.mark.parametrize(("argv", "_kw"), EDITS, ids=lambda v: str(v)[:40])
def test_edits_apply_the_planned_payload_with_yes(argv, _kw):
    with _stub_run(PLAN, APPLIED), _stub_core(notifications_core, "apply_update") as apply_stub:
        result = _invoke(["--json", *argv, "--yes"])
    assert result.exit_code == 0
    assert json.loads(result.output)["ok"] is True
    assert apply_stub.call_args.args[1] is PLAN


# ── what each edit asks the core to plan ────────────────────────────────


@pytest.mark.parametrize(("argv", "kwarg"), EDITS, ids=lambda v: str(v)[:40])
def test_each_edit_plans_exactly_one_kind_of_change(argv, kwarg):
    with _stub_run(PLAN), _stub_core(notifications_core, "plan_update") as plan_stub:
        assert _invoke(["--json", *argv]).exit_code == 0
    kwargs = plan_stub.call_args.kwargs
    assert list(kwargs) == [kwarg]
    assert plan_stub.call_args.args[1] == "alarm-1"


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["notifications", "pause", "alarm-1"], "off"),
        (["notifications", "resume", "alarm-1"], "on"),
    ],
    ids=["pause", "resume"],
)
def test_pause_and_resume_map_to_the_status_words(argv, expected):
    with _stub_run(PLAN), _stub_core(notifications_core, "plan_update") as plan_stub:
        _invoke(["--json", *argv])
    assert plan_stub.call_args.kwargs["status"] == expected


def test_snooze_defaults_to_amazons_own_nine_minutes():
    with _stub_run(PLAN), _stub_core(notifications_core, "plan_update") as plan_stub:
        _invoke(["--json", "notifications", "snooze", "alarm-1"])
    assert plan_stub.call_args.kwargs["snooze_minutes"] == notifications_core.DEFAULT_SNOOZE_MINUTES


def test_snooze_passes_an_explicit_span_through():
    with _stub_run(PLAN), _stub_core(notifications_core, "plan_update") as plan_stub:
        _invoke(["--json", "notifications", "snooze", "alarm-1", "--minutes", "30"])
    assert plan_stub.call_args.kwargs["snooze_minutes"] == 30


def test_reschedule_at_an_absolute_epoch_is_passed_verbatim():
    with _stub_run(PLAN), _stub_core(notifications_core, "plan_update") as plan_stub:
        _invoke(["--json", "notifications", "reschedule", "alarm-1", "--at", "1767250800000"])
    assert plan_stub.call_args.kwargs["at_epoch_ms"] == 1767250800000


def test_reschedule_in_seconds_becomes_an_epoch_in_the_future():
    with (
        _stub_run(PLAN),
        _stub_core(notifications_core, "plan_update") as plan_stub,
        patch("cli_anything.alexa.core.notifications.time.time", return_value=1767247200.0),
    ):
        _invoke(["--json", "notifications", "reschedule", "alarm-1", "--in", "600"])
    assert plan_stub.call_args.kwargs["at_epoch_ms"] == 1767247200000 + 600_000


# ── argument validation (before any network call) ───────────────────────


@pytest.mark.parametrize(
    "argv",
    [
        ["notifications", "reschedule", "alarm-1"],
        ["notifications", "reschedule", "alarm-1", "--in", "60", "--at", "1767250800000"],
    ],
    ids=["neither", "both"],
)
def test_reschedule_demands_exactly_one_of_in_or_at(argv):
    with patch("cli_anything.alexa.alexa_cli._login") as login:
        result = CliRunner().invoke(cli, ["--json", *argv], obj={})
    assert result.exit_code != 0
    assert "exactly one" in result.output
    login.assert_not_called()


@pytest.mark.parametrize(
    "argv",
    [
        ["notifications", "pause"],
        ["notifications", "resume"],
        ["notifications", "snooze"],
        ["notifications", "show"],
    ],
    ids=lambda v: str(v),
)
def test_every_edit_requires_a_target(argv):
    assert CliRunner().invoke(cli, ["--json", *argv], obj={}).exit_code != 0


def test_a_no_op_edit_is_reported_as_such_and_never_written():
    """Pausing an already-paused alarm has an empty diff — say so, do nothing."""
    with (
        _stub_run({"id": "alarm-1", "change": {}}),
        _stub_core(notifications_core, "apply_update") as apply_stub,
    ):
        result = _invoke(["--json", "notifications", "pause", "alarm-1", "--yes"])
    parsed = json.loads(result.output)
    assert parsed["ok"] is True
    assert parsed["change"] == {}
    assert "already" in parsed["note"]
    apply_stub.assert_not_called()


def test_an_unresolvable_target_aborts_with_the_core_message():
    with (
        patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()),
        patch.object(
            notifications_core,
            "plan_update",
            side_effect=ValueError("no notification matching 'nope'"),
        ),
    ):
        result = CliRunner().invoke(cli, ["--json", "notifications", "pause", "nope"], obj={})
    assert result.exit_code != 0
    assert "no notification matching" in result.output


# ── notifications show (read-only) ──────────────────────────────────────


def test_show_is_read_only_and_needs_no_yes():
    row = {"id": "alarm-1", "type": "Alarm", "raw": {"notificationIndex": "alarm-1"}}
    with _stub_run(row), _stub_core(notifications_core, "show_notification") as stub:
        result = _invoke(["--json", "notifications", "show", "Wake up"])
    assert result.exit_code == 0
    assert json.loads(result.output) == row
    assert stub.call_args.args[1] == "Wake up"


# ── auth whoami ─────────────────────────────────────────────────────────


def test_whoami_prints_the_account_identity():
    row = {"authenticated": True, "email": "you@example.com", "customerId": "A1"}
    with _stub_run(row):
        result = _invoke(["--json", "auth", "whoami"])
    assert result.exit_code == 0
    assert json.loads(result.output)["customerId"] == "A1"


def test_whoami_exits_nonzero_when_the_cookie_no_longer_buys_an_account():
    with _stub_run({"authenticated": False}):
        result = CliRunner().invoke(cli, ["--json", "auth", "whoami"], obj={})
    assert result.exit_code == 1
    assert json.loads(result.output)["authenticated"] is False


# ── echos preferences / wifi ────────────────────────────────────────────


PREF_ROWS = [
    {"device": "Kitchen", "serial": "SN1", "timeZoneId": "Europe/London"},
    {"device": "Study", "serial": "SN2", "timeZoneId": "Europe/Berlin"},
]


def test_preferences_lists_every_echo_by_default():
    with _stub_run(PREF_ROWS), _stub_core(devices_meta_core, "fetch_device_preferences") as stub:
        result = _invoke(["--json", "echos", "preferences"])
    assert json.loads(result.output) == PREF_ROWS
    stub.assert_called_once()


@pytest.mark.parametrize("target", ["Study", "study", "SN2"])
def test_preferences_filters_to_one_echo_by_name_or_serial(target):
    with _stub_run(PREF_ROWS):
        result = _invoke(["--json", "echos", "preferences", target])
    assert [r["serial"] for r in json.loads(result.output)] == ["SN2"]


def test_preferences_aborts_on_an_unknown_echo():
    with _stub_run(PREF_ROWS):
        result = CliRunner().invoke(cli, ["--json", "echos", "preferences", "Attic"], obj={})
    assert result.exit_code != 0
    assert "no device matching" in result.output


def test_preferences_renders_a_table_without_json():
    with _stub_run(PREF_ROWS):
        result = _invoke(["echos", "preferences"])
    assert result.exit_code == 0
    assert "Europe/London" in result.output


def test_wifi_defaults_to_the_first_online_echo():
    row = {"device": "Kitchen", "ssid": "home-wifi"}
    with _stub_run(row), _stub_core(devices_meta_core, "fetch_wifi_details") as stub:
        result = _invoke(["--json", "echos", "wifi"])
    assert json.loads(result.output) == row
    assert stub.call_args.args[1] is None


def test_wifi_takes_an_explicit_echo():
    with _stub_run({}), _stub_core(devices_meta_core, "fetch_wifi_details") as stub:
        _invoke(["--json", "echos", "wifi", "Study"])
    assert stub.call_args.args[1] == "Study"
