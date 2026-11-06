"""Behavioural tests for the `auth enroll` CLI paths (0.15.0).

Contracts:
* `--json` never prompts — it needs `--email` + `--redirect-url` up front;
* the flow state is saved BEFORE any network call, and deleted once the
  enrollment completes (the PKCE verifier is single-use secret material);
* `--start` is a pure phase-1: URL out, state file written, no core
  coroutine ever launched;
* `--finish` resumes the saved state (its domain wins over the CLI's) and
  refuses cleanly when no enrollment is in progress;
* the summary row carries NO secret material, and an unverified session
  (`logged_in` False) still emits the row but exits non-zero;
* an unsupported region aborts before anything is written.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from cli_anything.alexa.alexa_cli import cli
from cli_anything.alexa.core import enroll as enroll_core

REDIRECT = "https://www.amazon.com/ap/maplanding?openid.oa2.authorization_code=AUTHCODE"
ACTIVATION = {"email": "u@example.com", "cookie_saved": True, "logged_in": True}


class _FakeCreds:
    """Stand-in `DeviceCredentials` (real as_dict → real summary row)."""

    refresh_token = "rt-secret"
    mac_dms = {"device_serial": "SERIAL-9", "private_key": "pk"}
    serial = "SERIAL-9"
    customer_id = "cid-1"
    domain = "amazon.co.uk"

    @staticmethod
    def as_dict():
        return {
            "refresh_token": "rt-secret",
            "mac_dms": {"device_serial": "SERIAL-9", "private_key": "pk"},
            "serial": "SERIAL-9",
            "customer_id": "cid-1",
            "domain": "amazon.co.uk",
        }


@contextlib.contextmanager
def _enrollment_patched(activation=ACTIVATION):
    """Patch the network half at the enrol module boundary.

    `register_code` still runs (against a faked aiohttp session inside the
    fake flow?) … no: the CLI needs a real-ish flow to print the URL. The
    URL generation is alexapy's own (pure), so it stays; only the exchange
    and the activation are faked.
    """
    calls = {"register": 0, "activate": 0}

    def fake_register(flow, pasted_url, aio_session=None):
        async def _c():
            calls["register"] += 1
            return _FakeCreds()

        return _c()

    def fake_activate(creds, em, region, config_dir):
        async def _c():
            calls["activate"] += 1
            return {"email": em, "cookie_saved": True, "logged_in": activation.get("logged_in", True)}

        return _c()

    with (
        patch.object(enroll_core, "register_code", fake_register),
        patch.object(enroll_core, "activate_credentials", fake_activate),
    ):
        yield calls


@pytest.fixture(autouse=True)
def _isolate_profile(monkeypatch, tmp_path):
    """Point project.py's import-time CONFIG_DIR at a private tmp dir.

    project.py resolves CONFIG_DIR/DEFAULT_CONFIG_PATH at import time, so a
    test that only patches $HOME would still read (and worse, WRITE) the
    invoking user's real profile.
    """
    home = tmp_path / "isohome"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        "cli_anything.alexa.core.project.DEFAULT_CONFIG_PATH",
        tmp_path / "profile" / "config.json",
    )

def _home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return home / ".config" / "cli-anything-alexa"


def _cfg(monkeypatch):
    return Path(os.environ["HOME"]) / ".config/cli-anything-alexa"


def _invoke(args, input=None):
    return CliRunner().invoke(
        cli, args, input=input, obj={}, catch_exceptions=False
    )


def _ok(args, **kw):
    result = _invoke(args, **kw)
    assert result.exit_code == 0, result.output
    return result


# ── happy path (one shot, --json) ───────────────────────────────────────


def test_enroll_exchanges_verifies_and_saves(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    with _enrollment_patched():
        result = _ok(
            [
                "--json",
                "--email",
                "u@example.com",
                "--url",
                "amazon.co.uk",
                "auth",
                "enroll",
                "--redirect-url",
                REDIRECT,
            ]
        )
    summary = json.loads(result.output)
    assert summary["method"] == "enroll"
    assert summary["email"] == "u@example.com"
    assert summary["domain"] == "amazon.co.uk"  # the flow's normalized domain
    assert summary["serial"] == "SERIAL-9"
    assert summary["customer_id"] == "cid-1"
    assert summary["logged_in"] is True
    assert summary["cookie_saved"] is True
    config_dir = _cfg(monkeypatch)
    assert summary["credentials_file"] == str(config_dir / "credentials.json")
    assert "refresh_token" not in result.output  # secrets never reach stdout
    # the PKCE state is consumed on success
    assert not (config_dir / "enrollment.json").exists()


def test_enroll_writes_the_credentials_file_readable_only_by_owner(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    with _enrollment_patched():
        _ok(
            [
                "--json",
                "--email",
                "u@example.com",
                "auth",
                "enroll",
                "--redirect-url",
                REDIRECT,
            ]
        )
    credentials = Path(os.environ["HOME"]) / ".config/cli-anything-alexa/credentials.json"
    assert credentials.is_file()
    assert os.stat(credentials).st_mode & 0o777 == 0o600


# ── argument refusals (before anything is written) ──────────────────────


def test_enroll_needs_an_email_with_json(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    result = _invoke(["--json", "auth", "enroll", "--redirect-url", REDIRECT])
    assert result.exit_code == 1
    assert "--email is required with --json" in result.output


def test_enroll_json_without_a_redirect_needs_redirect_url(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    with _enrollment_patched():
        result = _ok(["--json", "--email", "u@example.com", "auth", "enroll"])
    row = json.loads(result.output)
    assert row["started"] is False
    assert row["enrollment_url"].startswith("https://www.amazon.co.uk/ap/register")
    assert row["hint"]


def test_enroll_json_without_redirect_still_saves_the_flow_state(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    with _enrollment_patched():
        _ok(["--json", "--email", "u@example.com", "auth", "enroll"])
    config_dir = _cfg(monkeypatch)
    assert (config_dir / "enrollment.json").exists()


def test_enroll_json_without_email_would_prompt_interactively(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    with _enrollment_patched():
        result = _invoke(
            ["auth", "enroll", "--redirect-url", REDIRECT], input="u@example.com\n"
        )
    assert result.exit_code == 0, result.output
    # the email was prompted, the paste skipped (a redirect was given) and the
    # plaintext summary row printed:
    assert "Amazon account email: u@example.com" in result.output
    assert "method: enroll" in result.output
    assert "SERIAL-9" in result.output


def test_enroll_refuses_an_unsupported_region_before_any_writing(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    with _enrollment_patched() as calls:
        result = _invoke(
            [
                "--json",
                "--email",
                "u@example.com",
                "auth",
                "enroll",
                "--url",
                "amazon.pl",
                "--redirect-url",
                REDIRECT,
            ]
        )
    assert result.exit_code == 1
    assert "not available" in result.output
    assert calls == {"register": 0, "activate": 0}


# ── phase 1 (`--start`) ─────────────────────────────────────────────────


def test_enroll_start_prints_the_url_and_saves_the_state(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    with _enrollment_patched() as calls:
        result = _ok(["--json", "--email", "u@example.com", "auth", "enroll", "--start"])
    row = json.loads(result.output)
    assert row["started"] is True
    assert row["enrollment_url"].startswith("https://")
    assert row["code_challenge_len"] == 43
    assert row["hint"].startswith("run `auth enroll --finish --redirect-url")
    config_dir = _cfg(monkeypatch)
    assert (config_dir / "enrollment.json").exists()
    assert calls == {"register": 0, "activate": 0}  # pure phase-1: no network


def test_enroll_start_state_file_is_owner_only(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    with _enrollment_patched():
        _ok(["--json", "--email", "u@example.com", "auth", "enroll", "--start"])
    state = Path(os.environ["HOME"]) / ".config/cli-anything-alexa/enrollment.json"
    assert os.stat(state).st_mode & 0o777 == 0o600


def test_enroll_start_state_never_leaks_the_verifier(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    with _enrollment_patched():
        result = _ok(["--json", "--email", "u@example.com", "auth", "enroll", "--start"])
    state = Path(os.environ["HOME"]) / ".config/cli-anything-alexa/enrollment.json"
    verifier = json.loads(state.read_text())["code_verifier"]
    assert verifier not in result.output


# ── phase 2 (`--finish`) ────────────────────────────────────────────────


def test_enroll_finish_resumes_the_saved_state(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    with _enrollment_patched():
        _ok(["--json", "--email", "u@example.com", "auth", "enroll", "--start"])
        result = _ok(
            [
                "--json",
                "--email",
                "u@example.com",
                "auth",
                "enroll",
                "--finish",
                "--redirect-url",
                REDIRECT,
            ]
        )
    summary = json.loads(result.output)
    assert summary["method"] == "enroll"
    assert summary["logged_in"] is True
    config_dir = _cfg(monkeypatch)
    assert not (config_dir / "enrollment.json").exists()


def test_enroll_finish_refuses_without_an_enrollment(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    result = _invoke(
        [
            "--json",
            "--email",
            "u@example.com",
            "auth",
            "enroll",
            "--finish",
            "--redirect-url",
            REDIRECT,
        ]
    )
    assert result.exit_code == 1
    assert "no enrollment in progress" in result.output


def test_enroll_finish_requires_the_redirect_url_with_json(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    with _enrollment_patched():
        _ok(["--json", "--email", "u@example.com", "auth", "enroll", "--start"])
        result = _invoke(["--json", "--email", "u@example.com", "auth", "enroll", "--finish"])
    assert result.exit_code == 1
    assert "--redirect-url is required with --json --finish" in result.output


# ── failure semantics ───────────────────────────────────────────────────


def test_enroll_reports_an_unverified_session_and_exits_nonzero(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    with _enrollment_patched(activation={"email": "u@example.com", "cookie_saved": True, "logged_in": False}):
        result = _invoke(
            ["--json", "--email", "u@example.com", "auth", "enroll", "--redirect-url", REDIRECT]
        )
    assert result.exit_code == 1
    assert json.loads(result.output)["logged_in"] is False


def test_enroll_survives_a_garbled_state_file(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    config_dir = _cfg(monkeypatch)
    config_dir.mkdir(parents=True)
    (config_dir / "enrollment.json").write_text("{not json")
    with _enrollment_patched():
        result = _invoke(
            [
                "--json",
                "--email",
                "u@example.com",
                "auth",
                "enroll",
                "--finish",
                "--redirect-url",
                REDIRECT,
            ]
        )
    assert result.exit_code == 1
    assert "unreadable" in result.output


def test_enroll_interactive_two_step_round_trip(monkeypatch, tmp_path):
    """No --json, no flags: the URL is printed, then the paste is prompted."""
    _home(monkeypatch, tmp_path)
    with _enrollment_patched():
        result = _invoke(
            ["--email", "u@example.com", "auth", "enroll"],
            input=f"{REDIRECT}\n",
        )
    assert result.exit_code == 0, result.output
    assert "https://www.amazon.co.uk/ap/register" in result.output
    assert "Paste the redirect URL" in result.output
