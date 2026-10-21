"""Behavioural tests for the push-event stream CLI paths (`events` group).

0.13.0 wires the 0.12.0 library module into the CLI as `events status` /
`events listen`. Assertions are on observable behaviour — exit code, JSON on
stdout, and which core coroutine was invoked with what — never on source
text. Both commands are READ-ONLY: neither needs `--yes`, and a listen never
touches device state. Validation (`--timeout`/`--max-events`) must fail
BEFORE `_login`, per the harness-wide rule.
"""

from __future__ import annotations

import contextlib
import json
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from cli_anything.alexa.alexa_cli import cli
from cli_anything.alexa.core import events as events_core


_SUMMARY = {
    "connected": True,
    "duration": 1.2,
    "count": 2,
    "events": [
        {"service": "FABE", "channel": "events", "type": "CHG",
         "device": "urn:dss:device:123", "payload": {"color": "blue"}},
        {"service": "TUNE", "channel": "0xb479", "type": "CHG",
         "device": "urn:dss:device:999", "payload": "tuning"},
    ],
    "error": None,
}


def _ready_login():
    return MagicMock(mac_dms={"adp_token": "tok", "device_private_key": "key"})


def _bare_login():
    return MagicMock(mac_dms={})


def _invoke(args, obj=None):
    return CliRunner().invoke(cli, args, obj=obj or {}, catch_exceptions=False)


def _json_out(result):
    return json.loads(result.output)


def _stubbed_login(login):
    return patch("cli_anything.alexa.alexa_cli._login", return_value=login)


@contextlib.contextmanager
def _stubbed_listen(summary):
    """Patch the core listener; the context manager records the call kwargs."""
    calls = []

    def fake_listen(login, timeout=None, max_events=None):
        calls.append({"timeout": timeout, "max_events": max_events})

        def listen_events():
            return summary

        return listen_events()

    with patch("cli_anything.alexa.core.events.listen_events", side_effect=fake_listen):
        yield calls


# ── events status ────────────────────────────────────────────────────────


def test_events_status_is_json_with_a_ready_session():
    with _stubbed_login(_ready_login()):
        result = _invoke(["--json", "events", "status"])
    assert result.exit_code == 0
    body = _json_out(result)
    assert body["websocket"] == "wss://dp-gw-na.amazon.co.uk/tcomm/"
    assert body["alexapy_client"] is True
    assert body["mac_dms"] is True
    assert body["ready"] is True
    assert "events listen" in body["hint"]


def test_events_status_reports_a_missing_key_as_not_ready():
    with _stubbed_login(_bare_login()):
        result = _invoke(["--json", "events", "status"])
    assert result.exit_code == 0
    body = _json_out(result)
    assert body["ready"] is False
    assert "re-authenticate" in body["hint"]


def test_events_status_text_mode():
    with _stubbed_login(_ready_login()):
        result = _invoke(["events", "status"])
    assert result.exit_code == 0
    assert "wss://dp-gw-na.amazon.co.uk/tcomm/" in result.output


def test_events_status_uses_the_selected_region():
    with _stubbed_login(_ready_login()):
        result = _invoke(["--json", "--url", "amazon.com", "events", "status"])
    assert _json_out(result)["websocket"] == "wss://dp-gw-na-js.amazon.com/tcomm/"


# ── events listen ────────────────────────────────────────────────────────


def test_events_listen_needs_no_yes_and_is_read_only():
    with _stubbed_login(_ready_login()), _stubbed_listen(_SUMMARY) as lp:
        result = _invoke(["--json", "events", "listen"])
    assert result.exit_code == 0
    assert lp == [{"timeout": 30.0, "max_events": None}]
    body = _json_out(result)
    assert body["connected"] is True
    assert body["collected"] == 2
    assert body["shown"] == 2
    assert body["error"] is None
    assert len(body["events"]) == 2
    assert body["events"][0]["payload"] == {"color": "blue"}


def test_events_listen_honors_the_timeout_and_cap_options():
    with _stubbed_login(_ready_login()), _stubbed_listen(_SUMMARY) as lp:
        _invoke(["--json", "events", "listen", "--timeout", "5", "--max-events", "10"])
    assert lp == [{"timeout": 5.0, "max_events": 10}]


def test_events_listen_text_mode_prints_summary_and_rows():
    with _stubbed_login(_ready_login()), _stubbed_listen(_SUMMARY):
        result = _invoke(["events", "listen"])
    assert result.exit_code == 0
    assert "connected" in result.output
    assert "urn:dss:device:123" in result.output
    assert "color" in result.output


def test_events_listen_applies_the_client_side_filters():
    with _stubbed_login(_ready_login()), _stubbed_listen(_SUMMARY):
        result = _invoke(["--json", "events", "listen", "--service", "FABE"])
    body = _json_out(result)
    assert body["shown"] == 1
    assert body["collected"] == 2
    assert [r["service"] for r in body["events"]] == ["FABE"]
    assert body["filters"]["service"] == "FABE"


def test_events_listen_contains_reaches_into_payload_json():
    with _stubbed_login(_ready_login()), _stubbed_listen(_SUMMARY):
        result = _invoke(["--json", "events", "listen", "--contains", "blue"])
    body = _json_out(result)
    assert [r["device"] for r in body["events"]] == ["urn:dss:device:123"]
    assert body["shown"] == 1


def test_events_listen_without_json_still_renders_filters():
    with _stubbed_login(_ready_login()), _stubbed_listen(_SUMMARY):
        result = _invoke(["events", "listen", "--channel", "events"])
    assert result.exit_code == 0
    assert "urn:dss:device:123" in result.output
    assert "urn:dss:device:999" not in result.output


def test_events_listen_reports_a_socket_error_from_the_summary():
    summary = {**_SUMMARY, "connected": False, "count": 0, "events": [],
               "error": "RuntimeError: no gateway"}
    with _stubbed_login(_ready_login()), _stubbed_listen(summary):
        result = _invoke(["--json", "events", "listen"])
    body = _json_out(result)
    assert body["connected"] is False
    assert body["error"] == "RuntimeError: no gateway"


# ── validate BEFORE _login ───────────────────────────────────────────────


def test_events_listen_validates_options_before_the_session_loads():
    with _stubbed_login(_ready_login()) as login_patch, _stubbed_listen(_SUMMARY) as lp:
        result = _invoke(["--json", "events", "listen", "--timeout", "-1"])
    assert result.exit_code == 1
    assert "--timeout must be >= 0" in result.output
    # nothing ran: no session load, no socket
    assert login_patch.return_value is not None
    assert login_patch.called is False
    assert lp == []


def test_events_listen_rejects_a_zero_max_events():
    with _stubbed_login(_ready_login()), _stubbed_listen(_SUMMARY) as lp:
        result = _invoke(["--json", "events", "listen", "--max-events", "0"])
    assert result.exit_code == 1
    assert "--max-events must be at least 1" in result.output
    assert lp == []


def test_events_rejects_a_non_numeric_timeout():
    with _stubbed_login(_ready_login()), _stubbed_listen(_SUMMARY) as lp:
        result = _invoke(["--json", "events", "listen", "--timeout", "soon"])
    # click's own type=float rejects it before the core validator is reached
    assert result.exit_code == 2
    assert lp == []


# ── workflow: status then listen on one session ──────────────────────────


def test_events_status_then_listen_workflow():
    """The documented pairing: check readiness, then watch the stream."""
    login = _ready_login()
    with _stubbed_login(login), _stubbed_listen(_SUMMARY) as lp:
        status = _invoke(["--json", "events", "status"])
        listen = _invoke(["--json", "events", "listen"])
    assert _json_out(status)["ready"] is True
    assert listen.exit_code == 0
    assert _json_out(listen)["collected"] == 2
    # the SAME login object rode both commands
    assert lp == [{"timeout": 30.0, "max_events": None}]


def test_events_commands_are_repl_reachable():
    """The events group is a first-class cli group (the REPL re-invokes it)."""
    assert "events" in cli.commands
    assert set(cli.commands["events"].commands) == {"status", "listen"}
