"""Behavioural tests for the session-lifecycle CLI paths (0.7.0 refine).

`auth ping`, `auth refresh`, `auth logout` and `auth totp`. Contracts:

* read-only commands (`ping`, `refresh`) need no `--yes` and answer as a
  JSON row; they exit non-zero when the check fails (the status/whoami
  pattern);
* `auth logout` is destructive, so it previews by default (`dry_run` +
  `would_remove` + hint) and only deletes on `--yes`, reporting `verified`
  from a fresh disk re-read;
* `auth logout` REFUSES under `--cookie-dir` (read-in-place): that cookie
  belongs to another tool, and deleting it would break their session;
* `auth totp` needs no session and never touches the network; a bad secret
  aborts with the harness error style, not a traceback.
"""

from __future__ import annotations

import contextlib
import json
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from cli_anything.alexa.alexa_cli import cli
from cli_anything.alexa.core import session as session_core

PING = {"ok": True, "detail": {"hmac": "abc"}}
REFRESHED = {"refreshed": True, "has_refresh_token": True, "expires_at": "2026-09-09T13:00:00"}
UNREFRESHED = {"refreshed": False, "has_refresh_token": True, "expires_at": None}


def _invoke(args, obj=None, catch_exceptions=False):
    return CliRunner().invoke(
        cli, args, obj=obj or {}, catch_exceptions=catch_exceptions
    )


def _home(monkeypatch, tmp_path):
    """A fake $HOME so the config dir resolves to <tmp>/.config/cli-anything-alexa
    WITHOUT --cookie-dir (the flag means read-in-place, which `auth logout`
    must refuse)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home / ".config" / "cli-anything-alexa"


@contextlib.contextmanager
def _stub_run(*return_values):
    """Patch ``_login``/``_run`` to answer each call in turn."""
    answers = list(return_values)

    def fake_run(_ctx, coro):
        if hasattr(coro, "close"):
            coro.close()
        return answers.pop(0) if answers else None

    with (
        patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()),
        patch("cli_anything.alexa.alexa_cli._run", side_effect=fake_run),
    ):
        yield


# ── auth ping ───────────────────────────────────────────────────────────


def test_ping_emits_the_health_row():
    with _stub_run(PING):
        result = _invoke(["--json", "--email", "u@example.com", "auth", "ping"])
    assert result.exit_code == 0
    assert json.loads(result.output) == PING


def test_ping_exits_nonzero_when_the_session_is_dead():
    with _stub_run({"ok": False, "detail": None}):
        result = _invoke(["--json", "--email", "u@example.com", "auth", "ping"])
    assert result.exit_code == 1
    assert json.loads(result.output)["ok"] is False


# ── auth refresh ────────────────────────────────────────────────────────


def test_refresh_reports_a_successful_exchange():
    with _stub_run(REFRESHED):
        result = _invoke(["--json", "--email", "u@example.com", "auth", "refresh"])
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["refreshed"] is True
    assert parsed["email"] == "u@example.com"


def test_refresh_exits_nonzero_when_the_exchange_fails():
    with _stub_run(UNREFRESHED):
        result = _invoke(["--json", "--email", "u@example.com", "auth", "refresh"])
    assert result.exit_code == 1
    assert json.loads(result.output)["refreshed"] is False


# ── auth logout ─────────────────────────────────────────────────────────


def test_logout_previews_which_files_would_go_without_yes(monkeypatch, tmp_path):
    config_dir = _home(monkeypatch, tmp_path)
    config_dir.mkdir(parents=True)
    (config_dir / "alexa_media.u@example.com.pickle").write_text("cookie")
    with patch("cli_anything.alexa.alexa_cli.session_core.logout_session") as gone:
        result = _invoke(["--json", "--email", "u@example.com", "auth", "logout"])
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["dry_run"] is True
    assert parsed["would_remove"] == [str(config_dir / "alexa_media.u@example.com.pickle")]
    assert "--yes" in parsed["hint"]
    gone.assert_not_called()  # nothing deleted in preview


def test_logout_deletes_and_verifies_with_yes(monkeypatch, tmp_path):
    config_dir = _home(monkeypatch, tmp_path)
    config_dir.mkdir(parents=True)
    (config_dir / "alexa_media.u@example.com.pickle").write_text("cookie")
    result = _invoke(
        ["--json", "--email", "u@example.com", "auth", "logout", "--yes"],
    )
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["verified"] is True
    assert parsed["removed"] == [str(config_dir / "alexa_media.u@example.com.pickle")]
    assert not (tmp_path / "alexa_media.u@example.com.pickle").exists()


def test_logout_reports_an_unverified_delete_as_a_failure(monkeypatch, tmp_path):
    # A directory sitting where the cookie file should be: unlink fails.
    config_dir = _home(monkeypatch, tmp_path)
    stuck = config_dir / "alexa_media.u@example.com.pickle"
    stuck.mkdir(parents=True)
    (stuck / "inside").write_text("x")
    result = _invoke(
        ["--json", "--email", "u@example.com", "auth", "logout", "--yes"],
        catch_exceptions=True,
    )
    assert result.exit_code == 1  # friendly failure, not a silent success
    assert json.loads(result.output)["verified"] is False


def test_logout_refuses_to_delete_a_cookie_it_reads_in_place():
    with patch("cli_anything.alexa.alexa_cli.session_core.logout_session") as gone:
        result = _invoke(
            ["--json", "--email", "u@example.com", "--cookie-dir", "/config", "auth", "logout", "--yes"],
        )
    assert result.exit_code == 1
    assert "--cookie-dir" in result.output
    gone.assert_not_called()


def test_logout_needs_an_email():
    result = _invoke(["--json", "auth", "logout", "--yes"], obj={})
    assert result.exit_code == 1
    assert "email" in result.output


def test_logout_is_a_real_filesystem_write_so_yes_runs_the_core(monkeypatch, tmp_path):
    """The --yes path is NOT stubbed: it deletes through core/logout_session."""
    config_dir = _home(monkeypatch, tmp_path)
    storage = config_dir / ".storage"
    storage.mkdir(parents=True)
    (storage / "alexa_media.u@example.com.cookies").write_text("cookie")
    result = _invoke(["--json", "--email", "u@example.com", "auth", "logout", "--yes"])
    parsed = json.loads(result.output)
    assert parsed["verified"] is True
    assert session_core.logout_plan(config_dir, "u@example.com")["present"] == []


# ── auth totp ───────────────────────────────────────────────────────────


def test_totp_prints_a_six_digit_code_and_validity():
    result = _invoke(["--json", "auth", "totp", "--otp-secret", "JBSWY3DPEHPK3PXP"])
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert len(parsed["code"]) == 6
    assert parsed["interval"] == 30
    assert 1 <= parsed["valid_for"] <= 30


def test_totp_aborts_cleanly_on_a_bad_secret():
    result = _invoke(["--json", "auth", "totp", "--otp-secret", "not base32 !!"])
    assert result.exit_code == 1
    assert "error:" in result.output


def test_totp_requires_the_secret():
    result = _invoke(["--json", "auth", "totp"])
    assert result.exit_code != 0


# ── workflow: status → ping → refresh → logout round-trip ───────────────


def test_lifecycle_workflow_end_to_end(monkeypatch, tmp_path):
    """The four lifecycle commands read one coherent session story off disk:
    ping confirms the live session, refresh renews the token, logout deletes
    every cookie file and the follow-up plan shows nothing left."""
    config_dir = _home(monkeypatch, tmp_path)
    storage = config_dir / ".storage"
    storage.mkdir(parents=True)
    (storage / "alexa_media.u@example.com.pickle").write_text("cookie")
    with _stub_run(PING):
        ping = _invoke(["--json", "--email", "u@example.com", "auth", "ping"])
    assert ping.exit_code == 0 and json.loads(ping.output)["ok"] is True
    with _stub_run(REFRESHED):
        refresh = _invoke(["--json", "--email", "u@example.com", "auth", "refresh"])
    assert refresh.exit_code == 0 and json.loads(refresh.output)["refreshed"] is True
    plan = _invoke(["--json", "--email", "u@example.com", "auth", "logout"])
    assert json.loads(plan.output)["would_remove"] == [
        str(storage / "alexa_media.u@example.com.pickle")
    ]
    out = _invoke(["--json", "--email", "u@example.com", "auth", "logout", "--yes"])
    assert json.loads(out.output)["verified"] is True
    assert session_core.logout_plan(config_dir, "u@example.com")["present"] == []
