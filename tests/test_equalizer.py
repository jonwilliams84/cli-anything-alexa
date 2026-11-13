"""Behavioural tests for the Echo equalizer surface (0.16.0).

Two previously unwrapped alexapy calls:

* ``AlexaAPI.get_equalizer(login, serial, device_type)`` — the per-Echo
  bass/mid/treble read (``echos equalizer``);
* ``AlexaAPI.set_equalizer(..., bass, mid, treble)`` — the write
  (``echos set-equalizer``), static like the notification-volume surface,
  NOT a ``DeviceRef`` path.

Amazon zeroes any band left out, so all three are always sent.  The write
answers ``True`` merely because a response exists, so the executed write
re-reads the equalizer document and reports ``ok`` from what Amazon holds —
three-valued like every other verify.  The row/band/verify helpers are pure
and tested directly; the live wrappers run against a fake ``AlexaAPI``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_anything.alexa.core import equalizer


def _run(coro):
    return asyncio.run(coro)


ECHO = {
    "serialNumber": "SN1",
    "accountName": "Kitchen",
    "deviceType": "DT1",
    "deviceFamily": "ECHO",
    "online": True,
}
NO_EQ = {
    "serialNumber": "SN2",
    "accountName": "Plug",
    "deviceType": "DT2",
    "online": True,
}


# ── normalize_band ───────────────────────────────────────────────────────


@pytest.mark.parametrize("band", ["bass", "mid", "treble"])
def test_normalize_band_accepts_the_whole_range(band):
    assert [equalizer.normalize_band(v, band) for v in (-6, 0, 3, 6)] == [-6, 0, 3, 6]


def test_normalize_band_refuses_bool():
    with pytest.raises(ValueError, match="bass must be an integer"):
        equalizer.normalize_band(True, "bass")


def test_normalize_band_refuses_out_of_range():
    with pytest.raises(ValueError, match="mid must be from -6 to 6, got 7"):
        equalizer.normalize_band(7, "mid")
    with pytest.raises(ValueError, match="treble must be from -6 to 6, got -7"):
        equalizer.normalize_band(-7, "treble")


def test_normalize_band_refuses_non_int():
    with pytest.raises(ValueError, match="bass must be an integer"):
        equalizer.normalize_band(2.5, "bass")


# ── equalizer_row ────────────────────────────────────────────────────────


def test_equalizer_row_names_the_device_and_keeps_all_three_bands():
    row = equalizer.equalizer_row({"bass": 2, "mid": 0, "treble": -1}, ECHO)
    assert row == {
        "device": "Kitchen",
        "serial": "SN1",
        "deviceType": "DT1",
        "bass": 2,
        "mid": 0,
        "treble": -1,
    }


def test_equalizer_row_shows_none_when_the_device_answered_nothing():
    row = equalizer.equalizer_row(None, ECHO)
    assert (row["bass"], row["mid"], row["treble"]) == (None, None, None)


def test_equalizer_row_survives_a_partial_answer():
    row = equalizer.equalizer_row({"bass": 5}, ECHO)
    assert (row["bass"], row["mid"], row["treble"]) == (5, None, None)


# ── resolve_equalizer_target ─────────────────────────────────────────────


def test_resolve_target_matches_name_case_insensitively():
    record = equalizer.resolve_equalizer_target([ECHO, NO_EQ], "kitchen")
    assert record["serialNumber"] == "SN1"


def test_resolve_target_unknown_name_lists_candidates():
    with pytest.raises(ValueError, match="no device matching 'Garage'; devices: Kitchen, Plug"):
        equalizer.resolve_equalizer_target([ECHO, NO_EQ], "Garage")


def test_resolve_target_refuses_a_record_without_devicetype():
    serialless = {"serialNumber": "SN3", "accountName": "Attic"}
    with pytest.raises(ValueError, match="no deviceType"):
        equalizer.resolve_equalizer_target([ECHO, serialless], "Attic")


# ── observed_bands / verify_equalizer ────────────────────────────────────


def test_observed_bands_keeps_missing_bands_none():
    observed = equalizer.observed_bands({"bass": 1, "treble": "3"})
    assert observed == {"bass": 1, "mid": None, "treble": 3}


def test_observed_bands_none_when_read_gave_nothing():
    assert equalizer.observed_bands(None) is None
    assert equalizer.observed_bands("no") is None


def test_verify_equalizer_true_when_all_bands_match():
    requested = {"bass": 2, "mid": 0, "treble": -1}
    assert equalizer.verify_equalizer(requested, dict(requested)) is True


def test_verify_equalizer_false_when_any_band_differs():
    assert (
        equalizer.verify_equalizer(
            {"bass": 2, "mid": 0, "treble": -1}, {"bass": 0, "mid": 0, "treble": -1}
        )
        is False
    )


def test_verify_equalizer_none_when_it_could_not_check():
    requested = {"bass": 2, "mid": 0, "treble": -1}
    assert equalizer.verify_equalizer(requested, None) is None
    assert equalizer.verify_equalizer(requested, {"bass": 2, "mid": None, "treble": -1}) is None


# ── the live wrappers, against a fake AlexaAPI ───────────────────────────


def _fake(reads: dict):
    """A MagicMock AlexaAPI: get_devices + the two static equalizer calls."""
    fake = MagicMock()
    fake.get_devices = AsyncMock(return_value=[ECHO, NO_EQ])
    fake.get_equalizer = AsyncMock(side_effect=lambda login, serial, dtype: reads.get(serial))
    writes: list[tuple] = []

    def _record_write(_login, serial, dtype, bass, mid, treble):
        writes.append((serial, bass, mid, treble))
        return True

    fake.set_equalizer = AsyncMock(side_effect=_record_write)
    fake.writes = writes
    return fake


def test_fetch_equalizer_names_every_device_and_makes_the_unguarded_plug_visible():
    fake = _fake({"SN1": {"bass": 2, "mid": 0, "treble": -1}})
    with patch("alexapy.AlexaAPI", fake):
        rows = _run(equalizer.fetch_equalizer(MagicMock()))
    assert [r["device"] for r in rows] == ["Kitchen", "Plug"]
    assert rows[0]["treble"] == -1
    assert (rows[1]["bass"], rows[1]["mid"], rows[1]["treble"]) == (None, None, None)
    fake.get_devices.assert_awaited_once()
    # the plug is visible but was still asked — its bands just answered None
    assert fake.get_equalizer.await_count == 2


def test_fetch_equalizer_one_named_device_hits_the_network_only_for_it():
    fake = _fake({})
    with patch("alexapy.AlexaAPI", fake):
        rows = _run(equalizer.fetch_equalizer(MagicMock(), "Kitchen"))
    assert [r["serial"] for r in rows] == ["SN1"]
    assert fake.get_equalizer.await_count == 1
    assert fake.get_equalizer.await_args[0][1] == "SN1"


def test_fetch_equalizer_reuses_a_device_list_it_was_given():
    fake = _fake({"SN1": None})
    with patch("alexapy.AlexaAPI", fake):
        _run(equalizer.fetch_equalizer(MagicMock(), devices=[ECHO]))
    fake.get_devices.assert_not_awaited()


def test_set_equalizer_bands_sends_all_three_and_verifies_by_re_read():
    fake = _fake({"SN1": {"bass": 2, "mid": 0, "treble": -1}})
    with patch("alexapy.AlexaAPI", fake):
        result = _run(equalizer.set_equalizer_bands(MagicMock(), "Kitchen", 2, 0, -1))
    assert fake.writes == [("SN1", 2, 0, -1)]
    assert result["ok"] is True
    assert result["observed"] == {"bass": 2, "mid": 0, "treble": -1}
    assert result["sent"] is True
    # the verify is a re-read of the same document
    fake.get_equalizer.assert_awaited_once()


def test_set_equalizer_bands_reports_false_when_amazon_stored_something_else():
    fake = _fake({"SN1": {"bass": 0, "mid": 0, "treble": 0}})
    with patch("alexapy.AlexaAPI", fake):
        result = _run(equalizer.set_equalizer_bands(MagicMock(), "Kitchen", 2, 0, -1))
    assert result["ok"] is False


def test_set_equalizer_bands_ok_is_none_when_the_verify_read_answered_nothing():
    fake = _fake({"SN1": None})
    with patch("alexapy.AlexaAPI", fake):
        result = _run(equalizer.set_equalizer_bands(MagicMock(), "Kitchen", 2, 0, -1))
    assert result["ok"] is None
    assert result["observed"] is None


def test_set_equalizer_bands_refuses_unknown_device_before_any_write():
    fake = _fake({})
    with patch("alexapy.AlexaAPI", fake), pytest.raises(ValueError, match="no device matching"):
        _run(equalizer.set_equalizer_bands(MagicMock(), "Garage", 1, 0, 0))
    fake.set_equalizer.assert_not_awaited()


def test_set_equalizer_bands_rejects_an_out_of_range_band():
    with pytest.raises(ValueError, match="bass must be"):
        _run(equalizer.set_equalizer_bands(MagicMock(), "Kitchen", 9, 0, 0))


# ── the read wrapper's device argument is validated locally ──────────────


def test_fetch_equalizer_refuses_unknown_device_locally():
    with pytest.raises(ValueError, match="no device matching 'Garage'"):
        _run(equalizer.fetch_equalizer(MagicMock(), "Garage", devices=[ECHO]))
