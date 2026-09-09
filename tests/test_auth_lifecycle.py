"""Behavioural tests for the session-lifecycle surface (0.7.0 refine).

Three previously unwrapped alexapy calls plus a standalone TOTP helper:

* ``AlexaAPI.ping`` (the app's own authenticated ``/api/ping`` health check)
  → ``auth ping``;
* ``AlexaLogin.refresh_access_token`` (OAuth ``/auth/token`` exchange from
  the cookie's refresh token) → ``auth refresh``;
* ``AlexaLogin._cookiefile`` deletion (``delete_cookiefile``/``reset``) →
  ``auth logout`` (pure path math + filesystem, no alexapy needed);
* ``set_totp``/``get_totp_token`` → ``auth totp`` (pure, pyotp).

The row builders are pure and tested directly; the wrappers run against
fakes (no network, no live account).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_anything.alexa.core import session


# ── ping_row (pure) ─────────────────────────────────────────────────────


def test_ping_row_ok_from_a_live_body():
    assert session.ping_row({"hmac": "abc"}) == {"ok": True, "detail": {"hmac": "abc"}}


@pytest.mark.parametrize("payload", [None, {}, ""])
def test_ping_row_refuses_an_empty_answer(payload):
    assert session.ping_row(payload)["ok"] is False


def test_ping_row_refuses_an_error_body():
    assert session.ping_row({"error": "boom"})["ok"] is False


def test_ping_row_empty_payload_reports_detail_none():
    assert session.ping_row(None)["detail"] is None


# ── session_ping (wrapper) ──────────────────────────────────────────────


def test_session_ping_calls_alexapy_ping():
    fake = MagicMock()
    fake.ping = AsyncMock(return_value={"hmac": "abc"})
    with patch("alexapy.AlexaAPI", fake):
        row = asyncio.run(session.session_ping(MagicMock()))
    assert row["ok"] is True
    fake.ping.assert_awaited_once()


def test_session_ping_reports_false_when_the_api_returns_none():
    fake = MagicMock()
    fake.ping = AsyncMock(return_value=None)
    with patch("alexapy.AlexaAPI", fake):
        row = asyncio.run(session.session_ping(MagicMock()))
    assert row == {"ok": False, "detail": None}


# ── refresh_row / refresh_access_token ──────────────────────────────────


def test_refresh_row_defaults():
    row = session.refresh_row(False, False)
    assert row == {"refreshed": False, "has_refresh_token": False, "expires_at": None}


def test_refresh_access_token_refuses_without_a_refresh_token():
    login = MagicMock()
    login.refresh_token = None
    row = asyncio.run(session.refresh_access_token(login))
    assert row == {"refreshed": False, "has_refresh_token": False, "expires_at": None}
    assert not login.method_calls  # the network exchange was never attempted


def test_refresh_access_token_reports_the_new_expiry():
    login = MagicMock()
    login.refresh_token = "rt"
    login.refresh_access_token = AsyncMock(return_value=True)
    login.expires_in = time.time() + 3600
    row = asyncio.run(session.refresh_access_token(login))
    assert row["refreshed"] is True
    assert row["has_refresh_token"] is True
    assert row["expires_at"]  # iso timestamp present
    login.refresh_access_token.assert_awaited_once()


def test_refresh_access_token_reports_failure_without_an_expiry():
    login = MagicMock()
    login.refresh_token = "rt"
    login.refresh_access_token = AsyncMock(return_value=False)
    row = asyncio.run(session.refresh_access_token(login))
    assert row["refreshed"] is False
    assert row["expires_at"] is None


def test_refresh_access_token_survives_a_garbled_expiry():
    login = MagicMock()
    login.refresh_token = "rt"
    login.refresh_access_token = AsyncMock(return_value=True)
    login.expires_in = "not-a-number"
    row = asyncio.run(session.refresh_access_token(login))
    assert row["refreshed"] is True
    assert row["expires_at"] is None


# ── cookie_paths_in_dir / logout_plan (pure) ────────────────────────────


def test_cookie_paths_mirror_alexapys_cookiefile_list(tmp_path):
    paths = session.cookie_paths_in_dir(tmp_path, "user@example.com")
    assert paths == [
        tmp_path / ".storage" / "alexa_media.user@example.com.cookies",
        tmp_path / ".storage" / "alexa_media.user@example.com.pickle",
        tmp_path / "alexa_media.user@example.com.pickle",
        tmp_path / ".storage" / "alexa_media.user@example.com.txt",
    ]


def test_cookie_paths_sanitize_the_email(tmp_path):
    paths = session.cookie_paths_in_dir(tmp_path, "../../etc/passwd")
    for p in paths:
        assert tmp_path in p.parents or p.parent == tmp_path
        assert ".." not in p.parts


def test_logout_plan_splits_present_from_absent(tmp_path):
    present = tmp_path / "alexa_media.u@example.com.pickle"
    present.parent.mkdir(exist_ok=True)
    present.write_text("x")
    plan = session.logout_plan(tmp_path, "u@example.com")
    assert plan["present"] == [str(present)]
    assert len(plan["absent"]) == 3


def test_logout_plan_on_an_empty_dir_lists_everything_absent(tmp_path):
    plan = session.logout_plan(tmp_path, "u@example.com")
    assert plan["present"] == []
    assert len(plan["absent"]) == 4


# ── logout_session (filesystem) ─────────────────────────────────────────


def test_logout_session_removes_every_cookie_file_and_verifies(tmp_path):
    storage = tmp_path / ".storage"
    storage.mkdir()
    names = [
        storage / "alexa_media.u@example.com.cookies",
        storage / "alexa_media.u@example.com.pickle",
        tmp_path / "alexa_media.u@example.com.pickle",
        storage / "alexa_media.u@example.com.txt",
    ]
    for n in names:
        n.write_text("cookie")
    result = session.logout_session("u@example.com", config_dir=tmp_path)
    assert result["verified"] is True
    assert result["failed"] == []
    assert sorted(Path(p).name for p in result["removed"]) == sorted(
        n.name for n in names
    )
    for n in names:
        assert not n.exists()


def test_logout_session_reports_verified_true_when_nothing_was_there(tmp_path):
    result = session.logout_session("nobody@example.com", config_dir=tmp_path)
    assert result == {
        "email": "nobody@example.com",
        "config_dir": str(tmp_path),
        "removed": [],
        "verified": True,
        "failed": [],
    }


def test_logout_session_reports_a_directory_in_the_way_as_failed(tmp_path):
    """A directory squatting on the cookie path is never silently 'removed':
    verified is False and failed names the leftover."""
    stuck = tmp_path / "alexa_media.u@example.com.pickle"
    stuck.mkdir()
    (stuck / "inside").write_text("x")
    result = session.logout_session("u@example.com", config_dir=tmp_path)
    assert result["verified"] is False
    assert result["failed"] == [str(stuck)]
    assert result["removed"] == []


# ── totp_row ────────────────────────────────────────────────────────────


def test_totp_row_pins_a_code_to_an_instant():
    row = session.totp_row("JBSWY3DPEHPK3PXP", at=1_700_000_000)
    assert len(row["code"]) == 6
    assert row["interval"] == 30
    assert 1 <= row["valid_for"] <= 30


def test_totp_row_is_stable_for_the_same_instant():
    assert session.totp_row("JBSWY3DPEHPK3PXP", at=59)["code"] == session.totp_row(
        "JBSWY3DPEHPK3PXP", at=59
    )["code"]
    assert session.totp_row("JBSWY3DPEHPK3PXP", at=59)["valid_for"] == 1


def test_totp_row_rejects_an_empty_secret():
    with pytest.raises(session.AlexaSessionError, match="otp-secret"):
        session.totp_row("")


def test_totp_row_rejects_a_non_base32_secret():
    with pytest.raises(session.AlexaSessionError, match="invalid TOTP secret"):
        session.totp_row("not base32 !!")
