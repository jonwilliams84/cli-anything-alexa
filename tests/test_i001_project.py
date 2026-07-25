"""Regression tests for I001: sorted/formatted import block in project.py.

The automated scanner flagged one I001 finding in
cli_anything/alexa/core/project.py.  This module pins the import layout so it
cannot regress.
"""

import ast
import subprocess
import sys
from pathlib import Path

from cli_anything.alexa.core import project


SRC = Path(project.__file__).read_text()
TREE = ast.parse(SRC)


def test_project_import_block_is_sorted():
    """project.py passes ruff's isort check (I001)."""
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", project.__file__],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_no_optional_import_in_project_module():
    """The ``Optional`` name must not be imported from typing in project.py."""
    assert "from typing import Any, Optional" not in SRC
    assert "Optional" not in SRC


def test_load_config_path_uses_pipe_none():
    """load_config path is ``Path | None`` (UP045 line 44)."""
    fn = next(
        node
        for node in ast.walk(TREE)
        if isinstance(node, ast.FunctionDef) and node.name == "load_config"
    )
    ann = fn.args.args[0].annotation
    assert ast.unparse(ann) == "Path | None"


def test_save_config_path_uses_pipe_none():
    """save_config path is ``Path | None`` (UP045 line 60)."""
    fn = next(
        node
        for node in ast.walk(TREE)
        if isinstance(node, ast.FunctionDef) and node.name == "save_config"
    )
    ann = fn.args.args[1].annotation
    assert ast.unparse(ann) == "Path | None"


def test_load_save_config_behaviour_preserved(tmp_path, monkeypatch):
    """load_config/save_config still read/write the same config shape."""
    monkeypatch.delenv("CLI_ALEXA_EMAIL", raising=False)
    monkeypatch.delenv("CLI_ALEXA_URL", raising=False)
    cfg_path = tmp_path / "config.json"
    project.save_config({"email": "a@b.com", "url": "amazon.com", "extra": "ignored"}, cfg_path)
    loaded = project.load_config(cfg_path)
    assert loaded["email"] == "a@b.com"
    assert loaded["url"] == "amazon.com"
    assert "extra" not in loaded
