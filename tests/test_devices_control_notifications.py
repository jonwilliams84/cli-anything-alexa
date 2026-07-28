"""Behavioural tests for the async device, control, and notification operations.

These exercise the real branching logic in ``devices.py``, ``control.py`` and
``notifications.py`` — the phoenix graph normalisation, the csrf-guard error
paths, device-not-found / no-devices errors, and the delete/verify round-trip —
using lightweight fakes instead of a live alexapy session.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_anything.alexa.core import control, devices, notifications
from cli_anything.alexa.core.session import AlexaSessionError


# ── shared fakes ─────────────────────────────────────────────────────────

class _FakeCookie:
    def __init__(self, key: str, value: str):
        self.key = key
        self.value = value


class _FakeCookieJar:
    def __init__(self, cookies: list[_FakeCookie] | None = None):
        self._cookies = cookies or []

    def __iter__(self):
        return iter(self._cookies)


class _FakeResponse:
    """Minimal aiohttp-style async context-manager response."""

    def __init__(self, status: int, body: str = ""):
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Stand-in for the aiohttp ClientSession on a login object."""

    def __init__(self, resp: _FakeResponse, cookies: list[_FakeCookie] | None = None):
        self._resp = resp
        self.cookie_jar = _FakeCookieJar(cookies)
        self.delete = MagicMock(return_value=resp)
        self.post = MagicMock(return_value=resp)
        self.put = MagicMock(return_value=resp)


class _FakeLogin:
    """A login object with just enough surface for the core modules."""

    def __init__(self, url: str = "amazon.co.uk", cookies: list[_FakeCookie] | None = None,
                 resp: _FakeResponse | None = None):
        self.url = url
        self.session = _FakeSession(resp or _FakeResponse(200, ""), cookies)


def _run(coro):
    return asyncio.run(coro)


def _fake_alexapy_api_class(*, api_instance: MagicMock | None = None):
    """Build a stand-in for the ``AlexaAPI`` class used inside control.py.

    ``control.py`` does ``from alexapy import AlexaAPI`` then calls both
    ``AlexaAPI.get_devices(login)`` (async classmethod) and
    ``AlexaAPI(runner, login)`` (constructor).  The returned fake class
    supports both: the classmethod is an AsyncMock and the constructor
    returns ``api_instance``.
    """
    fake_cls = MagicMock()
    fake_cls.get_devices = AsyncMock()
    fake_cls.get_automations = AsyncMock()
    fake_cls.return_value = api_instance or MagicMock()
    return fake_cls


# ── devices.fetch_appliances: phoenix graph normalisation ────────────────

def _phoenix_graph():
    """A realistic nested networkDetail graph with one bridge + two appliances."""
    return {
        "networkDetail": {
            "locationDetails": {
                "amazonBridgeDetails": {
                    "amazonBridgeDetails": {
                        "bridge_1": {
                            "applianceDetails": {
                                "applianceDetails": {
                                    "a1": {"applianceId": "p_light#kitchen", "friendlyName": "Kitchen"},
                                    "a2": {"applianceId": "p_switch#hall", "friendlyName": "Hall"},
                                }
                            }
                        }
                    }
                }
            }
        }
    }


def test_fetch_appliances_extracts_from_nested_graph():
    graph = _phoenix_graph()
    login = _FakeLogin()
    with patch("alexapy.AlexaAPI.get_network_details", new=AsyncMock(return_value=graph)):
        result = _run(devices.fetch_appliances(login))
    ids = {a["applianceId"] for a in result}
    assert ids == {"p_light#kitchen", "p_switch#hall"}


def test_fetch_appliances_empty_data_returns_empty_list():
    login = _FakeLogin()
    with patch("alexapy.AlexaAPI.get_network_details", new=AsyncMock(return_value=None)):
        assert _run(devices.fetch_appliances(login)) == []
    with patch("alexapy.AlexaAPI.get_network_details", new=AsyncMock(return_value={})):
        assert _run(devices.fetch_appliances(login)) == []


def test_fetch_appliances_fallback_appliance_details_map():
    """When the nested graph has no bridges, fall back to the flat applianceDetails map."""
    data = {
        "applianceDetails": {
            "applianceDetails": {
                "x": {"applianceId": "native_hue_1"},
            }
        }
    }
    login = _FakeLogin()
    with patch("alexapy.AlexaAPI.get_network_details", new=AsyncMock(return_value=data)):
        result = _run(devices.fetch_appliances(login))
    assert len(result) == 1
    assert result[0]["applianceId"] == "native_hue_1"


def test_fetch_appliances_list_data_passes_through():
    raw_list = [{"applianceId": "a"}, {"applianceId": "b"}]
    login = _FakeLogin()
    with patch("alexapy.AlexaAPI.get_network_details", new=AsyncMock(return_value=raw_list)):
        result = _run(devices.fetch_appliances(login))
    assert result == raw_list


def test_fetch_appliances_dict_with_no_appliances_returns_empty():
    """A dict that has neither a nested graph nor a flat applianceDetails map."""
    login = _FakeLogin()
    with patch("alexapy.AlexaAPI.get_network_details",
               new=AsyncMock(return_value={"networkDetail": {"locationDetails": {}}})):
        assert _run(devices.fetch_appliances(login)) == []


# ── devices.delete_appliance ─────────────────────────────────────────────

def test_delete_appliance_success_returns_deleted_true():
    resp = _FakeResponse(200, "")
    login = _FakeLogin(cookies=[_FakeCookie("csrf", "tok123")], resp=resp)
    result = _run(devices.delete_appliance(login, "p_light#kitchen"))
    assert result["deleted"] is True
    assert result["status"] == 200
    assert result["applianceId"] == "p_light#kitchen"
    # The URL must URL-encode the appliance id (the # becomes %23).
    called_url = login.session.delete.call_args[0][0]
    assert "%23" in called_url
    assert "#" not in called_url.split("/api/phoenix/appliance/")[1]


def test_delete_appliance_non_200_returns_deleted_false():
    resp = _FakeResponse(404, "not found")
    login = _FakeLogin(cookies=[_FakeCookie("csrf", "tok")], resp=resp)
    result = _run(devices.delete_appliance(login, "orphan"))
    assert result["deleted"] is False
    assert result["status"] == 404


def test_delete_appliance_no_csrf_raises():
    login = _FakeLogin(cookies=[])  # no csrf cookie
    with pytest.raises(AlexaSessionError, match="csrf"):
        _run(devices.delete_appliance(login, "any"))


def test_delete_appliance_truncates_long_body():
    long_body = "x" * 500
    resp = _FakeResponse(200, long_body)
    login = _FakeLogin(cookies=[_FakeCookie("csrf", "t")], resp=resp)
    result = _run(devices.delete_appliance(login, "id1"))
    assert len(result["body"]) == 200


# ── devices.trigger_discovery ────────────────────────────────────────────

def test_trigger_discovery_success():
    resp = _FakeResponse(200, "{}")
    login = _FakeLogin(cookies=[_FakeCookie("csrf", "tok")], resp=resp)
    result = _run(devices.trigger_discovery(login))
    assert result["discovery"] == "triggered"
    assert result["status"] == 200


def test_trigger_discovery_failure_status():
    resp = _FakeResponse(500, "err")
    login = _FakeLogin(cookies=[_FakeCookie("csrf", "tok")], resp=resp)
    result = _run(devices.trigger_discovery(login))
    assert result["discovery"] == "failed"
    assert result["status"] == 500


def test_trigger_discovery_no_csrf_raises():
    login = _FakeLogin(cookies=[])
    with pytest.raises(AlexaSessionError, match="csrf"):
        _run(devices.trigger_discovery(login))


# ── devices.verify_deletes ───────────────────────────────────────────────

def test_verify_deletes_reports_reappeared_devices():
    login = _FakeLogin(cookies=[_FakeCookie("csrf", "tok")])
    deleted = [{"applianceId": "p_light#kitchen", "name": "Kitchen"}]

    with patch("cli_anything.alexa.core.devices.trigger_discovery",
               new=AsyncMock(return_value={"discovery": "triggered"})), \
         patch("cli_anything.alexa.core.devices.asyncio.sleep", new=AsyncMock()), \
         patch("cli_anything.alexa.core.endpoints.fetch_endpoint_records",
               new=AsyncMock(return_value=[{"applianceId": "p_light#kitchen", "name": "Kitchen"}])):
        result = _run(devices.verify_deletes(login, deleted, wait_seconds=0))

    assert result["checked"] == 1
    assert len(result["reappeared"]) == 1
    assert result["reappeared"][0]["reappeared_as"] == "applianceId"


def test_verify_deletes_empty_deleted_list():
    login = _FakeLogin(cookies=[_FakeCookie("csrf", "tok")])
    with patch("cli_anything.alexa.core.devices.trigger_discovery",
               new=AsyncMock(return_value={"discovery": "triggered"})), \
         patch("cli_anything.alexa.core.devices.asyncio.sleep", new=AsyncMock()), \
         patch("cli_anything.alexa.core.endpoints.fetch_endpoint_records",
               new=AsyncMock(return_value=[])):
        result = _run(devices.verify_deletes(login, [], wait_seconds=0))
    assert result["checked"] == 0
    assert result["reappeared"] == []


# ── control.announce ─────────────────────────────────────────────────────

def test_announce_to_all_devices_picks_first_online():
    devices_list = [
        {"serialNumber": "SN1", "accountName": "Echo Off", "online": False},
        {"serialNumber": "SN2", "accountName": "Echo On", "online": True},
    ]
    fake_api = MagicMock()
    fake_api.send_announcement = AsyncMock()
    fake_cls = _fake_alexapy_api_class(api_instance=fake_api)
    fake_cls.get_devices = AsyncMock(return_value=devices_list)
    login = _FakeLogin()
    with patch("alexapy.AlexaAPI", fake_cls):
        result = _run(control.announce(login, "dinner time"))
    assert result["announced"] == "dinner time"
    assert result["target"] == "all"
    assert result["via_device"] == "Echo On"
    # No specific target -> targets=None passed to alexapy
    fake_api.send_announcement.assert_awaited_once()
    assert fake_api.send_announcement.call_args.kwargs["targets"] is None


def test_announce_to_named_target_uses_serial():
    devices_list = [
        {"serialNumber": "SN1", "accountName": "Kitchen Echo", "online": True},
        {"serialNumber": "SN2", "accountName": "Bedroom Echo", "online": True},
    ]
    fake_api = MagicMock()
    fake_api.send_announcement = AsyncMock()
    fake_cls = _fake_alexapy_api_class(api_instance=fake_api)
    fake_cls.get_devices = AsyncMock(return_value=devices_list)
    login = _FakeLogin()
    with patch("alexapy.AlexaAPI", fake_cls):
        result = _run(control.announce(login, "wake up", device="bedroom echo"))
    assert result["target"] == "Bedroom Echo"
    assert fake_api.send_announcement.call_args.kwargs["targets"] == ["SN2"]


def test_announce_no_devices_raises():
    fake_cls = _fake_alexapy_api_class()
    fake_cls.get_devices = AsyncMock(return_value=[])
    login = _FakeLogin()
    with patch("alexapy.AlexaAPI", fake_cls):
        with pytest.raises(ValueError, match="no Alexa devices"):
            _run(control.announce(login, "hello"))


def test_announce_device_not_found_raises():
    devices_list = [{"serialNumber": "SN1", "accountName": "Kitchen", "online": True}]
    fake_cls = _fake_alexapy_api_class()
    fake_cls.get_devices = AsyncMock(return_value=devices_list)
    login = _FakeLogin()
    with patch("alexapy.AlexaAPI", fake_cls):
        with pytest.raises(ValueError, match="no device matching"):
            _run(control.announce(login, "hi", device="nonexistent"))


# ── control.set_dnd ──────────────────────────────────────────────────────

def test_set_dnd_on():
    devices_list = [{"serialNumber": "SN1", "accountName": "Kitchen", "online": True}]
    fake_api = MagicMock()
    fake_api.set_dnd_state = AsyncMock()
    fake_cls = _fake_alexapy_api_class(api_instance=fake_api)
    fake_cls.get_devices = AsyncMock(return_value=devices_list)
    login = _FakeLogin()
    with patch("alexapy.AlexaAPI", fake_cls):
        result = _run(control.set_dnd(login, "kitchen", True))
    assert result["dnd"] == "on"
    assert result["device"] == "Kitchen"
    fake_api.set_dnd_state.assert_awaited_once_with(True)


def test_set_dnd_off():
    devices_list = [{"serialNumber": "SN1", "accountName": "Kitchen", "online": True}]
    fake_api = MagicMock()
    fake_api.set_dnd_state = AsyncMock()
    fake_cls = _fake_alexapy_api_class(api_instance=fake_api)
    fake_cls.get_devices = AsyncMock(return_value=devices_list)
    login = _FakeLogin()
    with patch("alexapy.AlexaAPI", fake_cls):
        result = _run(control.set_dnd(login, "kitchen", False))
    assert result["dnd"] == "off"
    fake_api.set_dnd_state.assert_awaited_once_with(False)


def test_set_dnd_device_not_found_raises():
    devices_list = [{"serialNumber": "SN1", "accountName": "Kitchen", "online": True}]
    fake_cls = _fake_alexapy_api_class()
    fake_cls.get_devices = AsyncMock(return_value=devices_list)
    login = _FakeLogin()
    with patch("alexapy.AlexaAPI", fake_cls):
        with pytest.raises(ValueError, match="no device matching"):
            _run(control.set_dnd(login, "ghost", True))


# ── notifications.create_notification ────────────────────────────────────

def test_create_notification_success():
    resp = _FakeResponse(201, '{"created": true}')
    login = _FakeLogin(cookies=[_FakeCookie("csrf", "tok")], resp=resp)
    payload = notifications.build_reminder("test", "SN1", "DT1", 1700000000000)
    result = _run(notifications.create_notification(login, payload))
    assert result["ok"] is True
    assert result["status"] == 201


def test_create_notification_no_csrf_raises():
    login = _FakeLogin(cookies=[])
    with pytest.raises(AlexaSessionError, match="csrf"):
        _run(notifications.create_notification(login, {"type": "Alarm"}))


def test_create_notification_non_ok_status():
    resp = _FakeResponse(403, "forbidden")
    login = _FakeLogin(cookies=[_FakeCookie("csrf", "tok")], resp=resp)
    result = _run(notifications.create_notification(login, {"type": "Alarm"}))
    assert result["ok"] is False
    assert result["status"] == 403


# ── notifications.delete_notification ────────────────────────────────────

def test_delete_notification_success_204():
    resp = _FakeResponse(204, "")
    login = _FakeLogin(cookies=[_FakeCookie("csrf", "tok")], resp=resp)
    result = _run(notifications.delete_notification(login, "notif_123"))
    assert result["deleted"] is True
    assert result["id"] == "notif_123"
    assert result["status"] == 204


def test_delete_notification_success_200():
    resp = _FakeResponse(200, "ok")
    login = _FakeLogin(cookies=[_FakeCookie("csrf", "tok")], resp=resp)
    result = _run(notifications.delete_notification(login, "notif_456"))
    assert result["deleted"] is True


def test_delete_notification_non_ok():
    resp = _FakeResponse(404, "not found")
    login = _FakeLogin(cookies=[_FakeCookie("csrf", "tok")], resp=resp)
    result = _run(notifications.delete_notification(login, "gone"))
    assert result["deleted"] is False
    assert result["status"] == 404


def test_delete_notification_no_csrf_raises():
    login = _FakeLogin(cookies=[])
    with pytest.raises(AlexaSessionError, match="csrf"):
        _run(notifications.delete_notification(login, "any"))


def test_delete_notification_url_contains_id():
    resp = _FakeResponse(204, "")
    login = _FakeLogin(cookies=[_FakeCookie("csrf", "tok")], resp=resp)
    _run(notifications.delete_notification(login, "notif_abc"))
    called_url = login.session.delete.call_args[0][0]
    assert "notif_abc" in called_url
    assert "/api/notifications/notif_abc" in called_url
