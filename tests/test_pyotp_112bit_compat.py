"""Regression tests: pyotp >= 2.10 (NIST 112-bit minimum) compatibility.

pyotp 2.10 (2025) started enforcing NIST SP 800-63B's 112-bit minimum TOTP
key length and raises ``ValueError`` for the 80-bit (16 base32-char) secrets
Amazon's own authenticator setup issues. Since HMAC-SHA1 zero-pads any key
below the block size, zero-extending the decoded key to 112 bits produces
byte-identical HMAC input — and therefore identical codes.

These tests assert that behaviour, computed against an independent
RFC 6238 reference implementation (``hmac``/``hashlib`` directly) — not
against pyotp itself — for short secrets, and against pyotp for
already-compliant (>= 112-bit) secrets whose path is unchanged.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import struct

import pyotp
import pytest

from cli_anything.alexa.core import session

AMAZON_80BIT_SECRET = "JBSWY3DPEHPK3PXP"  # 16 base32 chars -> 10 raw bytes


# -- independent RFC 6238 reference (no pyotp) ---------------------------


def _reference_totp(secret_b32: str, at: int, step: int = 30, digits: int = 6) -> str:
    """RFC 6238 TOTP computed from the *decoded* key, no pyotp involved."""
    key = base64.b32decode(secret_b32, casefold=True)
    counter = int(at // step)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0xF
    n = (
        (mac[offset] & 0x7F) << 24
        | mac[offset + 1] << 16
        | mac[offset + 2] << 8
        | mac[offset + 3]
    )
    return str(10_000_000_000 + n % 10**digits)[-digits:]


# -- totp_row: short secrets work again and match the raw-key reference --


def test_totp_row_short_secret_matches_the_raw_key_reference():
    """An 80-bit Amazon secret codes exactly what the raw key computes."""
    row = session.totp_row(AMAZON_80BIT_SECRET, at=1_700_000_000)
    assert row["code"] == _reference_totp(AMAZON_80BIT_SECRET, 1_700_000_000)
    assert len(row["code"]) == 6
    assert row["interval"] == 30


def test_totp_row_short_secret_is_stable_within_a_step():
    at = 59
    assert session.totp_row(AMAZON_80BIT_SECRET, at=at)["code"] == (
        session.totp_row(AMAZON_80BIT_SECRET, at=at)["code"]
    )
    assert session.totp_row(AMAZON_80BIT_SECRET, at=at)["valid_for"] == 1


def test_totp_row_lowercase_and_spaced_secret_is_accepted():
    """Case/spacing normalisation still holds under the new decode path."""
    row = session.totp_row(" jbsw y3dp ehpk3pxp ", at=0)
    assert row["code"] == _reference_totp(AMAZON_80BIT_SECRET, 0)


def test_totp_row_compliant_secret_still_matches_pyotp():
    """Secrets pyotp already accepts must follow the unchanged pyotp path."""
    long_secret = base64.b32encode(b"\x01" * 14).decode("ascii")  # exactly 112 bits
    at = 1_700_000_000
    row = session.totp_row(long_secret, at=at)
    totp = pyotp.TOTP(long_secret)
    assert row["code"] == totp.at(float(at))
    # and equals the RFC 6238 reference too
    assert row["code"] == _reference_totp(long_secret, at)


def test_totp_row_still_rejects_junk_and_empty():
    with pytest.raises(session.AlexaSessionError, match="otp-secret"):
        session.totp_row("")
    with pytest.raises(session.AlexaSessionError, match="invalid TOTP secret"):
        session.totp_row("not base32 !!")


# -- the padded secret pyotp receives ------------------------------------


def test_padded_secret_decodes_to_the_raw_key_zero_extended():
    padded = session.totp_secret_for_pyotp(AMAZON_80BIT_SECRET)
    decoded = base64.b32decode(padded)
    raw = base64.b32decode(AMAZON_80BIT_SECRET)
    assert len(decoded) >= 14  # satisfies pyotp >= 2.10's 112-bit rule
    assert decoded == raw + b"\x00" * (len(decoded) - len(raw))


def test_padded_secret_is_accepted_by_pyotp_and_codes_identically():
    at = 1_700_000_000
    padded = session.totp_secret_for_pyotp(AMAZON_80BIT_SECRET)
    # Construction must not raise for pyotp >= 2.10, and the code must be
    # what pyotp itself computes for the padded key...
    assert pyotp.TOTP(padded).at(float(at)) == _reference_totp(AMAZON_80BIT_SECRET, at)


# -- fresh_login passes alexapy a secret its pyotp can construct ---------


class _RecordingLogin:
    """Minimal AlexaLogin stand-in that records the otp_secret it is given."""

    def __init__(self, *args, **kwargs):
        self.otp_secret = kwargs.get("otp_secret", "")
        self.status = {"login_successful": True}

    def set_totp(self, secret):
        self.set_totp_secret = secret
        return None

    async def login(self, *args, **kwargs):
        return None


def test_fresh_login_gives_alexapy_a_112bit_safe_secret(monkeypatch):
    captured: dict = {}

    def fake_import():
        def _ctor(url, email, password, outpath, otp_secret=""):
            captured["otp_secret"] = otp_secret
            return _RecordingLogin(url, email, password, outpath, otp_secret=otp_secret)

        return _ctor, object()

    monkeypatch.setattr(session, "_import_alexapy", fake_import)
    asyncio.run(
        session.fresh_login("you@example.com", "pw", otp_secret=AMAZON_80BIT_SECRET)
    )
    raw = base64.b32decode(AMAZON_80BIT_SECRET)
    given = base64.b32decode(captured["otp_secret"])
    # Construction inside alexapy must see a pyotp>=2.10-acceptable key that
    # is the original key zero-extended — same codes, no ValueError.
    assert given == raw + b"\x00" * (len(given) - len(raw))


def test_fresh_login_undecodable_secret_is_passed_through_unchanged(monkeypatch):
    """A secret that does not decode as base32 reaches alexapy verbatim, so
    alexapy reports the invalid key exactly as it has all along."""
    captured: dict = {}

    def fake_import():
        def _ctor(url, email, password, outpath, otp_secret=""):
            captured["otp_secret"] = otp_secret
            return _RecordingLogin(otp_secret=otp_secret)

        return _ctor, object()

    monkeypatch.setattr(session, "_import_alexapy", fake_import)
    asyncio.run(session.fresh_login("you@example.com", "pw", otp_secret="nope!!"))
    assert captured["otp_secret"] == "nope!!"


def test_fresh_login_without_secret_is_untouched(monkeypatch):
    captured: dict = {}

    def fake_import():
        def _ctor(url, email, password, outpath, otp_secret=""):
            captured["otp_secret"] = otp_secret
            return _RecordingLogin(otp_secret=otp_secret)

        return _ctor, object()

    monkeypatch.setattr(session, "_import_alexapy", fake_import)
    asyncio.run(session.fresh_login("you@example.com", "pw"))
    assert captured["otp_secret"] == ""
