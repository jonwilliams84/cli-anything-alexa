"""Behavioural tests for the new CLI command paths: `media`, `speak`, `echos` reads.

Asserts the harness-wide contract on every newly added mutating command —
**preview by default, act only on --yes** — plus argument validation and the
read-only paths.  All assertions are on observable behaviour (exit code, JSON
on stdout, which core coroutine was invoked), never on source text.
"""

from __future__ import annotations

import contextlib
import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli_anything.alexa.alexa_cli import cli
from cli_anything.alexa.core import media as media_core


def _invoke(args, obj=None):
    return CliRunner().invoke(cli, args, obj=obj or {}, catch_exceptions=False)


def _json_invoke(args):
    """Invoke with a stubbed login and parse the JSON on stdout."""
    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()):
        result = _invoke(["--json", *args])
    assert result.exit_code == 0, result.output
    return result


@contextlib.contextmanager
def _stub_run(return_value=None, side_effect=None):
    """Patch the CLI's ``_run`` with a stub that *closes* the coroutine it gets.

    The command builds its core coroutine eagerly and hands it to ``_run``. A
    plain ``MagicMock`` would leave that coroutine un-awaited, which leaks a
    frame and raises ``RuntimeWarning`` from whichever unrelated test happens to
    trigger the GC. Closing it keeps the suite's output clean and the failure
    attribution honest.
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
def _stub_core(name):
    """Replace an async core function with a plain MagicMock.

    ``patch.object`` auto-detects coroutine functions and substitutes an
    ``AsyncMock``, whose un-awaited return value warns exactly like the real
    thing. A plain MagicMock records the call arguments without creating one.
    """
    with patch.object(media_core, name, MagicMock()) as stub:
        yield stub


#: (argv, the dry-run field that must describe the pending action)
MUTATING_MEDIA_COMMANDS = [
    (["media", "play", "Kitchen"], "action"),
    (["media", "pause", "Kitchen"], "action"),
    (["media", "next", "Kitchen"], "action"),
    (["media", "previous", "Kitchen"], "action"),
    (["media", "forward", "Kitchen"], "action"),
    (["media", "rewind", "Kitchen"], "action"),
    (["media", "stop", "Kitchen"], "action"),
    (["media", "volume", "Kitchen", "--level", "40"], "volume"),
    (["media", "shuffle", "Kitchen", "--state", "on"], "shuffle"),
    (["media", "repeat", "Kitchen", "--state", "off"], "repeat"),
    (["media", "play-music", "jazz", "--device", "Kitchen"], "search"),
    (["speak", "hello there", "--device", "Kitchen"], "would_speak"),
]


# ── the dry-run contract ────────────────────────────────────────────────


@pytest.mark.parametrize(("argv", "field"), MUTATING_MEDIA_COMMANDS, ids=lambda v: str(v))
def test_every_new_mutating_command_previews_without_yes(argv, field):
    parsed = json.loads(_json_invoke(argv).output)
    assert parsed["dry_run"] is True
    assert field in parsed
    assert "--yes" in parsed["hint"]


@pytest.mark.parametrize("argv", [a for a, _ in MUTATING_MEDIA_COMMANDS], ids=lambda v: str(v))
def test_no_new_mutating_command_reaches_the_network_without_yes(argv):
    """A dry run must never call a core coroutine."""
    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()):
        with _stub_run() as ran:
            result = _invoke(["--json", *argv])
    assert result.exit_code == 0
    ran.assert_not_called()


# ── media transport execution ───────────────────────────────────────────


@pytest.mark.parametrize("verb", sorted(media_core.TRANSPORT_COMMANDS))
def test_transport_command_with_yes_dispatches_the_right_verb(verb):
    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()):
        with _stub_run({"ok": True}) as ran:
            with _stub_core("transport") as transport:
                result = _invoke(["--json", "media", verb, "Kitchen", "--yes"])
    assert result.exit_code == 0
    ran.assert_called_once()
    assert transport.call_args[0][2] == verb


def test_media_status_is_read_only_and_needs_no_yes():
    row = {"device": "Kitchen", "state": "PLAYING", "title": "Take Five"}
    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()):
        with _stub_run(row):
            result = _invoke(["--json", "media", "status", "Kitchen"])
    assert result.exit_code == 0
    assert json.loads(result.output)["title"] == "Take Five"


def test_media_status_without_a_device_is_allowed():
    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()):
        with _stub_run({"device": "Kitchen"}):
            result = _invoke(["--json", "media", "status"])
    assert result.exit_code == 0


def test_media_command_without_a_device_reports_the_implicit_target():
    parsed = json.loads(_json_invoke(["media", "pause"]).output)
    assert parsed["device"] == "first online"


# ── stop --all ──────────────────────────────────────────────────────────


def test_media_stop_all_previews_all_devices():
    parsed = json.loads(_json_invoke(["media", "stop", "--all"]).output)
    assert parsed["device"] == "all"
    assert parsed["action"] == "stop"


def test_media_stop_all_with_yes_passes_the_flag_through():
    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()):
        with _stub_run({"ok": True}):
            with _stub_core("stop") as stop:
                result = _invoke(["--json", "media", "stop", "--all", "--yes"])
    assert result.exit_code == 0
    assert stop.call_args.kwargs["all_devices"] is True


# ── volume validation ───────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["150", "-5", "1000"])
def test_media_volume_rejects_out_of_range_before_logging_in(bad):
    """Validation must fail identically whether or not --yes was given."""
    with patch("cli_anything.alexa.alexa_cli._login") as login:
        result = _invoke(["--json", "media", "volume", "Kitchen", "--level", bad])
    assert result.exit_code == 1
    assert "between 0 and 100" in result.output
    login.assert_not_called()


def test_media_volume_rejects_out_of_range_even_with_yes():
    with patch("cli_anything.alexa.alexa_cli._login"):
        result = _invoke(["--json", "media", "volume", "Kitchen", "--level", "150", "--yes"])
    assert result.exit_code == 1


def test_media_volume_rejects_a_non_numeric_level():
    result = CliRunner().invoke(
        cli, ["--json", "media", "volume", "Kitchen", "--level", "loud"], obj={}
    )
    assert result.exit_code != 0


def test_media_volume_accepts_the_boundaries():
    for level in ("0", "100"):
        parsed = json.loads(_json_invoke(["media", "volume", "Kitchen", "--level", level]).output)
        assert parsed["dry_run"] is True


def test_media_volume_with_yes_converts_and_executes():
    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()):
        with _stub_run({"volume": 40}):
            with _stub_core("set_volume") as set_volume:
                result = _invoke(["--json", "media", "volume", "Kitchen", "--level", "40", "--yes"])
    assert result.exit_code == 0
    assert set_volume.call_args[0][2] == 40.0


# ── shuffle / repeat choices ────────────────────────────────────────────


@pytest.mark.parametrize("sub", ["shuffle", "repeat"])
def test_shuffle_and_repeat_reject_a_bad_state(sub):
    result = CliRunner().invoke(cli, ["media", sub, "Kitchen", "--state", "maybe"], obj={})
    assert result.exit_code != 0


@pytest.mark.parametrize("sub", ["shuffle", "repeat"])
def test_shuffle_and_repeat_require_a_state(sub):
    result = CliRunner().invoke(cli, ["media", sub, "Kitchen"], obj={})
    assert result.exit_code != 0


@pytest.mark.parametrize(("sub", "core_fn"), [("shuffle", "set_shuffle"), ("repeat", "set_repeat")])
def test_shuffle_and_repeat_translate_on_to_true(sub, core_fn):
    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()):
        with _stub_run({"ok": True}):
            with _stub_core(core_fn) as fn:
                _invoke(["--json", "media", sub, "Kitchen", "--state", "on", "--yes"])
    assert fn.call_args[0][2] is True


@pytest.mark.parametrize(("sub", "core_fn"), [("shuffle", "set_shuffle"), ("repeat", "set_repeat")])
def test_shuffle_and_repeat_translate_off_to_false(sub, core_fn):
    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()):
        with _stub_run({"ok": True}):
            with _stub_core(core_fn) as fn:
                _invoke(["--json", "media", sub, "Kitchen", "--state", "off", "--yes"])
    assert fn.call_args[0][2] is False


# ── play-music ──────────────────────────────────────────────────────────


def test_play_music_normalises_the_provider_in_the_preview():
    parsed = json.loads(
        _json_invoke(["media", "play-music", "jazz", "--provider", "spotify"]).output
    )
    assert parsed["provider"] == "SPOTIFY"
    assert parsed["search"] == "jazz"


def test_play_music_defaults_to_amazon_music():
    parsed = json.loads(_json_invoke(["media", "play-music", "jazz"]).output)
    assert parsed["provider"] == media_core.DEFAULT_MUSIC_PROVIDER


def test_play_music_requires_a_search_phrase():
    result = CliRunner().invoke(cli, ["media", "play-music"], obj={})
    assert result.exit_code != 0


def test_play_music_with_yes_passes_the_normalised_provider():
    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()):
        with _stub_run({"ok": True}):
            with _stub_core("play_music") as play:
                _invoke(["--json", "media", "play-music", "jazz", "--provider", "tunein", "--yes"])
    assert play.call_args[0][3] == "TUNEIN"


def test_play_music_help_lists_the_known_providers():
    result = CliRunner().invoke(cli, ["media", "play-music", "-h"], obj={})
    assert media_core.DEFAULT_MUSIC_PROVIDER in result.output


# ── speak ───────────────────────────────────────────────────────────────


def test_speak_preview_reports_the_text_and_device():
    parsed = json.loads(_json_invoke(["speak", "dinner is ready", "--device", "Kitchen"]).output)
    assert parsed["would_speak"] == "dinner is ready"
    assert parsed["device"] == "Kitchen"


def test_speak_without_a_device_targets_the_first_online_echo():
    parsed = json.loads(_json_invoke(["speak", "hi"]).output)
    assert parsed["device"] == "first online"


def test_speak_with_yes_calls_the_tts_core():
    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()):
        with _stub_run({"spoke": "hi"}) as ran:
            result = _invoke(["--json", "speak", "hi", "--yes"])
    assert result.exit_code == 0
    ran.assert_called_once()
    assert json.loads(result.output)["spoke"] == "hi"


def test_speak_requires_text():
    result = CliRunner().invoke(cli, ["speak"], obj={})
    assert result.exit_code != 0


def test_speak_surfaces_a_core_value_error_as_a_clean_abort():
    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()):
        with _stub_run(side_effect=ValueError("no device matching 'Garage'")):
            result = _invoke(["--json", "speak", "hi", "--device", "Garage", "--yes"])
    assert result.exit_code == 1
    assert "no device matching" in result.output


# ── echos read-only subcommands ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("sub", "core_fn"),
    [
        ("bluetooth", "fetch_bluetooth"),
        ("wake-words", "fetch_wake_words"),
        ("dnd", "fetch_dnd_states"),
    ],
)
def test_echos_reads_emit_rows_without_needing_yes(sub, core_fn):
    rows = [{"device": "Kitchen", "serial": "SN1"}]
    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()):
        with _stub_run(rows):
            result = _invoke(["--json", "echos", sub])
    assert result.exit_code == 0
    assert json.loads(result.output) == rows


@pytest.mark.parametrize("sub", ["bluetooth", "wake-words", "dnd"])
def test_echos_reads_render_a_table_in_text_mode(sub):
    rows = [{"device": "Kitchen", "serial": "SN1"}]
    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()):
        with _stub_run(rows):
            result = _invoke(["echos", sub])
    assert result.exit_code == 0
    assert "Kitchen" in result.output


def test_echos_dnd_read_does_not_collide_with_the_dnd_write_command():
    """`echos dnd` reads; top-level `dnd` writes and still demands --yes."""
    parsed = json.loads(_json_invoke(["dnd", "Kitchen", "on"]).output)
    assert parsed["dry_run"] is True


# ── discoverability ─────────────────────────────────────────────────────


def test_media_group_is_registered_on_the_root_cli():
    result = CliRunner().invoke(cli, ["-h"], obj={})
    assert "media" in result.output
    assert "speak" in result.output


@pytest.mark.parametrize(
    "sub",
    [
        "status",
        "play",
        "pause",
        "next",
        "previous",
        "forward",
        "rewind",
        "stop",
        "volume",
        "shuffle",
        "repeat",
        "play-music",
    ],
)
def test_every_media_subcommand_has_help(sub):
    result = CliRunner().invoke(cli, ["media", sub, "-h"], obj={})
    assert result.exit_code == 0
    assert "Usage:" in result.output
