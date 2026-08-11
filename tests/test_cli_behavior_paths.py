"""Behavioural tests for the `run` (behaviours/sequences) and `activity` CLI paths.

Every `run` verb makes a speaker do something, so each is checked against the
harness-wide contract — **preview by default, act only on --yes** — plus
validation-before-login and the `--queue-delay` pass-through.  The `activity`
reads are checked for their filter/limit plumbing, and `activity clear` (the one
destructive call in that group) for its dry-run guard.

Assertions are on observable behaviour — exit code, JSON on stdout, which core
coroutine was invoked with what — never on source text.
"""

from __future__ import annotations

import contextlib
import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli_anything.alexa.alexa_cli import cli
from cli_anything.alexa.core import activity as activity_core
from cli_anything.alexa.core import sequences as sequences_core


def _invoke(args, obj=None):
    return CliRunner().invoke(cli, args, obj=obj or {}, catch_exceptions=False)


def _json_invoke(args):
    """Invoke with a stubbed login and assert a clean exit."""
    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()):
        result = _invoke(["--json", *args])
    assert result.exit_code == 0, result.output
    return result


@contextlib.contextmanager
def _stub_run(return_value=None, side_effect=None):
    """Patch the CLI's ``_run`` with a stub that *closes* the coroutine it gets.

    The command builds its core coroutine eagerly; a plain MagicMock would leave
    it un-awaited and raise ``RuntimeWarning`` from whichever unrelated test
    happens to trigger the GC.
    """

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
    """Replace an async core function with a plain MagicMock (records the call)."""
    with patch.object(module, name, MagicMock()) as stub:
        yield stub


#: (argv, the dry-run field that must describe the pending action, core function)
MUTATING_RUN_COMMANDS = [
    (["run", "command", "turn off the kitchen lights"], "command", "run_command"),
    (["run", "sequence", "weather"], "sequence", "run_sequence"),
    (["run", "sound", "doorbell"], "sound", "play_sound"),
    (["run", "skill", "amzn1.ask.1p.tellalexa"], "skill", "run_skill"),
]


# ── the dry-run contract ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("argv", "field", "_core"), MUTATING_RUN_COMMANDS, ids=lambda v: str(v)[:40]
)
def test_every_run_verb_previews_without_yes(argv, field, _core):
    parsed = json.loads(_json_invoke(argv).output)
    assert parsed["dry_run"] is True
    assert field in parsed
    assert "--yes" in parsed["hint"]


@pytest.mark.parametrize("argv", [a for a, _f, _c in MUTATING_RUN_COMMANDS], ids=lambda v: str(v))
def test_no_run_verb_reaches_the_network_without_yes(argv):
    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()), _stub_run() as ran:
        result = _invoke(["--json", *argv])
    assert result.exit_code == 0
    ran.assert_not_called()


@pytest.mark.parametrize("argv", [a for a, _f, _c in MUTATING_RUN_COMMANDS], ids=lambda v: str(v))
def test_run_verb_without_a_device_reports_the_implicit_target(argv):
    """ "first online" is the documented default target, same as media/announce."""
    assert json.loads(_json_invoke(argv).output)["device"] == "first online"


@pytest.mark.parametrize("argv", [a for a, _f, _c in MUTATING_RUN_COMMANDS], ids=lambda v: str(v))
def test_run_verb_echoes_the_named_device_in_the_preview(argv):
    parsed = json.loads(_json_invoke([*argv, "--device", "Kitchen Echo"]).output)
    assert parsed["device"] == "Kitchen Echo"


@pytest.mark.parametrize(
    ("argv", "field", "core"), MUTATING_RUN_COMMANDS, ids=lambda v: str(v)[:40]
)
def test_run_verb_with_yes_calls_its_core_function(argv, field, core):
    with (
        patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()),
        _stub_run({"ok": True}) as ran,
        _stub_core(sequences_core, core) as stub,
    ):
        result = _invoke(["--json", *argv, "--device", "Kitchen", "--yes"])
    assert result.exit_code == 0
    ran.assert_called_once()
    stub.assert_called_once()
    assert stub.call_args[0][1] == "Kitchen"


# ── normalisation happens in the CLI, before any login ──────────────────


def test_run_sequence_resolves_the_alias_in_the_preview():
    parsed = json.loads(_json_invoke(["run", "sequence", "good night"]).output)
    assert parsed["sequence"] == "Alexa.GoodNight.Play"


def test_run_sound_resolves_the_alias_in_the_preview():
    parsed = json.loads(_json_invoke(["run", "sound", "air horn"]).output)
    assert parsed["sound"] == "air_horn_03"


def test_run_command_trims_the_utterance_in_the_preview():
    parsed = json.loads(_json_invoke(["run", "command", "  what's the weather  "]).output)
    assert parsed["command"] == "what's the weather"


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["run", "command", "   "], "a command is required"),
        (["run", "sequence", "make-tea"], "unknown sequence"),
        (["run", "sound", "not a sound!"], "unknown sound"),
        (["run", "skill", "nope"], "not an Alexa skill id"),
    ],
    ids=["command", "sequence", "sound", "skill"],
)
def test_run_verbs_reject_bad_input_before_logging_in(argv, expected):
    with patch("cli_anything.alexa.alexa_cli._login") as login:
        result = _invoke(["--json", *argv])
    assert result.exit_code == 1
    assert expected in result.output
    login.assert_not_called()


@pytest.mark.parametrize(
    "argv",
    [
        ["run", "command", "   ", "--yes"],
        ["run", "sequence", "make-tea", "--yes"],
        ["run", "sound", "not a sound!", "--yes"],
        ["run", "skill", "nope", "--yes"],
    ],
    ids=["command", "sequence", "sound", "skill"],
)
def test_run_verbs_reject_bad_input_identically_with_yes(argv):
    """--yes must never turn a validation error into a live call."""
    with patch("cli_anything.alexa.alexa_cli._login") as login:
        result = _invoke(["--json", *argv])
    assert result.exit_code == 1
    login.assert_not_called()


# ── --queue-delay ───────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["soon", "-1"])
def test_queue_delay_is_validated_before_logging_in(bad):
    with patch("cli_anything.alexa.alexa_cli._login") as login:
        result = _invoke(["--json", "run", "sound", "bell", "--queue-delay", bad])
    assert result.exit_code == 1
    assert "queue delay" in result.output
    login.assert_not_called()


def test_queue_delay_is_passed_through_to_the_core_call():
    with (
        patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()),
        _stub_run({"ok": True}),
        _stub_core(sequences_core, "run_command") as stub,
    ):
        result = _invoke(
            ["--json", "run", "command", "good morning", "--queue-delay", "2.5", "--yes"]
        )
    assert result.exit_code == 0
    assert stub.call_args[0][3] == 2.5


def test_unspecified_queue_delay_is_none_so_alexapys_own_default_survives():
    """alexapy's default differs per call (0 vs 1.5) — the CLI must not flatten it."""
    with (
        patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()),
        _stub_run({"ok": True}),
        _stub_core(sequences_core, "play_sound") as stub,
    ):
        result = _invoke(["--json", "run", "sound", "bell", "--yes"])
    assert result.exit_code == 0
    assert stub.call_args[0][3] is None


# ── run catalog (no account needed) ─────────────────────────────────────


def test_run_catalog_json_lists_both_kinds_without_logging_in():
    with patch("cli_anything.alexa.alexa_cli._login") as login:
        result = _invoke(["--json", "run", "catalog"])
    assert result.exit_code == 0
    login.assert_not_called()
    parsed = json.loads(result.output)
    assert [r["name"] for r in parsed["sequences"]] == sorted(sequences_core.SEQUENCE_COMMANDS)
    assert [r["name"] for r in parsed["sounds"]] == sorted(sequences_core.SOUND_ALIASES)


@pytest.mark.parametrize(
    ("kind", "present", "absent"),
    [("sequences", "weather", "doorbell"), ("sounds", "doorbell", "weather")],
)
def test_run_catalog_kind_filters_the_table(kind, present, absent):
    result = _invoke(["run", "catalog", "--kind", kind])
    assert result.exit_code == 0
    assert present in result.output
    assert absent not in result.output


def test_run_catalog_renders_a_table_in_human_mode():
    result = _invoke(["run", "catalog"])
    assert result.exit_code == 0
    assert "sequences:" in result.output
    assert "sounds:" in result.output
    assert "Alexa.Weather.Play" in result.output


def test_run_catalog_rejects_an_unknown_kind():
    result = CliRunner().invoke(cli, ["run", "catalog", "--kind", "skills"], obj={})
    assert result.exit_code != 0


# ── activity reads ──────────────────────────────────────────────────────


def test_activity_history_is_read_only_and_needs_no_yes():
    rows = [{"device": "Kitchen Echo", "utterance": "lights off", "response": "OK"}]
    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()), _stub_run(rows):
        result = _invoke(["--json", "activity", "history"])
    assert result.exit_code == 0
    assert json.loads(result.output)[0]["utterance"] == "lights off"


def test_activity_history_passes_its_filters_to_the_core_call():
    with (
        patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()),
        _stub_run([]),
        _stub_core(activity_core, "voice_history") as stub,
    ):
        result = _invoke(
            [
                "--json",
                "activity",
                "history",
                "--limit",
                "5",
                "--hours",
                "6",
                "--device",
                "Kitchen",
                "--contains",
                "lights",
                "--include-noise",
            ]
        )
    assert result.exit_code == 0
    assert stub.call_args.kwargs == {
        "limit": 5,
        "hours": "6",
        "device": "Kitchen",
        "contains": "lights",
        "include_noise": True,
    }


def test_activity_history_defaults_the_window_and_limit():
    with (
        patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()),
        _stub_run([]),
        _stub_core(activity_core, "voice_history") as stub,
    ):
        result = _invoke(["--json", "activity", "history"])
    assert result.exit_code == 0
    assert stub.call_args.kwargs["limit"] == activity_core.DEFAULT_HISTORY_LIMIT
    assert stub.call_args.kwargs["hours"] == activity_core.DEFAULT_HISTORY_HOURS
    assert stub.call_args.kwargs["include_noise"] is False


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["activity", "history", "--limit", "0"], "at least 1"),
        (["activity", "history", "--limit", "lots"], "whole number"),
        (["activity", "history", "--hours", "0"], "greater than 0"),
        (["activity", "history", "--hours", "soon"], "hours must be a number"),
        (["activity", "records", "--limit", "-2"], "at least 1"),
        (["activity", "last", "--limit", "nope"], "whole number"),
        (["activity", "clear", "--items", "0"], "at least 1"),
    ],
    ids=lambda v: str(v)[:40],
)
def test_activity_validates_its_arguments_before_logging_in(argv, expected):
    with patch("cli_anything.alexa.alexa_cli._login") as login:
        result = _invoke(["--json", *argv])
    assert result.exit_code == 1
    assert expected in result.output
    login.assert_not_called()


def test_activity_records_passes_the_limit_through():
    with (
        patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()),
        _stub_run([]),
        _stub_core(activity_core, "activity_records") as stub,
    ):
        result = _invoke(["--json", "activity", "records", "--limit", "3"])
    assert result.exit_code == 0
    assert stub.call_args.kwargs == {"limit": 3}


def test_activity_last_reports_the_answering_echo():
    row = {"device": "Study Dot", "utterance": "set a timer"}
    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()), _stub_run(row):
        result = _invoke(["--json", "activity", "last"])
    assert result.exit_code == 0
    assert json.loads(result.output)["device"] == "Study Dot"


def test_activity_last_passes_the_limit_through():
    with (
        patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()),
        _stub_run({}),
        _stub_core(activity_core, "last_command") as stub,
    ):
        result = _invoke(["--json", "activity", "last", "--limit", "7"])
    assert result.exit_code == 0
    assert stub.call_args.kwargs == {"limit": 7}


# ── activity clear (destructive) ─────────────────────────────────────────


def test_activity_clear_previews_and_flags_itself_irreversible():
    parsed = json.loads(_json_invoke(["activity", "clear"]).output)
    assert parsed["dry_run"] is True
    assert parsed["would_delete"] == 50  # the documented default
    assert parsed["irreversible"] is True
    assert "--yes" in parsed["hint"]


def test_activity_clear_does_not_delete_without_yes():
    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()), _stub_run() as ran:
        result = _invoke(["--json", "activity", "clear", "--items", "5"])
    assert result.exit_code == 0
    ran.assert_not_called()


def test_activity_clear_with_yes_deletes_the_requested_count():
    with (
        patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()),
        _stub_run({"requested": 5, "cleared": True}) as ran,
        _stub_core(activity_core, "clear_history") as stub,
    ):
        result = _invoke(["--json", "activity", "clear", "--items", "5", "--yes"])
    assert result.exit_code == 0
    ran.assert_called_once()
    assert stub.call_args.kwargs == {"items": 5}
    assert json.loads(result.output)["cleared"] is True
