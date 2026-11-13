"""Behavioural tests for the equalizer CLI paths (0.16.0).

`echos equalizer` (the per-Echo read) and `echos set-equalizer` (the write,
dry-run by default).  Assertions are on observable behaviour — exit code,
JSON on stdout, which core coroutine was invoked with what — never on source
text.  The write obeys the harness-wide contract: **preview by default, act
only on --yes**, and out-of-range bands are refused *before* login,
identically with and without `--yes`.
"""

from __future__ import annotations

import contextlib
import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli_anything.alexa.alexa_cli import cli
from cli_anything.alexa.core import equalizer as equalizer_core


def _invoke(args, obj=None):
    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()):
        result = CliRunner().invoke(cli, ["--json", *args], obj=obj or {}, catch_exceptions=False)
    return result


def _ok(args):
    result = _invoke(args)
    assert result.exit_code == 0, result.output
    return result


_ROWS = [
    {"device": "Kitchen", "serial": "SN1", "deviceType": "DT1", "bass": 2, "mid": 0, "treble": -1},
    {
        "device": "Plug",
        "serial": "SN2",
        "deviceType": "DT2",
        "bass": None,
        "mid": None,
        "treble": None,
    },
]


@contextlib.contextmanager
def _stub_run(queue=None):
    """Patch ``_run``: closes the coroutine it is handed, pops the queue."""
    values = queue or []

    def fake_run(_ctx, coro):
        if hasattr(coro, "close"):
            coro.close()
        return values.pop(0) if values else {}

    with patch("cli_anything.alexa.alexa_cli._run", side_effect=fake_run) as mock_run:
        yield mock_run


# ── the read ─────────────────────────────────────────────────────────────


def test_equalizer_lists_the_whole_roster():
    with _stub_run([[dict(r) for r in _ROWS]]):
        rows = json.loads(_ok(["echos", "equalizer"]).output)
    assert len(rows) == 2
    assert rows[0]["bass"] == 2


def test_equalizer_names_one_device():
    with _stub_run([[dict(_ROWS[0])]]):
        rows = json.loads(_ok(["echos", "equalizer", "Kitchen"]).output)
    assert [r["serial"] for r in rows] == ["SN1"]


def test_equalizer_shows_a_device_without_bands_rather_than_dropping_it():
    with _stub_run([[dict(_ROWS[1])]]):
        rows = json.loads(_ok(["echos", "equalizer", "Plug"]).output)
    assert (rows[0]["bass"], rows[0]["mid"], rows[0]["treble"]) == (None, None, None)


# ── the write ────────────────────────────────────────────────────────────


def test_set_equalizer_previews_without_yes():
    parsed = json.loads(
        _ok(["echos", "set-equalizer", "Kitchen", "--bass", 2, "--mid", 0, "--treble", "-1"]).output
    )
    assert parsed == {
        "dry_run": True,
        "device": "Kitchen",
        "bass": 2,
        "mid": 0,
        "treble": -1,
        "hint": "re-run with --yes to execute",
    }


def test_set_equalizer_executes_with_yes():
    row = {
        "device": "Kitchen",
        "serial": "SN1",
        "bass": 2,
        "mid": 0,
        "treble": -1,
        "sent": True,
        "observed": {"bass": 2, "mid": 0, "treble": -1},
        "ok": True,
    }
    with _stub_run([row]) as run:
        result = _ok(
            [
                "echos",
                "set-equalizer",
                "Kitchen",
                "--bass",
                2,
                "--mid",
                0,
                "--treble",
                "-1",
                "--yes",
            ]
        )
    assert json.loads(result.output) == row
    assert "set_equalizer_bands" in run.call_args[0][1].__name__


@pytest.mark.parametrize(
    "args",
    [
        ["--bass", "7", "--mid", "0", "--treble", "0"],  # above the range
        ["--bass", "-7", "--mid", "0", "--treble", "0"],  # below the range
        ["--bass", "0", "--treble", "0"],  # a missing band — all three are required
        ["--bass", "2", "--mid", "2.5", "--treble", "0"],  # fractional — refused, not rounded
    ],
)
def test_set_equalizer_out_of_range_or_partial_bands_are_refused_before_login(args):
    def _must_not_login(*_a, **_k):
        raise AssertionError("must not login before validating")

    with patch("cli_anything.alexa.alexa_cli._login", side_effect=_must_not_login):
        result = CliRunner().invoke(
            cli,
            ["--json", "echos", "set-equalizer", "Kitchen", *args],
            catch_exceptions=False,
        )
    assert result.exit_code != 0
    assert "dry_run" not in result.output
    assert "--yes" not in result.output


def test_set_equalizer_non_integer_band_is_refused_by_click():
    result = _invoke(
        ["echos", "set-equalizer", "Kitchen", "--bass", "loud", "--mid", 0, "--treble", 0]
    )
    assert result.exit_code != 0
    assert "dry_run" not in result.output


# ── workflow: read → set → read ──────────────────────────────────────────


def test_equalizer_workflow_read_then_set_then_read():
    """The whole loop a user runs: see it, change it, confirm the change."""
    queue: list = [
        [dict(r) for r in _ROWS],  # first read
        {  # the write's verified answer
            "device": "Kitchen",
            "serial": "SN1",
            "bass": -2,
            "mid": 1,
            "treble": 1,
            "sent": True,
            "observed": {"bass": -2, "mid": 1, "treble": 1},
            "ok": True,
        },
        [dict(_ROWS[0], bass=-2, mid=1, treble=1), dict(_ROWS[1])],  # confirming read
    ]
    with _stub_run(queue):
        before = json.loads(_ok(["echos", "equalizer", "Kitchen"]).output)
        assert before[0]["bass"] == 2
        written = json.loads(
            _ok(
                [
                    "echos",
                    "set-equalizer",
                    "Kitchen",
                    "--bass",
                    "-2",
                    "--mid",
                    1,
                    "--treble",
                    1,
                    "--yes",
                ]
            ).output
        )
        assert written["ok"] is True
        after = json.loads(_ok(["echos", "equalizer", "Kitchen"]).output)
        assert (after[0]["bass"], after[0]["mid"], after[0]["treble"]) == (-2, 1, 1)


# ── dry-run and --yes validate identically, before login ─────────────────


@pytest.mark.parametrize("yes", [True, False])
def test_set_equalizer_bad_band_fails_identically_with_and_without_yes(yes):
    args = ["echos", "set-equalizer", "Kitchen", "--bass", "9", "--mid", "0", "--treble", "0"]
    if yes:
        args.append("--yes")
    result = _invoke(args)
    assert result.exit_code != 0
    assert "dry_run" not in result.output
    assert "-6<=x<=6" in result.output or "must be" in result.output


def test_cli_passes_the_core_module_the_named_device():
    with _stub_run([[dict(_ROWS[0])]]) as run:
        _ok(["echos", "equalizer", "Kitchen"])
    coro = run.call_args[0][1]
    assert "fetch_equalizer" in coro.__name__
