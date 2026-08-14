"""Behavioural tests for core/kids.py (Amazon Kids / child mode).

The pure layer (profile flattening, child resolution, the not-found message,
status rows) is tested directly.  The live wrappers run against a fake
``AlexaAPI`` so the two rules that matter are pinned without an account:

* a write is **verified by re-reading**, because alexapy's kids writes return
  ``None`` whether they worked or not;
* "unknown" (``get_child_mode`` → ``None``) never renders as "off".
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_anything.alexa.core import kids
from cli_anything.alexa.core.device_ref import DeviceRef


def _run(coro):
    return asyncio.run(coro)


def _echo(serial="SN1", name="Kitchen", online=True, device_type="A3S5BH2HU6VAYF"):
    return {
        "serialNumber": serial,
        "accountName": name,
        "deviceType": device_type,
        "deviceFamily": "ECHO",
        "online": online,
    }


ALICE = {"firstName": "Alice", "age": 7, "directedId": "amzn1.account.ALICE", "role": "CHILD"}
BOB = {"firstName": "Bob", "age": 10, "directedId": "amzn1.account.BOB", "role": "CHILD"}
PARENT = {"firstName": "Jon", "age": 40, "directedId": "amzn1.account.JON", "role": "ADULT"}


def _fake_api(devices, profiles=(ALICE, BOB), enabled=True, directed_id="amzn1.account.ALICE"):
    """Fake ``AlexaAPI``: the five static kids calls + get_devices."""
    fake_cls = MagicMock()
    fake_cls.get_devices = AsyncMock(return_value=devices)
    fake_cls.get_child_profiles = AsyncMock(return_value=list(profiles))
    fake_cls.get_child_mode = AsyncMock(return_value=enabled)
    fake_cls.get_device_child = AsyncMock(return_value=directed_id)
    fake_cls.enable_child_mode = AsyncMock(return_value=None)
    fake_cls.disable_child_mode = AsyncMock(return_value=None)
    return fake_cls


# ── profile_rows ────────────────────────────────────────────────────────


def test_profile_rows_flattens_the_fields_the_cli_shows():
    assert kids.profile_rows([ALICE]) == [
        {"name": "Alice", "age": 7, "directedId": "amzn1.account.ALICE"}
    ]


def test_profile_rows_drops_adults_from_a_raw_household_payload():
    rows = kids.profile_rows([ALICE, PARENT, BOB])
    assert [r["name"] for r in rows] == ["Alice", "Bob"]


def test_profile_rows_keeps_entries_with_no_role_key():
    """get_child_profiles has already filtered; those rows carry no `role`."""
    rows = kids.profile_rows([{"firstName": "Alice", "directedId": "x"}])
    assert [r["name"] for r in rows] == ["Alice"]


@pytest.mark.parametrize("payload", [None, [], [None, "junk", 3]])
def test_profile_rows_survives_junk(payload):
    assert kids.profile_rows(payload) == []


# ── normalize_name ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("Alice", "alice"), ("  ALICE  ", "alice"), ("Mary   Jane", "mary jane"), (None, ""), (7, "")],
)
def test_normalize_name(raw, expected):
    assert kids.normalize_name(raw) == expected


# ── resolve_child ───────────────────────────────────────────────────────


PROFILES = kids.profile_rows([ALICE, BOB])


def test_resolve_child_by_directed_id():
    assert kids.resolve_child(PROFILES, "amzn1.account.BOB") == [PROFILES[1]]


def test_resolve_child_by_exact_name():
    assert kids.resolve_child(PROFILES, "Alice") == [PROFILES[0]]


def test_resolve_child_by_normalized_name():
    assert kids.resolve_child(PROFILES, "  aLiCe ") == [PROFILES[0]]


def test_resolve_child_returns_every_sibling_sharing_a_name():
    """Two children CAN share a first name; ambiguity is the caller's call."""
    twins = kids.profile_rows(
        [
            {"firstName": "Sam", "directedId": "id-1"},
            {"firstName": "Sam", "directedId": "id-2"},
        ]
    )
    assert len(kids.resolve_child(twins, "Sam")) == 2


def test_resolve_child_exact_name_beats_a_case_variant():
    """The exact tier resolves `Sam` vs `sam` rather than calling it ambiguous."""
    pair = kids.profile_rows(
        [
            {"firstName": "Sam", "directedId": "id-1"},
            {"firstName": "sam", "directedId": "id-2"},
        ]
    )
    assert kids.resolve_child(pair, "Sam") == [pair[0]]
    # ...but a name matching NEITHER exactly falls through to the normalized
    # tier, where both are equally good answers.
    assert len(kids.resolve_child(pair, "SAM")) == 2


def test_resolve_child_prefers_directed_id_over_name():
    odd = kids.profile_rows(
        [
            {"firstName": "shared", "directedId": "shared"},
            {"firstName": "shared", "directedId": "other"},
        ]
    )
    assert kids.resolve_child(odd, "shared") == [odd[0]]


@pytest.mark.parametrize("target", ["", "   ", "nobody"])
def test_resolve_child_no_match(target):
    assert kids.resolve_child(PROFILES, target) == []


# ── error message ───────────────────────────────────────────────────────


def test_no_child_error_lists_the_alternatives():
    msg = kids.no_child_error("Charlie", PROFILES)
    assert "Charlie" in msg and "Alice" in msg and "Bob" in msg


def test_no_child_error_when_no_profiles_exist_points_at_the_app():
    msg = kids.no_child_error("Charlie", [])
    assert "no Amazon Kids child profiles" in msg and "Alexa app" in msg


def test_child_labels_falls_back_to_the_directed_id():
    assert kids.child_labels([{"directedId": "id-only"}, "junk", {}]) == ["id-only"]


# ── child_name_for / status_row ─────────────────────────────────────────


def test_child_name_for_maps_the_id_back_to_a_name():
    assert kids.child_name_for(PROFILES, "amzn1.account.BOB") == "Bob"


@pytest.mark.parametrize("directed_id", [None, "", "unknown-id"])
def test_child_name_for_unmatched(directed_id):
    assert kids.child_name_for(PROFILES, directed_id) is None


def test_status_row_from_a_raw_record():
    row = kids.status_row(_echo(), True, "amzn1.account.ALICE", PROFILES)
    assert row == {
        "device": "Kitchen",
        "serial": "SN1",
        "kids": "on",
        "child": "Alice",
        "childDirectedId": "amzn1.account.ALICE",
    }


def test_status_row_from_a_device_ref():
    row = kids.status_row(DeviceRef(_echo(name="Study")), False, None, PROFILES)
    assert row["device"] == "Study"
    assert row["serial"] == "SN1"
    assert row["kids"] == "off"


def test_status_row_keeps_unknown_distinct_from_off():
    """get_child_mode returns None when it could not read the state."""
    assert kids.status_row(_echo(), None, None, PROFILES)["kids"] is None
    assert kids.status_row(_echo(), False, None, PROFILES)["kids"] == "off"


# ── fetch_profiles / read_state ─────────────────────────────────────────


def test_fetch_profiles_flattens_the_live_payload():
    fake = _fake_api([_echo()])
    with patch("alexapy.AlexaAPI", fake):
        rows = _run(kids.fetch_profiles(MagicMock()))
    assert [r["name"] for r in rows] == ["Alice", "Bob"]


def test_read_state_asks_both_endpoints():
    fake = _fake_api([_echo()])
    login = MagicMock()
    with patch("alexapy.AlexaAPI", fake):
        enabled, directed_id = _run(kids.read_state(login, "SN1", "TYPE"))
    assert (enabled, directed_id) == (True, "amzn1.account.ALICE")
    fake.get_child_mode.assert_awaited_once_with(login, "SN1", "TYPE")
    fake.get_device_child.assert_awaited_once_with(login, "SN1", "TYPE")


# ── device_status / status_all ──────────────────────────────────────────


def test_device_status_resolves_the_named_echo():
    fake = _fake_api([_echo(), _echo(serial="SN2", name="Study")])
    with patch("alexapy.AlexaAPI", fake):
        row = _run(kids.device_status(MagicMock(), "Study"))
    assert row["device"] == "Study"
    assert row["kids"] == "on"
    assert row["child"] == "Alice"
    assert fake.get_child_mode.await_args.args[1] == "SN2"


def test_device_status_defaults_to_the_first_online_echo():
    fake = _fake_api([_echo(serial="SN1", name="Offline", online=False), _echo("SN2", "Kitchen")])
    with patch("alexapy.AlexaAPI", fake):
        row = _run(kids.device_status(MagicMock()))
    assert row["device"] == "Kitchen"


def test_device_status_unknown_device_is_refused():
    fake = _fake_api([_echo()])
    with patch("alexapy.AlexaAPI", fake), pytest.raises(ValueError, match="no device matching"):
        _run(kids.device_status(MagicMock(), "Garage"))


def test_status_all_returns_one_row_per_echo():
    fake = _fake_api([_echo(), _echo(serial="SN2", name="Study")])
    with patch("alexapy.AlexaAPI", fake):
        rows = _run(kids.status_all(MagicMock()))
    assert [r["device"] for r in rows] == ["Kitchen", "Study"]
    assert fake.get_child_mode.await_count == 2


def test_status_all_skips_records_with_no_serial():
    fake = _fake_api([_echo(), {"accountName": "Ghost", "deviceType": "T"}])
    with patch("alexapy.AlexaAPI", fake):
        rows = _run(kids.status_all(MagicMock()))
    assert [r["device"] for r in rows] == ["Kitchen"]


def test_status_all_still_lists_an_echo_whose_state_is_unreadable():
    fake = _fake_api([_echo()], enabled=None, directed_id=None)
    with patch("alexapy.AlexaAPI", fake):
        rows = _run(kids.status_all(MagicMock()))
    assert rows[0]["kids"] is None


def test_status_all_on_an_empty_account():
    fake = _fake_api([])
    with patch("alexapy.AlexaAPI", fake), pytest.raises(ValueError, match="no Alexa devices"):
        _run(kids.status_all(MagicMock()))


# ── enable ──────────────────────────────────────────────────────────────


def test_enable_posts_the_resolved_directed_id_and_verifies():
    fake = _fake_api([_echo()])
    login = MagicMock()
    with patch("alexapy.AlexaAPI", fake):
        row = _run(kids.enable(login, "Kitchen", "Alice"))
    fake.enable_child_mode.assert_awaited_once_with(
        login, "SN1", "A3S5BH2HU6VAYF", "amzn1.account.ALICE"
    )
    # verified by re-reading, not by "nothing raised"
    fake.get_child_mode.assert_awaited()
    assert row["ok"] is True
    assert row["kids"] == "on"
    assert row["requested"] == "Alice"


def test_enable_reports_not_ok_when_the_verify_read_says_it_did_not_take():
    """alexapy swallows a rejected assign and returns None either way."""
    fake = _fake_api([_echo()], enabled=False, directed_id=None)
    with patch("alexapy.AlexaAPI", fake):
        row = _run(kids.enable(MagicMock(), "Kitchen", "Alice"))
    fake.enable_child_mode.assert_awaited_once()
    assert row["ok"] is False


def test_enable_reports_not_ok_when_the_state_is_unreadable():
    fake = _fake_api([_echo()], enabled=None, directed_id=None)
    with patch("alexapy.AlexaAPI", fake):
        row = _run(kids.enable(MagicMock(), "Kitchen", "Alice"))
    assert row["ok"] is False
    assert row["kids"] is None


def test_enable_accepts_a_directed_id_as_the_child():
    fake = _fake_api([_echo()])
    login = MagicMock()
    with patch("alexapy.AlexaAPI", fake):
        _run(kids.enable(login, "Kitchen", "amzn1.account.BOB"))
    assert fake.enable_child_mode.await_args.args[3] == "amzn1.account.BOB"


@pytest.mark.parametrize("child", ["", "   "])
def test_enable_requires_a_child(child):
    fake = _fake_api([_echo()])
    with patch("alexapy.AlexaAPI", fake), pytest.raises(ValueError, match="child profile name"):
        _run(kids.enable(MagicMock(), "Kitchen", child))
    fake.enable_child_mode.assert_not_awaited()


def test_enable_refuses_an_unknown_child_locally():
    """An unknown childDirectedId is rejected server-side with no message."""
    fake = _fake_api([_echo()])
    with patch("alexapy.AlexaAPI", fake), pytest.raises(ValueError, match="no child profile"):
        _run(kids.enable(MagicMock(), "Kitchen", "Charlie"))
    fake.enable_child_mode.assert_not_awaited()


def test_enable_aborts_on_an_ambiguous_child():
    fake = _fake_api(
        [_echo()],
        profiles=[
            {"firstName": "Sam", "directedId": "id-1", "role": "CHILD"},
            {"firstName": "Sam", "directedId": "id-2", "role": "CHILD"},
        ],
    )
    with patch("alexapy.AlexaAPI", fake), pytest.raises(ValueError, match="matches 2 child"):
        _run(kids.enable(MagicMock(), "Kitchen", "Sam"))
    fake.enable_child_mode.assert_not_awaited()


def test_enable_refuses_a_profile_with_no_directed_id():
    fake = _fake_api([_echo()], profiles=[{"firstName": "Alice", "role": "CHILD"}])
    with patch("alexapy.AlexaAPI", fake), pytest.raises(ValueError, match="no directedId"):
        _run(kids.enable(MagicMock(), "Kitchen", "Alice"))
    fake.enable_child_mode.assert_not_awaited()


def test_enable_on_an_unknown_device_never_writes():
    fake = _fake_api([_echo()])
    with patch("alexapy.AlexaAPI", fake), pytest.raises(ValueError, match="no device matching"):
        _run(kids.enable(MagicMock(), "Garage", "Alice"))
    fake.enable_child_mode.assert_not_awaited()


# ── disable ─────────────────────────────────────────────────────────────


def test_disable_unassigns_and_verifies():
    fake = _fake_api([_echo()], enabled=False, directed_id=None)
    login = MagicMock()
    with patch("alexapy.AlexaAPI", fake):
        row = _run(kids.disable(login, "Kitchen"))
    fake.disable_child_mode.assert_awaited_once_with(login, "SN1", "A3S5BH2HU6VAYF")
    assert row["ok"] is True
    assert row["kids"] == "off"
    assert row["child"] is None


def test_disable_reports_not_ok_when_kids_mode_is_still_on():
    fake = _fake_api([_echo()], enabled=True)
    with patch("alexapy.AlexaAPI", fake):
        row = _run(kids.disable(MagicMock(), "Kitchen"))
    assert row["ok"] is False
    assert row["kids"] == "on"


def test_disable_unknown_state_is_not_reported_as_success():
    """`None` means the verify read failed, NOT that kids mode is off."""
    fake = _fake_api([_echo()], enabled=None, directed_id=None)
    with patch("alexapy.AlexaAPI", fake):
        row = _run(kids.disable(MagicMock(), "Kitchen"))
    assert row["ok"] is False
    assert row["kids"] is None


def test_disable_on_an_unknown_device_never_writes():
    fake = _fake_api([_echo()])
    with patch("alexapy.AlexaAPI", fake), pytest.raises(ValueError, match="no device matching"):
        _run(kids.disable(MagicMock(), "Garage"))
    fake.disable_child_mode.assert_not_awaited()
