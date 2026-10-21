"""Unit tests for the push-event stream core (`core/events.py`).

After login, Amazon keeps a WebSocket open on ``wss://dp-gw-na.<domain>/tcomm/``
and PUSHES device events out live — the one surface in this harness that is
push rather than pull (0.13.0 wires the 0.12.0 module into the CLI as
``events status`` / ``events listen``).

Covered here:

* the pure half — ``websocket_url`` (alexapy's subdomain choice: the
  ``dp-gw-na-js`` carrier for ``amazon.com``), ``mac_dms_ready`` (the
  device-signing key pre-check, both halves required), ``channel_name``
  (GW channel → readable name, unknown stays reportable as raw hex, junk
  never raises), ``event_row`` (type-safe flattening of an alexapy
  ``Message``), ``filter_events`` / ``json_blob_row`` (case-insensitive
  client-side ``--service``/``--channel``/``--contains`` selection),
  ``listen_summary`` and ``validate_listen_options`` (validate BEFORE the
  session loads, per the harness-wide rule).
* the network half — ``listen_events`` run against a fake
  ``WebsocketEchoClient`` (patched at ``alexapy.alexawebsocket``): connection
  + collection, junk rows skipped, the ``--max-events`` cap, the timeout,
  the always-closes contract, errors landing in the summary (never a silent
  "no events"), the up-front mac_dms refusal (no socket ever opened) and the
  missing-alexapy refusal.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cli_anything.alexa.core import events
from cli_anything.alexa.core.session import AlexaSessionError


# ── fixtures ─────────────────────────────────────────────────────────────


def _content(**over):
    """A FABE-style ``Content`` object (attribute access, like alexapy's)."""
    base = {
        "message_type": "GWM",
        "submessage_type": "CHG",
        "channel": 0x362,
        "device_id_urn": "urn:dss:device:123",
        "payload": {"state": "on"},
    }
    base.update(over)
    return SimpleNamespace(**base)


def _message(**over):
    """An alexapy ``Message``-shaped object (duck-typed the same fields)."""
    base = {
        "service": "FABE",
        "content": _content(),
        "json_payload": {},
        "message_type": "GWM",
        "channel": 0x362,
    }
    base.update(over)
    return SimpleNamespace(**base)


class _FakeWebsocket:
    """A websocket handle whose ``close`` records being awaited."""

    def __init__(self):
        self.closed_by_close = False

    async def close(self):
        self.closed_by_close = True


class _ResistantWebsocket(_FakeWebsocket):
    """The peer went away already — the close handshake itself fails."""

    async def close(self):
        raise RuntimeError("already dead")


def _fake_client_cls(
    messages=(), mode="close", raise_error=None, websocket_cls=_FakeWebsocket
):
    """Fake ``WebsocketEchoClient``: records construction, drives callbacks.

    Mirrors alexapy's real shape: ``async_run`` connects (opens), then
    messages are fed from a BACKGROUND task while the caller polls — so the
    wrapper's cap / timeout / stop conditions are exercised between
    messages, not after all of them at once.
    """
    constructed = []

    class FakeClient:
        def __init__(
            self, login, msg_callback=None, open_callback=None, close_callback=None,
            error_callback=None,
        ):
            self.login = login
            self.msg_callback = msg_callback
            self.open_callback = open_callback
            self.close_callback = close_callback
            self.error_callback = error_callback
            self.websocket = websocket_cls()
            self.messages = list(messages)
            self.mode = mode
            self.raise_error = raise_error
            constructed.append(self)

        async def _feed(self):
            for message in self.messages:
                if getattr(self.websocket, "closed_by_close", False):
                    break
                await self.msg_callback(message)
                await asyncio.sleep(0)  # yield so the wrapper's poll loop runs
            if self.mode == "close":
                await self.close_callback()

        async def async_run(self):
            if self.raise_error is not None:
                raise self.raise_error
            await self.open_callback()
            asyncio.get_event_loop().create_task(self._feed())

    FakeClient.constructed = constructed
    return FakeClient


def _listen(client_cls, login=None, **kwargs):
    """Run ``listen_events`` with the fake client patched in."""
    with patch("alexapy.alexawebsocket.WebsocketEchoClient", client_cls):
        summary = asyncio.run(events.listen_events(login or SimpleNamespace(_ok=True), **kwargs))
    return summary, client_cls.constructed


def _ready_login(**over):
    dms = {"adp_token": "tok", "device_private_key": "key"}
    dms.update(over)
    return SimpleNamespace(mac_dms=dms)


# ── websocket_url ────────────────────────────────────────────────────────


def test_websocket_url_default_region_uses_dp_gw_na():
    assert events.websocket_url() == "wss://dp-gw-na.amazon.co.uk/tcomm/"


def test_websocket_url_amazon_com_uses_the_js_gateway():
    assert events.websocket_url("amazon.com") == "wss://dp-gw-na-js.amazon.com/tcomm/"


def test_websocket_url_other_regions_use_dp_gw_na():
    assert events.websocket_url("amazon.de") == "wss://dp-gw-na.amazon.de/tcomm/"


def test_websocket_url_refuses_an_unknown_region():
    with pytest.raises(AlexaSessionError):
        events.websocket_url("amazon.invalid")


# ── mac_dms_ready ────────────────────────────────────────────────────────


def test_mac_dms_ready_true_only_when_both_halves_are_present():
    assert events.mac_dms_ready(_ready_login()) is True


@pytest.mark.parametrize(
    "dms",
    [
        {},                                   # nothing at all
        {"adp_token": "tok"},                 # one half only — dies in signature
        {"device_private_key": "key"},        # the other half only
        {"adp_token": "", "device_private_key": "key"},
        {"adp_token": "  ", "device_private_key": "key"},  # present but blank
    ],
)
def test_mac_dms_ready_refuses_half_baked_keys(dms):
    assert events.mac_dms_ready(SimpleNamespace(mac_dms=dms)) is False


@pytest.mark.parametrize("login", [None, SimpleNamespace(), "not-a-login"])
def test_mac_dms_ready_refuses_logins_without_a_key_at_all(login):
    assert events.mac_dms_ready(login) is False


def test_mac_dms_ready_never_lets_a_hostile_login_raise():
    class Hostile:
        @property
        def mac_dms(self):
            raise RuntimeError("boom")

    assert events.mac_dms_ready(Hostile()) is False


# ── channel_name ─────────────────────────────────────────────────────────


def test_channel_name_known_channels():
    assert events.channel_name(0x361) == "handshake"
    assert events.channel_name(0x362) == "events"
    assert events.channel_name(0x65) == "heartbeat"


def test_channel_name_accepts_hex_strings_and_whole_floats():
    assert events.channel_name("362") == "events"
    assert events.channel_name(866.0) == "events"


def test_channel_name_unknown_channel_stays_reportable_as_hex():
    assert events.channel_name(0xB479) == "0xb479"


def test_channel_name_junk_returns_none_and_never_raises():
    assert events.channel_name(None) is None
    assert events.channel_name(True) is None
    assert events.channel_name(7.5) is None
    assert events.channel_name("") is None
    assert events.channel_name("not-hex") is None
    assert events.channel_name(object()) is None


# ── event_row ────────────────────────────────────────────────────────────


def test_event_row_flattens_a_fabe_gwm_message():
    row = events.event_row(_message())
    assert row is not None
    assert row["service"] == "FABE"
    assert row["channel"] == "events"
    assert row["messageType"] == "GWM"
    assert row["type"] == "CHG"          # the submessage kind wins when present
    assert row["device"] == "urn:dss:device:123"
    assert row["payload"] == {"state": "on"}


def test_event_row_prefers_a_decoded_json_payload():
    row = events.event_row(_message(json_payload={"decoded": True}))
    assert row["payload"] == {"decoded": True}


def test_event_row_drops_the_submessage_type_when_absent():
    content = _content(submessage_type=None)
    row = events.event_row(_message(content=content))
    assert row["type"] == "GWM"


def test_event_row_handles_a_tune_dict_content():
    row = events.event_row(
        _message(service="TUNE", content={"payload": "tune-payload"}, channel=0)
    )
    assert row is not None
    assert row["service"] == "TUNE"
    assert row["payload"] == "tune-payload"
    assert row["device"] is None          # a tuning frame names no device


def test_event_row_junk_is_skipped_not_crashed():
    assert events.event_row(None) is None
    assert events.event_row(SimpleNamespace()) is None          # no service
    assert events.event_row(_message(service="")) is None       # empty service
    assert events.event_row("not a message") is None
    assert events.event_row(_message(content=None, json_payload=None)) is not None


def test_event_row_prefers_the_content_channel_over_the_frame_channel():
    row = events.event_row(_message(content=_content(channel=0xB479)))
    assert row["channel"] == "0xb479"     # unknown but seen — kept honest


# ── filter_events / json_blob_row ────────────────────────────────────────


def _rows():
    return [
        {"service": "FABE", "channel": "events", "type": "CHG",
         "device": "urn:dss:device:123", "payload": {"color": "blue"}},
        {"service": "TUNE", "channel": "0xb479", "type": "CHG",
         "device": "urn:dss:device:999", "payload": "tuning"},
    ]


def test_filter_events_without_filters_returns_the_rows_unchanged():
    rows = _rows()
    out = events.filter_events(rows)
    assert out == rows
    assert out is not rows  # a copy, not the caller's list


def test_filter_events_by_service_is_case_insensitive():
    assert [r["service"] for r in events.filter_events(_rows(), service="fab")] == ["FABE"]


def test_filter_events_by_channel():
    assert [r["service"] for r in events.filter_events(_rows(), channel="events")] == ["FABE"]
    assert [r["service"] for r in events.filter_events(_rows(), channel="0xb479")] == ["TUNE"]


def test_filter_events_by_contains_reaches_into_payload_json():
    assert [r["device"] for r in events.filter_events(_rows(), contains="blue")] == [
        "urn:dss:device:123"
    ]
    # case-insensitive over the device urn too
    assert len(events.filter_events(_rows(), contains="URN:DSS")) == 2


def test_filter_events_combines_filters():
    out = events.filter_events(_rows(), service="fabe", channel="events", contains="blue")
    assert len(out) == 1
    assert out[0]["device"] == "urn:dss:device:123"


def test_filter_events_combined_filters_that_match_nothing():
    assert events.filter_events(_rows(), service="fabe", channel="0xb479") == []


def test_filter_events_empty_input_is_a_no_op():
    assert events.filter_events([], service="FABE") == []


# ── listen_summary ───────────────────────────────────────────────────────


def test_listen_summary_separates_count_from_events():
    rows = [{"a": 1}, {"b": 2}]
    summary = events.listen_summary(rows, True, 1.234)
    assert summary == {
        "connected": True,
        "duration": 1.2,
        "count": 2,
        "events": rows,
        "error": None,
    }


def test_listen_summary_never_reports_a_negative_duration():
    assert events.listen_summary([], False, -5)["duration"] == 0.0


# ── validate_listen_options ──────────────────────────────────────────────


def test_validate_listen_options_defaults():
    assert events.validate_listen_options() == (events.DEFAULT_LISTEN_TIMEOUT, None)


def test_validate_listen_options_accepts_given_values():
    assert events.validate_listen_options(5, 10) == (5.0, 10)


@pytest.mark.parametrize("timeout", [-1, "abc", object()])
def test_validate_listen_options_refuses_bad_timeouts(timeout):
    with pytest.raises(ValueError, match="--timeout"):
        events.validate_listen_options(timeout)


def test_validate_listen_options_treats_none_as_the_default():
    assert events.validate_listen_options(None) == (events.DEFAULT_LISTEN_TIMEOUT, None)


@pytest.mark.parametrize("cap", [0, -3, "abc"])
def test_validate_listen_options_refuses_bad_caps(cap):
    with pytest.raises(ValueError, match="--max-events"):
        events.validate_listen_options(1.0, cap)


def test_validate_listen_options_tolerates_an_int_shaped_string_cap():
    assert events.validate_listen_options(1.0, "5") == (1.0, 5)


# ── client_available ─────────────────────────────────────────────────────


def test_client_available_true_with_alexapy_installed():
    assert events.client_available() is True


def test_client_available_false_without_alexapy(monkeypatch):
    monkeypatch.setitem(sys.modules, "alexapy.alexawebsocket", None)
    assert events.client_available() is False


# ── listen_events (fake WebsocketEchoClient) ─────────────────────────────


def test_listen_events_collects_rows_and_closes():
    fake = _fake_client_cls(messages=[_message(), _message(content=_content(device_id_urn="urn:dss:device:2"))])
    summary, constructed = _listen(fake, login=_ready_login())
    assert len(constructed) == 1                      # built with the login
    assert constructed[0].login is not None
    assert summary["connected"] is True
    assert summary["error"] is None
    assert summary["count"] == 2
    assert [r["device"] for r in summary["events"]] == ["urn:dss:device:123", "urn:dss:device:2"]


def test_listen_events_skips_undecodable_messages():
    fake = _fake_client_cls(messages=[_message(), "junk", None])
    summary, _ = _listen(fake, login=_ready_login())
    assert summary["count"] == 1


def test_listen_events_stops_on_the_max_events_cap():
    fake = _fake_client_cls(messages=[_message() for _ in range(6)], mode="stay_open")
    with patch.object(events, "LISTEN_POLL_SECONDS", 0.0):
        summary, _ = _listen(fake, login=_ready_login(), timeout=5.0, max_events=2)
    assert summary["count"] == 2
    assert summary["connected"] is True


def test_listen_events_honours_the_timeout():
    with patch.object(events, "LISTEN_POLL_SECONDS", 0.0):
        fake = _fake_client_cls(messages=[_message()], mode="stay_open")
        summary, _ = _listen(fake, login=_ready_login(), timeout=0.05)
    assert summary["count"] == 1
    assert summary["duration"] < 5.0


def test_listen_events_always_closes_the_socket():
    fake = _fake_client_cls(messages=[_message()], mode="stay_open")
    summary, constructed = _listen(fake, login=_ready_login(), timeout=0.05)
    assert summary["count"] == 1
    # the wrapper closes the socket itself when the peer never did
    assert constructed[0].websocket.closed_by_close is True


def test_listen_events_survives_a_socket_that_resists_closing():
    fake = _fake_client_cls(
        messages=[_message()], mode="close", websocket_cls=_ResistantWebsocket
    )
    summary, constructed = _listen(fake, login=_ready_login())
    # a close hiccup must not mask the collected rows
    assert summary["count"] == 1
    assert summary["error"] is None
    assert len(constructed) == 1


def test_listen_events_reports_a_protocol_failure_in_the_summary():
    fake = _fake_client_cls(raise_error=RuntimeError("boom"))
    summary, _ = _listen(fake, login=_ready_login())
    assert summary["connected"] is False
    assert "RuntimeError: boom" in summary["error"]


def test_listen_events_refuses_a_login_without_mac_dms_before_any_socket():
    fake = _fake_client_cls(messages=[_message()])
    with pytest.raises(AlexaSessionError, match="mac_dms"):
        with patch("alexapy.alexawebsocket.WebsocketEchoClient", fake):
            asyncio.run(events.listen_events(SimpleNamespace(mac_dms={}), timeout=1))
    assert fake.constructed == []  # never built the client


def test_listen_events_refuses_a_missing_alexapy_websocket_module(monkeypatch):
    monkeypatch.setitem(sys.modules, "alexapy.alexawebsocket", None)
    with pytest.raises(AlexaSessionError, match="alexapy"):
        asyncio.run(events.listen_events(_ready_login()))
