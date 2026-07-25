"""Regression tests for the three scanner findings in repl_skin.py.

The automated scanner reported three findings against
``cli_anything/alexa/utils/repl_skin.py``:

* F841 — local variable ``accent_hex`` assigned but never used (prompt_tokens)
* F841 — local variable ``sep_parts`` assigned but never used (table separator)
* I001  — import block un-sorted / un-formatted (prompt_toolkit imports)

These tests pin the fixes so they cannot regress.
"""

import subprocess
import sys
from pathlib import Path

from cli_anything.alexa.utils import repl_skin


SKIN_FILE = Path(repl_skin.__file__)


def _ruff(*select: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "ruff", "check", str(SKIN_FILE), "--select", *select],
        capture_output=True,
        text=True,
    )


# ── F841: accent_hex in prompt_tokens ───────────────────────────────

def test_prompt_tokens_has_no_unused_accent_hex():
    """The ``accent_hex`` local removed from ``prompt_tokens`` stays gone.

    ``prompt_tokens`` builds ``class:``-style tokens; the hex value was dead.
    A second ``accent_hex`` legitimately lives in ``get_prompt_style`` (it feeds
    ``Style.from_dict``), so we scope the assertion to the ``prompt_tokens``
    body only.
    """
    src = SKIN_FILE.read_text()
    start = src.index("def prompt_tokens")
    end = src.index("def get_prompt_style")
    body = src[start:end]
    assert "accent_hex" not in body, (
        "prompt_tokens must not reintroduce the unused `accent_hex` local"
    )


def test_prompt_tokens_returns_tokens():
    """Behaviour preserved: prompt_tokens still returns a non-empty token list."""
    skin = repl_skin.ReplSkin("gimp", version="1.0.0")
    tokens = skin.prompt_tokens()
    assert isinstance(tokens, list)
    assert tokens, "prompt_tokens must return at least one token"
    # First token is the icon marker.
    assert tokens[0][0] == "class:icon"


def test_ruff_no_f841_accent_hex():
    """ruff reports no F841 for the (now-removed) accent_hex assignment."""
    result = _ruff("F841")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "accent_hex" not in result.stdout


# ── F841: sep_parts in table separator ──────────────────────────────

def test_table_separator_has_no_unused_sep_parts():
    """The ``sep_parts`` local removed from ``table`` stays gone."""
    src = SKIN_FILE.read_text()
    start = src.index("def table(")
    end = src.index("\n    def ", start + 1)
    body = src[start:end]
    assert "sep_parts" not in body, (
        "table must not reintroduce the unused `sep_parts` local"
    )


def test_table_prints_separator(capsys):
    """Behaviour preserved: table still prints a separator line."""
    skin = repl_skin.ReplSkin("gimp", version="1.0.0")
    skin.table(["Name", "Value"], [["a", "b"]])
    out = capsys.readouterr().out
    assert "───" in out, "table must still render the box-drawing separator"


def test_ruff_no_f841_sep_parts():
    """ruff reports no F841 for the (now-removed) sep_parts assignment."""
    result = _ruff("F841")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "sep_parts" not in result.stdout


# ── I001: prompt_toolkit import block sorted ────────────────────────

def test_repl_skin_import_block_is_sorted():
    """repl_skin.py passes ruff's isort check (I001)."""
    result = _ruff("I001")
    assert result.returncode == 0, result.stdout + result.stderr


def test_repl_skin_prompt_toolkit_imports_order():
    """The prompt_toolkit import block is ordered by module path."""
    src = SKIN_FILE.read_text()
    block_start = src.index("from prompt_toolkit import PromptSession")
    block_end = src.index("\n\n", block_start)
    block = src[block_start:block_end]
    lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
    # Extract module paths for ordering comparison.
    mods = []
    for ln in lines:
        # "from prompt_toolkit.X import Y" or "from prompt_toolkit import Y"
        mods.append(ln.split(" import ")[0].replace("from ", ""))
    assert mods == sorted(mods), (
        f"prompt_toolkit imports must be sorted by module path: {mods}"
    )
