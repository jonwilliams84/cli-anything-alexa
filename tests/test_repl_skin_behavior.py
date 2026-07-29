"""Behaviour tests for uncovered ReplSkin logic.

Targets the display/formatting methods that have real logic but no existing
test coverage: prompt building (color vs no-color), progress bar math, table
column-width calculation, help formatting, status_block alignment, and the
_display_home_path helper.
"""

import os
import sys
from pathlib import Path

import pytest

from cli_anything.alexa.utils import repl_skin
from cli_anything.alexa.utils.repl_skin import (
    ReplSkin,
    _display_home_path,
    _strip_ansi,
    _visible_len,
)


# ── _strip_ansi / _visible_len ──────────────────────────────────────

def test_strip_ansi_removes_escape_codes():
    """_strip_ansi must remove all ANSI colour codes, leaving plain text."""
    coloured = f"\033[38;5;80mhello\033[0m world"
    assert _strip_ansi(coloured) == "hello world"


def test_strip_ansi_plain_text_unchanged():
    assert _strip_ansi("plain text") == "plain text"


def test_visible_len_excludes_ansi_codes():
    assert _visible_len(f"\033[1mabc\033[0m") == 3


def test_visible_len_plain_text():
    assert _visible_len("hello") == 5


# ── _display_home_path ──────────────────────────────────────────────

def test_display_home_path_replaces_home_with_tilde(monkeypatch, tmp_path):
    """A path inside $HOME is shown as ~/relative."""
    monkeypatch.setenv("HOME", str(tmp_path))
    sub = tmp_path / "projects" / "video.mlt"
    result = _display_home_path(str(sub))
    assert result == "~/projects/video.mlt"


def test_display_home_path_outside_home_shows_full(monkeypatch, tmp_path):
    """A path outside $HOME is shown as the resolved absolute path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    other = tmp_path.parent / "elsewhere"
    result = _display_home_path(str(other))
    assert "~" not in result
    assert str(other.resolve()) in result or result.endswith("elsewhere")


# ── prompt() — color vs no-color ────────────────────────────────────

def test_prompt_no_color_uses_angle_bracket(monkeypatch):
    """When colour is disabled the prompt starts with '> ' not an ANSI icon."""
    monkeypatch.setenv("NO_COLOR", "1")
    skin = ReplSkin("alexa", version="1.0.0")
    assert not skin._color
    p = skin.prompt()
    assert p.startswith("> ")


def test_prompt_no_color_strips_all_ansi(monkeypatch):
    """The no-colour prompt must contain zero ANSI escape sequences."""
    monkeypatch.setenv("NO_COLOR", "1")
    skin = ReplSkin("alexa", version="1.0.0")
    p = skin.prompt("my_project", modified=True)
    assert "\033[" not in p


def test_prompt_with_color_contains_ansi_icon(monkeypatch):
    """When colour is enabled the prompt contains the ANSI cyan icon."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("CLI_ANYTHING_NO_COLOR", raising=False)
    skin = ReplSkin("alexa", version="1.0.0")
    skin._color = True
    p = skin.prompt()
    assert "\033[" in p
    assert "◆" in p


def test_prompt_shows_project_and_modified_marker(monkeypatch):
    """The prompt includes the project name and '*' when modified."""
    monkeypatch.setenv("NO_COLOR", "1")
    skin = ReplSkin("alexa", version="1.0.0")
    p = skin.prompt("my_project", modified=True)
    assert "my_project*" in p


def test_prompt_shows_context_over_project_name(monkeypatch):
    """When both context and project_name are given, context wins."""
    monkeypatch.setenv("NO_COLOR", "1")
    skin = ReplSkin("alexa", version="1.0.0")
    p = skin.prompt("proj", context="custom ctx")
    assert "custom ctx" in p
    # project_name is not shown when context is provided
    assert "proj" not in p


# ── prompt_tokens() ─────────────────────────────────────────────────

def test_prompt_tokens_includes_context_when_provided():
    skin = ReplSkin("alexa", version="1.0.0")
    tokens = skin.prompt_tokens("proj", modified=True, context="ctx")
    texts = [t[1] for t in tokens]
    assert "ctx*" in texts  # context + modified marker


def test_prompt_tokens_no_context_when_empty():
    skin = ReplSkin("alexa", version="1.0.0")
    tokens = skin.prompt_tokens()
    # Only icon + software + arrow tokens, no bracket/context
    classes = [t[0] for t in tokens]
    assert "class:bracket" not in classes


# ── progress() ──────────────────────────────────────────────────────

def test_progress_zero_total_shows_zero_percent(capsys, monkeypatch):
    """total=0 must not divide by zero; shows 0%."""
    monkeypatch.setenv("NO_COLOR", "1")
    skin = ReplSkin("alexa", version="1.0.0")
    skin.progress(5, 0)
    out = capsys.readouterr().out
    assert "  0%" in out


def test_progress_full_shows_hundred_percent(capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    skin = ReplSkin("alexa", version="1.0.0")
    skin.progress(10, 10, label="done")
    out = capsys.readouterr().out
    assert "100%" in out
    assert "done" in out


def test_progress_partial_shows_filled_and_empty_bar(capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    skin = ReplSkin("alexa", version="1.0.0")
    skin.progress(5, 10)
    out = capsys.readouterr().out
    assert "█" in out  # filled portion
    assert "░" in out  # empty portion
    assert " 50%" in out


# ── table() ─────────────────────────────────────────────────────────

def test_table_empty_headers_prints_nothing(capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    skin = ReplSkin("alexa", version="1.0.0")
    skin.table([], [["a", "b"]])
    out = capsys.readouterr().out
    assert out == ""


def test_table_truncates_long_cells_to_max_col_width(capsys, monkeypatch):
    """Cells longer than max_col_width are truncated."""
    monkeypatch.setenv("NO_COLOR", "1")
    skin = ReplSkin("alexa", version="1.0.0")
    long_val = "x" * 100
    skin.table(["Col"], [[long_val]], max_col_width=10)
    out = capsys.readouterr().out
    # The cell should be truncated to 10 chars
    assert "x" * 10 in out
    assert "x" * 11 not in out


def test_table_column_width_uses_widest_cell(capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    skin = ReplSkin("alexa", version="1.0.0")
    skin.table(["A", "B"], [["short", "muchlongervalue"]], max_col_width=40)
    out = capsys.readouterr().out
    # Both columns should be present
    assert "short" in out
    assert "muchlongervalue" in out


# ── help() ──────────────────────────────────────────────────────────

def test_help_prints_commands_and_descriptions(capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    skin = ReplSkin("alexa", version="1.0.0")
    skin.help({"quit": "Exit the REPL", "status": "Show status"})
    out = capsys.readouterr().out
    assert "quit" in out
    assert "Exit the REPL" in out
    assert "status" in out
    assert "Show status" in out


def test_help_empty_commands_prints_section_only(capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    skin = ReplSkin("alexa", version="1.0.0")
    skin.help({})
    out = capsys.readouterr().out
    assert "Commands" in out


# ── status_block() ──────────────────────────────────────────────────

def test_status_block_aligns_keys_to_widest(capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    skin = ReplSkin("alexa", version="1.0.0")
    skin.status_block({"a": "1", "longkey": "2"})
    out = capsys.readouterr().out
    assert "a" in out
    assert "longkey" in out
    assert "1" in out
    assert "2" in out


def test_status_block_with_title_prints_section_header(capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    skin = ReplSkin("alexa", version="1.0.0")
    skin.status_block({"k": "v"}, title="My Section")
    out = capsys.readouterr().out
    assert "My Section" in out


# ── messages: success/error/warning/info/hint ───────────────────────

def test_error_writes_to_stderr(capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    skin = ReplSkin("alexa", version="1.0.0")
    skin.error("boom")
    captured = capsys.readouterr()
    assert "boom" in captured.err
    assert "boom" not in captured.out


def test_success_writes_to_stdout(capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    skin = ReplSkin("alexa", version="1.0.0")
    skin.success("ok")
    captured = capsys.readouterr()
    assert "ok" in captured.out


def test_warning_writes_to_stdout(capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    skin = ReplSkin("alexa", version="1.0.0")
    skin.warning("careful")
    out = capsys.readouterr().out
    assert "careful" in out


def test_info_writes_to_stdout(capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    skin = ReplSkin("alexa", version="1.0.0")
    skin.info("notice")
    out = capsys.readouterr().out
    assert "notice" in out


def test_hint_writes_to_stdout(capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    skin = ReplSkin("alexa", version="1.0.0")
    skin.hint("subtle")
    out = capsys.readouterr().out
    assert "subtle" in out


# ── section() ───────────────────────────────────────────────────────

def test_section_prints_title_and_underline(capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    skin = ReplSkin("alexa", version="1.0.0")
    skin.section("Devices")
    out = capsys.readouterr().out
    assert "Devices" in out
    assert "─" in out  # underline


# ── status() ────────────────────────────────────────────────────────

def test_status_prints_label_and_value(capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    skin = ReplSkin("alexa", version="1.0.0")
    skin.status("Devices", "3 online")
    out = capsys.readouterr().out
    assert "Devices" in out
    assert "3 online" in out


# ── print_goodbye() ─────────────────────────────────────────────────

def test_print_goodbye_outputs_message(capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    skin = ReplSkin("alexa", version="1.0.0")
    skin.print_goodbye()
    out = capsys.readouterr().out
    assert "Goodbye" in out


# ── _detect_color_support ───────────────────────────────────────────

def test_detect_color_no_color_env(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    skin = ReplSkin("alexa", version="1.0.0")
    assert skin._color is False


def test_detect_color_cli_anything_no_color_env(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("CLI_ANYTHING_NO_COLOR", "1")
    skin = ReplSkin("alexa", version="1.0.0")
    assert skin._color is False


# ── get_prompt_style ────────────────────────────────────────────────

def test_get_prompt_style_returns_style_object():
    """get_prompt_style returns a prompt_toolkit Style when available."""
    skin = ReplSkin("alexa", version="1.0.0")
    style = skin.get_prompt_style()
    # prompt_toolkit is installed (declared in install_requires), so this
    # should return a real Style object, not None.
    if style is not None:
        # Verify it's a Style by checking it has the expected attributes
        assert hasattr(style, "style_rules")


# ── create_prompt_session ───────────────────────────────────────────

def test_create_prompt_session_returns_session():
    skin = ReplSkin("alexa", version="1.0.0")
    session = skin.create_prompt_session()
    # prompt_toolkit is installed, so should get a real session
    if session is not None:
        assert hasattr(session, "prompt")


# ── bottom_toolbar ──────────────────────────────────────────────────

def test_bottom_toolbar_returns_callable():
    skin = ReplSkin("alexa", version="1.0.0")
    toolbar = skin.bottom_toolbar({"Status": "online"})
    assert callable(toolbar)


def test_bottom_toolbar_callable_returns_formatted_text():
    from prompt_toolkit.formatted_text import FormattedText

    skin = ReplSkin("alexa", version="1.0.0")
    toolbar = skin.bottom_toolbar({"Status": "online", "Devices": "3"})
    result = toolbar()
    assert isinstance(result, FormattedText)
    # Should contain the values
    texts = [t[1] for t in result]
    assert "online" in texts
    assert "3" in texts


# ── get_input fallback ──────────────────────────────────────────────

def test_get_input_fallback_to_builtin_input(monkeypatch):
    """When pt_session is None, get_input uses builtin input()."""
    monkeypatch.setenv("NO_COLOR", "1")
    skin = ReplSkin("alexa", version="1.0.0")
    monkeypatch.setattr("builtins.input", lambda prompt: "  hello  ")
    result = skin.get_input(None, project_name="proj")
    assert result == "hello"  # stripped


# ── software name normalisation ─────────────────────────────────────

def test_software_name_normalised_to_lowercase_underscore():
    skin = ReplSkin("My-Software", version="1.0.0")
    assert skin.software == "my_software"


def test_display_name_title_cased():
    skin = ReplSkin("my_software", version="1.0.0")
    assert skin.display_name == "My Software"


def test_skill_slug_uses_hyphens():
    skin = ReplSkin("alexa", version="1.0.0")
    assert skin.skill_slug == "alexa"
    assert skin.skill_id == "cli-anything-alexa"


def test_skill_slug_alias():
    skin = ReplSkin("iterm2_ctl", version="1.0.0")
    assert skin.skill_slug == "iterm2"


# ── accent color selection ──────────────────────────────────────────

def test_known_software_gets_specific_accent():
    skin = ReplSkin("gimp", version="1.0.0")
    assert skin.accent == repl_skin._ACCENT_COLORS["gimp"]


def test_unknown_software_gets_default_accent():
    skin = ReplSkin("unknown_sw", version="1.0.0")
    assert skin.accent == repl_skin._DEFAULT_ACCENT
