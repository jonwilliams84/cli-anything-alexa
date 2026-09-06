"""Behavioural tests for the refine-round surfaces: per-Echo wake word,
Echo Show background, and the per-appliance detail read.

Covers the three alexapy calls the first build did not wrap:

* ``AlexaAPI.find_wake_word(login, serial)`` — one Echo's wake word
  (``echos wake-word``), answering from the same feed as ``echos wake-words``
  but targeted at a single speaker.
* ``AlexaAPI.set_background(url)`` — the Echo Show background write
  (``echos background``), the one new mutation.  alexapy only *warns* about a
  plain ``http://`` URL and posts it anyway, so https is validated locally,
  before login, per the validate-before-``_login`` rule.
* ``AlexaAPI.get_devices_gql(login)`` — alexapy's older smart-home GraphQL
  query (``devices capabilities``), the only read returning applianceTypes /
  capability list / model / connectedVia / entityId per appliance.

The pure builders are unit-tested directly; the live wrappers run against a
fake ``AlexaAPI``; the CLI paths assert the harness-wide contract (preview by
default, act only on ``--yes``, validate before touching the session).
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from cli_anything.alexa.alexa_cli import cli
from cli_anything.alexa.core import devices_meta, endpoints
from cli_anything.alexa.core.device_ref import DeviceRef


def _run(coro):
    return asyncio.run(coro)


DEVICES = [
    {
        "serialNumber": "SN1",
        "accountName": "Kitchen Show",
        "online": True,
        "deviceType": "A3S5BH2HU6VAYF",
    },
    {"serialNumber": "SN2", "accountName": "Bedroom", "online": True},
]


def _fake_cls(devices=None, **statics):
    """Fake ``AlexaAPI`` class: static awaits from ``statics`` + devices."""
    fake = MagicMock()
    fake.get_devices = AsyncMock(return_value=devices if devices is not None else DEVICES)
    for name, value in statics.items():
        setattr(fake, name, AsyncMock(return_value=value))
    return fake


# ── endpoints.gql_appliance_rows ────────────────────────────────────────


def _gql_item(**over):
    legacy = {
        "applianceId": "aid-1",
        "friendlyName": "Kitchen Light",
        "manufacturerName": "Home Assistant",
        "modelName": "LZW31-SN",
        "connectedVia": "HA bridge",
        "entityId": "light.kitchen",
        "applianceTypes": ["LIGHT", "SWITCH"],
        "capabilities": ["setColor", "setBrightness", "turnOn", "turnOff"],
    }
    legacy.update(over)
    return {"legacyAppliance": legacy}


def test_gql_appliance_rows_flattens_the_legacy_appliance():
    rows = endpoints.gql_appliance_rows([_gql_item()])
    assert rows == [
        {
            "applianceId": "aid-1",
            "name": "Kitchen Light",
            "manufacturer": "Home Assistant",
            "model": "LZW31-SN",
            "connectedVia": "HA bridge",
            "entityId": "light.kitchen",
            "types": ["LIGHT", "SWITCH"],
            "capabilities": 4,
        }
    ]


def _gql_appliance(**over):
    return _gql_item(**over)


def test_gql_appliance_rows_counts_capabilities_and_defaults_empty_types():
    item = _gql_item(applianceTypes=None, capabilities=None)
    rows = endpoints.gql_appliance_rows([item])
    assert rows[0]["types"] == []
    assert rows[0]["capabilities"] is None


def test_gql_appliance_rows_skips_non_dict_items():
    rows = endpoints.gql_appliance_rows([_gql_item(), "junk", 42])
    assert len(rows) == 1


@pytest.mark.parametrize("items", [None, []])
def test_gql_appliance_rows_of_nothing_is_empty(items):
    assert endpoints.gql_appliance_rows(items) == []


def test_fetch_appliance_details_uses_get_devices_gql():
    fake = _fake_cls(get_devices_gql=[_gql_item()])
    with patch("alexapy.AlexaAPI", fake):
        rows = _run(endpoints.fetch_appliance_details(MagicMock()))
    fake.get_devices_gql.assert_awaited_once()
    assert rows[0]["applianceId"] == "aid-1"


def test_fetch_appliance_details_survives_a_none_response():
    fake = _fake_cls(get_devices_gql=None)
    with patch("alexapy.AlexaAPI", fake):
        assert _run(endpoints.fetch_appliance_details(MagicMock())) == []


# ── devices_meta.fetch_wake_word ────────────────────────────────────────


def test_fetch_wake_word_targets_the_requested_echo_and_lowers_nothing():
    """find_wake_word is handed the serial verbatim; its word passes through."""
    login = MagicMock()
    fake = _fake_cls(find_wake_word="alexa")
    with patch("alexapy.AlexaAPI", fake):
        row = _run(devices_meta.fetch_wake_word(login, "Kitchen Show"))
    fake.find_wake_word.assert_awaited_once_with(login, "SN1")
    assert row == {
        "device": "Kitchen Show",
        "serial": "SN1",
        "wakeWord": "alexa",
    }


def test_fetch_wake_word_keeps_none_when_the_serial_has_no_entry():
    """An unreadable wake word is not reported as 'alexa'."""
    fake = _fake_cls(find_wake_word=None)
    with patch("alexapy.AlexaAPI", fake):
        row = _run(devices_meta.fetch_wake_word(MagicMock(), "Bedroom"))
    assert row["wakeWord"] is None
    assert row["device"] == "Bedroom"


def test_fetch_wake_word_defaults_to_the_first_online_device():
    fake = _fake_cls(find_wake_word="echo")
    with patch("alexapy.AlexaAPI", fake):
        row = _run(devices_meta.fetch_wake_word(MagicMock(), None))
    assert row["serial"] == "SN1"


def test_fetch_wake_word_unknown_device_raises():
    fake = _fake_cls()
    with patch("alexapy.AlexaAPI", fake):
        with pytest.raises(ValueError, match="no device matching"):
            _run(devices_meta.fetch_wake_word(MagicMock(), "Attic"))


# ── devices_meta.background_url_problem / set_background ────────────────


@pytest.mark.parametrize(
    "url", ["https://example.com/photo.jpg", "HTTPS://EXAMPLE.COM/P.JPG", " https://x.io/i.png "]
)
def test_background_url_problem_accepts_https(url):
    assert devices_meta.background_url_problem(url) is None


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "   ",
        "http://example.com/photo.jpg",
        "ftp://example.com/photo.jpg",
        "example.com/photo.jpg",
        42,
    ],
)
def test_background_url_problem_refuses_everything_else(url):
    assert devices_meta.background_url_problem(url) is not None


def _bg_cls_and_api(devices, ok=True):
    api = MagicMock()
    api.set_background = AsyncMock(return_value=ok)
    fake = _fake_cls(devices=devices)
    fake.return_value = api
    return fake, api


@pytest.mark.parametrize("ok", [True, False])
def test_set_background_reports_ok_straight_from_alexapy(ok):
    """set_background returns a real bool — no verify re-read needed."""
    fake, api = _bg_cls_and_api(DEVICES, ok=ok)
    with patch("alexapy.AlexaAPI", fake):
        row = _run(devices_meta.set_background(MagicMock(), "Kitchen Show", "https://x.io/i.png"))
    assert row["ok"] is ok
    assert row["device"] == "Kitchen Show"
    assert row["serial"] == "SN1"
    assert row["url"] == "https://x.io/i.png"


def test_set_background_binds_alexaapi_to_a_device_ref_not_a_dict():
    """alexapy reads _device_type / serial as attributes — dict would crash."""
    fake, api = _bg_cls_and_api(DEVICES)
    with patch("alexapy.AlexaAPI", fake):
        _run(devices_meta.set_background(MagicMock(), None, "https://x.io/i.png"))
    bound = fake.call_args[0][0]
    assert isinstance(bound, DeviceRef)
    api.set_background.assert_awaited_once_with("https://x.io/i.png")


def test_set_background_posts_the_url_verbatim():
    fake, api = _bg_cls_and_api(DEVICES)
    with patch("alexapy.AlexaAPI", fake):
        _run(devices_meta.set_background(MagicMock(), "Bedroom", "https://a.b/c?d=e&f=g"))
    api.set_background.assert_awaited_once_with("https://a.b/c?d=e&f=g")


def test_set_background_unknown_device_raises():
    fake, _ = _bg_cls_and_api(DEVICES)
    with patch("alexapy.AlexaAPI", fake):
        with pytest.raises(ValueError, match="no device matching"):
            _run(devices_meta.set_background(MagicMock(), "Attic", "https://x.io/i.png"))


# ── CLI paths ───────────────────────────────────────────────────────────


def _invoke(args, obj=None):
    return CliRunner().invoke(cli, args, obj=obj or {}, catch_exceptions=False)


@contextlib.contextmanager
def _stub_run(return_value=None):
    def fake_run(_ctx, coro):
        if hasattr(coro, "close"):
            coro.close()
        return return_value

    with patch("cli_anything.alexa.alexa_cli._run", side_effect=fake_run) as mock_run:
        yield mock_run


def _stubbed(args, module, name, return_value):
    """Run --json with a faked session and a stubbed core call."""
    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()):
        with _stub_run(return_value=return_value):
            result = _invoke(["--json", *args])
    assert result.exit_code == 0, result.output
    return result


def test_echos_wake_word_cli_reads_one_device():
    stub = MagicMock()
    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()):
        with _stub_run(return_value={"device": "Kitchen Show", "wakeWord": "alexa"}):
            with patch.object(devices_meta, "fetch_wake_word", stub) as core:
                result = _invoke(["--json", "echos", "wake-word", "Kitchen Show"])
    assert result.exit_code == 0, result.output
    core.assert_called_once()
    assert '"wakeWord": "alexa"' in result.output


def test_echos_background_dry_run_does_not_call_the_write():
    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()) as login:
        with _stub_run(return_value=None):
            with patch.object(devices_meta, "set_background", AsyncMock()) as write:
                result = _invoke(["--json", "echos", "background", "https://x.io/i.png"])
    assert result.exit_code == 0, result.output
    write.assert_not_awaited()
    login.assert_called_once()
    data = __import__("json").loads(result.output)
    assert data["dry_run"] is True
    assert data["would_set_background"] == "https://x.io/i.png"


def test_echos_background_yes_executes_the_write():
    core = MagicMock(return_value={"device": "Kitchen Show", "ok": True})
    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()) as login:
        with _stub_run(return_value=core.return_value) as runner:
            with patch.object(devices_meta, "set_background", core):
                result = _invoke(
                    [
                        "--json",
                        "echos",
                        "background",
                        "https://x.io/i.png",
                        "--device",
                        "Kitchen",
                        "--yes",
                    ]
                )
    assert result.exit_code == 0, result.output
    core.assert_called_once_with(login.return_value, "Kitchen", "https://x.io/i.png")
    assert '"ok": true' in result.output


def test_echos_background_https_refusal_fires_before_the_session():
    """The validate-before-_login contract: no email, no session, and the
    command still fails with the https error — identically with and without
    --yes."""
    for argv in (
        ["echos", "background", "http://x.io/i.png"],
        ["echos", "background", "http://x.io/i.png", "--yes"],
    ):
        result = _invoke(argv)
        assert result.exit_code == 1
        assert "must be https" in result.output


def test_devices_capabilities_cli_emits_the_detail_rows():
    result = _stubbed(
        ["devices", "capabilities"], endpoints, "fetch_appliance_details",
        [{"applianceId": "aid-1", "capabilities": 4}],
    )
    assert '"applianceId": "aid-1"' in result.output
    assert '"capabilities": 4' in result.output
