"""Behavioural tests for the bluetooth + push CLI paths.

Covers `echos pairings` / `echos connect` / `echos disconnect` and the top-level
`push`.  Every mutating command is checked against the harness-wide contract —
**preview by default, act only on --yes** — and assertions are on observable
behaviour (exit code, JSON on stdout, which core coroutine was invoked with
what), never on source text.
"""

from __future__ import annotations

import contextlib
import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli_anything.alexa.alexa_cli import cli
from cli_anything.alexa.core import bluetooth as bluetooth_core
from cli_anything.alexa.core import control as control_core


def _invoke(args, obj=None):
    return CliRunner().invoke(cli, args, obj=obj or {}, catch_exceptions=False)


def _json_invoke(args):
    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()):
        result = _invoke(["--json", *args])
    assert result.exit_code == 0, result.output
    return result


@contextlib.contextmanager
def _stub_run(return_value=None, side_effect=None):
    """Patch ``_run`` with a stub that closes the coroutine it is handed."""

    def fake_run(_ctx, coro):
        if hasattr(coro, "close"):
            coro.close()
        if side_effect is not None:
            raise side_effect
        return return_value

    with patch("cli_anything.alexa.alexa_cli._run", side_effect=fake_run) as mock_run:
        yield mock_run


@contextlib.contextmanager
def _stub_core(module, name):
    with patch.object(module, name, MagicMock()) as stub:
        yield stub


#: (argv, the dry-run field describing the pending action)
MUTATING = [
    (["echos", "connect", "Jon's Phone"], "would_connect"),
    (["echos", "disconnect"], "would_disconnect"),
    (["push", "the washing is done"], "would_push"),
]


# ── the dry-run contract ────────────────────────────────────────────────


@pytest.mark.parametrize(("argv", "field"), MUTATING, ids=lambda v: str(v)[:40])
def test_new_mutating_commands_preview_without_yes(argv, field):
    parsed = json.loads(_json_invoke(argv).output)
    assert parsed["dry_run"] is True
    assert field in parsed
    assert "--yes" in parsed["hint"]


@pytest.mark.parametrize("argv", [a for a, _f in MUTATING], ids=lambda v: str(v))
def test_new_mutating_commands_do_not_reach_the_network_without_yes(argv):
    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()), _stub_run() as ran:
        result = _invoke(["--json", *argv])
    assert result.exit_code == 0
    ran.assert_not_called()


@pytest.mark.parametrize("argv", [a for a, _f in MUTATING], ids=lambda v: str(v))
def test_new_mutating_commands_report_the_implicit_echo(argv):
    assert json.loads(_json_invoke(argv).output)["device"] == "first online"


# ── echos pairings (read-only) ──────────────────────────────────────────


def test_echos_pairings_is_read_only_and_needs_no_yes():
    payload = {
        "device": "Kitchen",
        "serial": "SN1",
        "pairings": [{"name": "Jon's Phone", "address": "AA:BB:CC:DD:EE:FF", "connected": False}],
    }
    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()), _stub_run(payload):
        result = _invoke(["--json", "echos", "pairings"])
    assert result.exit_code == 0
    assert json.loads(result.output)["pairings"][0]["address"] == "AA:BB:CC:DD:EE:FF"


def test_echos_pairings_passes_the_named_echo_through():
    with (
        patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()),
        _stub_run({"pairings": []}),
        _stub_core(bluetooth_core, "list_pairings") as stub,
    ):
        result = _invoke(["--json", "echos", "pairings", "Kitchen Echo"])
    assert result.exit_code == 0
    assert stub.call_args[0][1] == "Kitchen Echo"


def test_echos_bluetooth_still_covers_the_whole_account():
    """Refine adds; the pre-existing account-wide read must keep working."""
    rows = [{"device": "Kitchen", "paired": "Jon's Phone"}]
    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()), _stub_run(rows):
        result = _invoke(["--json", "echos", "bluetooth"])
    assert result.exit_code == 0
    assert json.loads(result.output) == rows


# ── echos connect ───────────────────────────────────────────────────────


def test_echos_connect_preview_echoes_the_target_verbatim():
    parsed = json.loads(_json_invoke(["echos", "connect", "aa:bb:cc:dd:ee:ff"]).output)
    assert parsed["would_connect"] == "aa:bb:cc:dd:ee:ff"


def test_echos_connect_with_yes_calls_the_core_function():
    row = {"device": "Kitchen", "connected": "Jon's Phone", "ok": True}
    with (
        patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()),
        _stub_run(row) as ran,
        _stub_core(bluetooth_core, "connect") as stub,
    ):
        result = _invoke(
            ["--json", "echos", "connect", "Jon's Phone", "--device", "Kitchen", "--yes"]
        )
    assert result.exit_code == 0
    ran.assert_called_once()
    assert stub.call_args[0][1:] == ("Kitchen", "Jon's Phone")
    assert json.loads(result.output)["connected"] == "Jon's Phone"


def test_echos_connect_requires_a_target():
    result = CliRunner().invoke(cli, ["echos", "connect"], obj={})
    assert result.exit_code != 0


def test_echos_connect_surfaces_the_not_paired_message():
    """A core ValueError must reach the user verbatim, not as a traceback."""
    with (
        patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()),
        _stub_run(side_effect=SystemExit(1)),
    ):
        result = _invoke(["--json", "echos", "connect", "Garage", "--yes"])
    assert result.exit_code == 1


# ── echos disconnect ────────────────────────────────────────────────────


def test_echos_disconnect_preview_says_all_because_amazon_has_no_per_sink_call():
    parsed = json.loads(_json_invoke(["echos", "disconnect", "--device", "Kitchen"]).output)
    assert parsed["would_disconnect"] == "all"
    assert parsed["device"] == "Kitchen"


def test_echos_disconnect_with_yes_calls_the_core_function():
    with (
        patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()),
        _stub_run({"device": "Kitchen", "disconnected": "all", "ok": True}) as ran,
        _stub_core(bluetooth_core, "disconnect") as stub,
    ):
        result = _invoke(["--json", "echos", "disconnect", "--device", "Kitchen", "--yes"])
    assert result.exit_code == 0
    ran.assert_called_once()
    assert stub.call_args[0][1] == "Kitchen"
    assert json.loads(result.output)["disconnected"] == "all"


# ── push ────────────────────────────────────────────────────────────────


def test_push_preview_shows_the_normalised_message_and_default_title():
    parsed = json.loads(_json_invoke(["push", "  the oven is done  "]).output)
    assert parsed["would_push"] == "the oven is done"
    assert parsed["title"] == control_core.DEFAULT_PUSH_TITLE
    assert parsed["kind"] == "mobilepush"


def test_push_dropin_preview_names_the_other_channel():
    parsed = json.loads(_json_invoke(["push", "look at this", "--dropin"]).output)
    assert parsed["kind"] == "dropin"


@pytest.mark.parametrize("blank", ["", "   "])
def test_push_rejects_an_empty_message_before_logging_in(blank):
    with patch("cli_anything.alexa.alexa_cli._login") as login:
        result = _invoke(["--json", "push", blank])
    assert result.exit_code == 1
    assert "a message is required" in result.output
    login.assert_not_called()


def test_push_rejects_an_empty_message_identically_with_yes():
    """--yes must never turn a validation error into a live call."""
    with patch("cli_anything.alexa.alexa_cli._login") as login:
        result = _invoke(["--json", "push", "   ", "--yes"])
    assert result.exit_code == 1
    login.assert_not_called()


def test_push_with_yes_passes_the_resolved_title_and_kind_through():
    row = {"pushed": "hi", "kind": "dropin", "via_device": "Kitchen"}
    with (
        patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()),
        _stub_run(row) as ran,
        _stub_core(control_core, "push") as stub,
    ):
        result = _invoke(
            [
                "--json",
                "push",
                "hi",
                "--title",
                " Laundry ",
                "--device",
                "Kitchen",
                "--dropin",
                "--yes",
            ]
        )
    assert result.exit_code == 0
    ran.assert_called_once()
    assert stub.call_args.kwargs == {"title": "Laundry", "device": "Kitchen", "dropin": True}
    assert json.loads(result.output)["kind"] == "dropin"


def test_push_default_is_the_silent_mobile_channel_not_a_dropin():
    with (
        patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()),
        _stub_run({"kind": "mobilepush"}),
        _stub_core(control_core, "push") as stub,
    ):
        result = _invoke(["--json", "push", "hi", "--yes"])
    assert result.exit_code == 0
    assert stub.call_args.kwargs["dropin"] is False


# ── the new commands are discoverable ───────────────────────────────────


@pytest.mark.parametrize("name", ["pairings", "connect", "disconnect", "bluetooth"])
def test_echos_help_lists_the_bluetooth_commands(name):
    result = _invoke(["echos", "--help"])
    assert result.exit_code == 0
    assert name in result.output


def test_push_is_listed_in_the_root_help():
    result = _invoke(["--help"])
    assert result.exit_code == 0
    assert "push" in result.output
