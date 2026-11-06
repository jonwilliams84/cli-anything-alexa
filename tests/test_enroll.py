"""Behavioural tests for the device-credential enrollment surface (0.15.0).

``auth enroll`` wraps alexapy's ``secureauth`` module (the "alexa-auth-
redesign") — the paste-URL PKCE flow that exchanges a browser sign-in for
DURABLE device credentials (refresh token + mac_dms) and, without a
password or a proxy:

* ``register_code`` → ``EnrollmentFlow.async_register`` — the code-for-
  credentials exchange, ridden on a caller-supplied aiohttp session;
* ``activate_credentials`` — builds an ``AlexaLogin`` over the oauth
  material, mints cookies via ``exchange_token_for_cookies``, verifies
  with ``test_loggedin``, saves the cookie.

Pure helpers are tested directly (domain gates, flow/credential rows,
masked secrets, the state/credentials files); the network half runs
against fakes (no traffic, no live account). Secrets are asserted to
NEVER land in display rows or summary JSON.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_anything.alexa.core import enroll
from cli_anything.alexa.core.session import AlexaSessionError

GOOD_URL = (
    "https://www.amazon.com/ap/maplanding?openid.oa2.authorization_code=AUTHTOKEN"
)


def _run(coro):
    import asyncio

    return asyncio.run(coro)


# ── the domain gates ─────────────────────────────────────────────────────


def test_supported_domains_come_from_alexapy():
    assert "amazon.com" in enroll.supported_domains()
    assert isinstance(enroll.supported_domains(), set)


def test_validate_enrollment_region_accepts_a_supported_host():
    assert enroll.validate_enrollment_region("amazon.co.uk") == "amazon.co.uk"
    assert enroll.validate_enrollment_region("https://alexa.Amazon.com") == "amazon.com"


@pytest.mark.parametrize(
    "host",
    ["evil.com", "amazon.example.org", "amazon.pl", "", None],
)
def test_validate_enrollment_region_refuses_hosts_outside_the_allowlist(host):
    # amazon.pl is a session region but NOT enrollable: refuse before a
    # browser round-trip burns the PKCE verifier.
    with pytest.raises(enroll.AlexaEnrollError) as err:
        enroll.validate_enrollment_region(host)
    assert "not available" in str(err.value) or "Amazon region host" in str(err.value)


def test_new_flow_builds_a_flow_for_a_valid_domain():
    flow = enroll.new_flow("amazon.com", serial="SERIAL-1")
    assert flow.domain == "amazon.com"
    assert flow.serial == "SERIAL-1"


# ── flow state: start here, finish elsewhere ────────────────────────────


def test_flow_state_round_trip_preserves_the_verifier(tmp_path):
    flow = enroll.new_flow("amazon.com", serial="SERIAL-2")
    path = enroll.save_flow_state(tmp_path / "cfg", flow)
    restored = enroll.load_flow_state(path)
    assert restored.code_verifier == flow.code_verifier
    assert restored.serial == "SERIAL-2"


def test_save_flow_state_is_owner_only(tmp_path):
    path = enroll.save_flow_state(tmp_path / "cfg", enroll.new_flow("amazon.com"))
    mode = os.stat(path).st_mode & 0o777
    assert mode == 0o600


def test_load_flow_state_refuses_a_missing_file(tmp_path):
    with pytest.raises(enroll.AlexaEnrollError, match="no enrollment in progress"):
        enroll.load_flow_state(tmp_path / "cfg" / "enrollment.json")


def test_load_flow_state_refuses_garbage_state(tmp_path, monkeypatch):
    path = tmp_path / "enrollment.json"
    path.write_text(json.dumps({"domain": "amazon.com"}))  # no verifier
    with pytest.raises(enroll.AlexaEnrollError, match="unusable"):
        enroll.load_flow_state(path)


def test_restore_flow_rejects_a_broken_state_dict():
    with pytest.raises(enroll.AlexaEnrollError, match="unusable"):
        enroll.restore_flow({})


def test_code_from_redirect_extracts_the_code():
    flow = enroll.new_flow("amazon.com")
    assert enroll.code_from_redirect(flow, GOOD_URL) == "AUTHTOKEN"


@pytest.mark.parametrize(
    "url",
    [
        "http://www.amazon.com/ap/maplanding?openid.oa2.authorization_code=x",
        "https://evil.com/ap/maplanding?openid.oa2.authorization_code=x",
        "https://www.amazon.com/otherpage",
        "https://www.amazon.com/ap/maplanding",
    ],
)
def test_code_from_redirect_refuses_urls_that_are_not_the_maplanding(url):
    flow = enroll.new_flow("amazon.com")
    with pytest.raises(enroll.AlexaEnrollError, match="paste"):
        enroll.code_from_redirect(flow, url)


# ── display rows: never echo secrets ────────────────────────────────────


def test_flow_row_shows_the_url_but_not_the_verifier():
    row = enroll.flow_row(enroll.new_flow("amazon.com", serial="SERIAL-3"))
    assert row["domain"] == "amazon.com"
    assert row["serial"] == "SERIAL-3"
    assert row["enrollment_url"].startswith("https://www.amazon.com/ap/register")
    assert "code_verifier" not in json.dumps(row)
    assert "verifier" not in json.dumps(row)


def test_credentials_row_masks_the_refresh_token():
    creds = _fake_creds()
    row = enroll.credentials_row(creds)
    assert row["has_refresh_token"] is True
    assert row["has_mac_dms"] is True
    assert row["mac_dms_serial"] == "SERIAL-9"
    assert "rt-secret" not in json.dumps(row)


def test_credentials_row_survives_junk_attributes():
    class Stub:
        refresh_token = None

    row = enroll.credentials_row(Stub())
    assert row == {
        "serial": "",
        "customer_id": None,
        "domain": "",
        "has_refresh_token": False,
        "has_mac_dms": False,
        "mac_dms_serial": None,
    }


# ── the credentials file ────────────────────────────────────────────────


def _fake_creds():
    class Creds:
        refresh_token = "rt-secret"
        mac_dms = {"device_serial": "SERIAL-9", "private_key": "pk"}
        serial = "SERIAL-9"
        customer_id = "cid-1"
        domain = "amazon.com"

        @staticmethod
        def as_dict():
            return {
                "refresh_token": "rt-secret",
                "mac_dms": {"device_serial": "SERIAL-9", "private_key": "pk"},
                "serial": "SERIAL-9",
                "customer_id": "cid-1",
                "domain": "amazon.com",
            }

    return Creds()


def test_save_credentials_round_trip_and_owner_only(tmp_path):
    path = enroll.save_credentials(enroll.credentials_path(tmp_path), _fake_creds())
    assert path == tmp_path / "credentials.json"
    assert os.stat(path).st_mode & 0o777 == 0o600
    stored = json.loads(path.read_text())
    assert stored["refresh_token"] == "rt-secret"
    assert stored["saved_at"].startswith("20")


def test_load_credentials_rebuilds_the_record(tmp_path):
    path = enroll.save_credentials(enroll.credentials_path(tmp_path), _fake_creds())
    creds = enroll.load_credentials(path)
    assert creds.refresh_token == "rt-secret"
    assert creds.mac_dms["device_serial"] == "SERIAL-9"


def test_load_credentials_refuses_a_missing_file(tmp_path):
    with pytest.raises(enroll.AlexaEnrollError, match="no saved device credentials"):
        enroll.load_credentials(tmp_path / "credentials.json")


def test_load_credentials_refuses_garbage(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text("{not json")
    with pytest.raises(enroll.AlexaEnrollError, match="unreadable"):
        enroll.load_credentials(path)


def test_load_credentials_refuses_an_unknown_shape(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({"weird": 1}))
    with pytest.raises(enroll.AlexaEnrollError, match="unknown shape"):
        enroll.load_credentials(path)


def test_credentials_default_paths_live_in_the_config_dir(tmp_path):
    assert enroll.credentials_path(tmp_path) == tmp_path / "credentials.json"
    assert enroll.enrollment_state_path(tmp_path) == tmp_path / "enrollment.json"


# ── the credential exchange (network half, faked) ───────────────────────


class _FakeResponse:
    def __init__(self, body, status=200):
        self._body = body
        self.status = status

    async def json(self, content_type=None):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, body, status=200):
        self._body = body
        self._status = status
        self.posts = []
        self.closed = []

    def post(self, url, *args, **kwargs):
        self.posts.append(url)
        return _FakeResponse(self._body, self._status)

    async def close(self):
        self.closed.append(True)


_SUCCESS_BODY = {
    "response": {
        "success": {
            "tokens": {
                "bearer": {"refresh_token": "rt-secret"},
                "mac_dms": {"device_serial": "SERIAL-9"},
            },
            "extensions": {"customer_info": {"user_id": "cid-1"}},
        }
    }
}


def test_register_code_exchanges_the_code_for_credentials():
    flow = enroll.new_flow("amazon.com", serial="SERIAL-9")
    session = _FakeSession(_SUCCESS_BODY)
    creds = _run(enroll.register_code(flow, GOOD_URL, aio_session=session))
    assert creds.refresh_token == "rt-secret"
    assert creds.mac_dms["device_serial"] == "SERIAL-9"
    assert creds.customer_id == "cid-1"
    assert any("/auth/register" in url for url in session.posts)
    # a caller-supplied session is NEVER closed by the wrapper
    assert session.closed == []


def test_register_code_opens_and_closes_its_own_session():
    session = _FakeSession(_SUCCESS_BODY)
    fake_aiohttp = MagicMock()
    fake_aiohttp.ClientSession = MagicMock(return_value=session)
    flow = enroll.new_flow("amazon.com")
    with patch.object(enroll, "_registration_session", return_value=fake_aiohttp):
        creds = _run(enroll.register_code(flow, GOOD_URL))
    assert creds.refresh_token == "rt-secret"
    assert session.closed == [True]  # the wrapper ALWAYS closes its own session


def test_register_code_closes_its_own_session_on_failure():
    session = _FakeSession({})

    class ExplodingFlow:
        parse_redirect_url = MagicMock(return_value="code")

        async def async_register(self, session, code):
            raise RuntimeError("network down")

    fake_aiohttp = MagicMock()
    fake_aiohttp.ClientSession = MagicMock(return_value=session)
    with patch.object(enroll, "_registration_session", return_value=fake_aiohttp):
        with pytest.raises(enroll.AlexaEnrollError, match="network down"):
            _run(enroll.register_code(ExplodingFlow(), GOOD_URL))
    assert session.closed == [True]  # even on failure


def test_register_code_wraps_an_http_error_answer_in_enroll_error():
    flow = enroll.new_flow("amazon.com")
    session = _FakeSession({"response": {}}, status=200)
    import asyncio

    with pytest.raises(enroll.AlexaEnrollError, match="failed"):
        asyncio.run(enroll.register_code(flow, GOOD_URL, aio_session=session))


# ── activation (cookie minting, faked at the AlexaLogin boundary) ───────


def _fake_alexalogin(exchange=True, logged_in=True):
    login = MagicMock()
    login.exchange_token_for_cookies = AsyncMock(return_value=exchange)
    login.test_loggedin = AsyncMock(return_value=logged_in)
    login.save_cookiefile = AsyncMock(return_value=None)
    login.close = AsyncMock(return_value=None)
    return login


def test_activate_credentials_mints_verifies_and_saves_the_cookie(tmp_path):
    login = _fake_alexalogin()
    with patch("alexapy.AlexaLogin", MagicMock(return_value=login)):
        row = _run(enroll.activate_credentials(_fake_creds(), "u@example.com", "amazon.com", tmp_path))
    assert row == {"email": "u@example.com", "cookie_saved": True, "logged_in": True}
    login.exchange_token_for_cookies.assert_awaited_once()
    login.test_loggedin.assert_awaited_once()
    login.save_cookiefile.assert_awaited_once()
    login.close.assert_awaited_once()


def test_activate_credentials_reports_an_unverified_session(tmp_path):
    login = _fake_alexalogin(logged_in=False)
    with patch("alexapy.AlexaLogin", MagicMock(return_value=login)):
        row = _run(enroll.activate_credentials(_fake_creds(), "u@example.com", "amazon.com", tmp_path))
    assert row["cookie_saved"] is True
    assert row["logged_in"] is False


def test_activate_credentials_refuses_an_empty_email(tmp_path):
    with pytest.raises(enroll.AlexaEnrollError, match="email"):
        _run(enroll.activate_credentials(_fake_creds(), "  ", "amazon.com", tmp_path))


def test_activate_credentials_aborts_when_the_exchange_is_refused(tmp_path):
    login = _fake_alexalogin(exchange=False)
    with patch("alexapy.AlexaLogin", MagicMock(return_value=login)):
        with pytest.raises(enroll.AlexaEnrollError, match="refused the token"):
            _run(enroll.activate_credentials(_fake_creds(), "u@example.com", "amazon.com", tmp_path))
    login.save_cookiefile.assert_not_awaited()


# ── the CLI summary (pure) ──────────────────────────────────────────────

ACTIVATION = {"email": "u@example.com", "cookie_saved": True, "logged_in": True}


def test_enrollment_summary_composes_the_emit_row(tmp_path):
    creds = enroll.credentials_row(_fake_creds())
    summary = enroll.enrollment_summary(creds, ACTIVATION, Path("/cfg/credentials.json"))
    assert summary["method"] == "enroll"
    assert summary["email"] == "u@example.com"
    assert summary["domain"] == "amazon.com"
    assert summary["logged_in"] is True
    assert summary["cookie_saved"] is True
    assert "refresh_token" not in json.dumps(summary)
    assert "enrollment_state_file" not in summary


def test_enrollment_summary_can_carry_the_state_file(tmp_path):
    summary = enroll.enrollment_summary(
        enroll.credentials_row(_fake_creds()),
        ACTIVATION,
        Path("/cfg/credentials.json"),
        state_file=Path("/cfg/enrollment.json"),
    )
    assert summary["enrollment_state_file"] == "/cfg/enrollment.json"


def test_enrollment_summary_reflects_an_unverified_session():
    summary = enroll.enrollment_summary(
        enroll.credentials_row(_fake_creds()),
        {"email": "u@example.com", "cookie_saved": True, "logged_in": False},
        Path("/cfg/credentials.json"),
    )
    assert summary["logged_in"] is False


# ── the version guard (alexapy without secureauth) ──────────────────────


def test_new_flow_refuses_an_alexapy_without_secureauth(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "alexapy.secureauth", None)
    with pytest.raises(enroll.AlexaEnrollError, match="alexapy>=1.34"):
        enroll.new_flow("amazon.com")


def test_supported_domains_falls_back_to_the_builtin_list_without_alexapy(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "alexapy.secureauth", None)
    domains = enroll.supported_domains()
    assert "amazon.com" in domains
    assert "amazon.com.mx" in domains  # the fallback copy knows it
