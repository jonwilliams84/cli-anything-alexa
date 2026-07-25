"""Regression tests for UP045: modern X | None type annotations in notifications.py.

The automated scanner flagged two UP045 findings in
cli_anything/alexa/core/notifications.py.  This module pins the annotations so
the old ``Optional[X]`` style cannot regress.
"""

import ast
from pathlib import Path

from cli_anything.alexa.core import notifications


SRC = Path(notifications.__file__).read_text()
TREE = ast.parse(SRC)


def _function_node(name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(TREE):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found in notifications.py")


def _annotation_source(node: ast.AST) -> str:
    return ast.unparse(node)


def test_epoch_ms_seconds_from_now_uses_pipe_none():
    """_epoch_ms seconds_from_now is ``float | None`` (UP045 line 39)."""
    fn = _function_node("_epoch_ms")
    ann = fn.args.args[0].annotation
    assert _annotation_source(ann) == "float | None"


def test_epoch_ms_at_epoch_ms_uses_pipe_none():
    """_epoch_ms at_epoch_ms is ``int | None`` (UP045 line 40)."""
    fn = _function_node("_epoch_ms")
    ann = fn.args.args[1].annotation
    assert _annotation_source(ann) == "int | None"


def test_no_optional_import_in_notifications_module():
    """The ``Optional`` name must not be imported from typing in notifications.py."""
    assert "from typing import Any, Optional" not in SRC
    assert "Optional" not in SRC


def test_epoch_ms_behaviour_preserved():
    """Behaviour preservation: _epoch_ms still computes the same values."""
    import time

    now = int(time.time() * 1000)
    # at_epoch_ms takes precedence.
    assert notifications._epoch_ms(at_epoch_ms=1234) == 1234
    # seconds_from_now adds to current time.
    result = notifications._epoch_ms(seconds_from_now=0.5)
    assert now + 400 <= result <= now + 700
    # None defaults to current time.
    result = notifications._epoch_ms()
    assert now - 100 <= result <= now + 100
