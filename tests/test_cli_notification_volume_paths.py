"""Behavioural tests for the notification-volume CLI paths (0.14.0).

`echos notification-volume` (the account-wide read, filtered per device) and
`echos set-notification-volume` (the write, dry-run by default).  Assertions
are on observable behaviour — exit code, JSON on stdout, which core coroutine
was invoked with what — never on source text.  The write obeys the
harness-wide contract: **preview by default, act only on --yes**, and bad
volumes are refused *before* login, identically with and without `--yes`.
"""

from __future__ import annotations

import contextlib
import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli_anything.alexa.alexa_cli import cli
from cli_anything.alexa.core import devices_meta as devices_meta_core


def _invoke(args, obj=None):
    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()):
        result = CliRunner().invoke(cli, ["--json", *args], obj=obj or {}, catch_exceptions=False)
    return result


def _ok(args):
    result = _invoke(args)
    assert result.exit_code == 0, result.output
    return result


_ROWS = [
    {"device": "Kitchen", "serial": "SN1", "volume": 40, "softwareVersion": "1234567890"},
    {"device": "Bedroom", "serial": "SN2", "volume": 85, "softwareVersion": "9876543210"},
]


@contextlib.contextmanager
def _stub_run(queue=None):
    """Patch ``_run``: closes the coroutine it is handed, pops the queue."""

    values = queue or []

    def fake_run(_ctx, coro):
        if hasattr(coro, "close"):
            coro.close()
        return values.pop(0) if values else {}

    with patch("cli_anything.alexa.alexa_cli._run", side_effect=fake_run) as mock_run:
        yield mock_run


# ── the read ─────────────────────────────────────────────────────────────


def test_notification_volume_lists_every_echo():
    with _stub_run([[dict(r) for r in _ROWS]]):
        result = _ok(["echos", "notification-volume"])
    rows = json.loads(result.output)
    assert [r["volume"] for r in rows] == [40, 85]


def test_notification_volume_filters_to_one_device():
    with _stub_run([[dict(r) for r in _ROWS]]):
        rows = json.loads(_ok(["echos", "notification-volume", "Bedroom"]).output)
    assert [r["serial"] for r in rows] == ["SN2"]


def test_notification_volume_unknown_device_aborts():
    with _stub_run([[dict(r) for r in _ROWS]]):
        result = _invoke(["echos", "notification-volume", "Garage"])
    assert result.exit_code == 1
    assert "no device matching" in result.output


# ── the write ────────────────────────────────────────────────────────────


def test_set_notification_volume_previews_without_yes():
    parsed = json.loads(_ok(["echos", "set-notification-volume", "Kitchen", "50"]).output)
    assert parsed == {
        "dry_run": True,
        "device": "Kitchen",
        "volume": 50,
        "hint": "re-run with --yes to execute",
    }


def test_set_notification_volume_executes_with_yes():
    row = {
        "device": "Kitchen",
        "serial": "SN1",
        "volume": 50,
        "sent": True,
        "observed": 50,
        "ok": True,
    }
    with _stub_run([row]) as run:
        result = _ok(["echos", "set-notification-volume", "Kitchen", "50", "--yes"])
    assert json.loads(result.output) == row
    assert "set_notification_volume" in run.call_args[0][1].__name__


@pytest.mark.parametrize("level", ["30.5", "101", "nan", "inf"])
def test_set_notification_volume_bad_level_refused_before_login(level):
    def _must_not_login(*_a, **_k):
        raise AssertionError("must not login before validating")

    with patch(
        "cli_anything.alexa.alexa_cli._login", side_effect=_must_not_login
    ):
        result = CliRunner().invoke(
            cli,
            ["--json", "echos", "set-notification-volume", "Kitchen", level],
            catch_exceptions=False,
        )
    assert result.exit_code == 1
    assert "notification volume" in result.output


@pytest.mark.parametrize("level", ["loud", "5x"])
def test_set_notification_volume_non_numeric_level_refused_by_click(level):
    result = _invoke(["echos", "set-notification-volume", "Kitchen", level])
    assert result.exit_code != 0
    assert "dry_run" not in result.output


# ── workflow: read → set → read ──────────────────────────────────────────


def test_volume_workflow_read_then_set_then_read():
    """The whole loop a user runs: see it, change it, confirm the change."""
    queue: list = [
        [dict(r) for r in _ROWS],  # first read
        {  # the write's verified answer
            "device": "Kitchen",
            "serial": "SN1",
            "volume": 50,
            "sent": True,
            "observed": 50,
            "ok": True,
        },
        [dict(_ROWS[0], volume=50), dict(_ROWS[1])],  # the confirming read
    ]
    with _stub_run(queue):
        before = json.loads(_ok(["echos", "notification-volume", "Kitchen"]).output)
        changed = json.loads(_ok(["echos", "set-notification-volume", "Kitchen", "50", "--yes"]).output)
        after = json.loads(_ok(["echos", "notification-volume"]).output)
    assert before[0]["volume"] == 40
    assert changed["ok"] is True
    assert after[0]["volume"] == 50
    assert queue == []  # every staged answer was consumed
