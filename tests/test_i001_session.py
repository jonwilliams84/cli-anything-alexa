"""Regression tests for I001: sorted/formatted import block in session.py.

The automated scanner flagged one I001 finding in
cli_anything/alexa/core/session.py.  This module pins the import layout so it
cannot regress.
"""

import subprocess
import sys
from pathlib import Path

from cli_anything.alexa.core import session


def test_session_import_block_is_sorted():
    """session.py passes ruff's isort check (I001)."""
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", session.__file__, "--select", "I001"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_session_no_optional_import():
    """The ``Optional`` name must not be imported from typing in session.py."""
    src = Path(session.__file__).read_text()
    assert "from typing import Any, Optional" not in src
    assert "Optional" not in src
