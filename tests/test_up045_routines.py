"""Regression tests for UP045: modern X | None type annotations in routines.py.

The automated scanner flagged two UP045 findings in
cli_anything/alexa/core/routines.py.  This module pins the annotations so the
old ``Optional[X]`` style cannot regress.
"""

import ast
from pathlib import Path

from cli_anything.alexa.core import routines


SRC = Path(routines.__file__).read_text()
TREE = ast.parse(SRC)


def _function_node(name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(TREE):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found in routines.py")


def _annotation_source(node: ast.AST) -> str:
    return ast.unparse(node)


def test_node_summary_return_uses_pipe_none():
    """_node_summary returns ``str | None`` (UP045 line 27)."""
    fn = _function_node("_node_summary")
    assert _annotation_source(fn.returns) == "str | None"


def test_find_routine_return_uses_pipe_none():
    """find_routine returns ``dict[str, Any] | None`` (UP045 line 93)."""
    fn = _function_node("find_routine")
    assert _annotation_source(fn.returns) == "dict[str, Any] | None"


def test_no_optional_import_in_routines_module():
    """The ``Optional`` name must not be imported from typing in routines.py."""
    assert "from typing import Any, Optional" not in SRC
    assert "Optional" not in SRC


def test_find_routine_behaviour_preserved():
    """Behaviour preservation: find_routine still matches by id and name."""
    automations = [
        {"automationId": "amzn1.alexa.automation.1", "name": "Good Night"},
        {"automationId": "amzn1.alexa.automation.2", "name": "Good Morning"},
    ]
    assert routines.find_routine(automations, "amzn1.alexa.automation.1")["name"] == "Good Night"
    assert routines.find_routine(automations, "good night")["automationId"] == "amzn1.alexa.automation.1"
    assert routines.find_routine(automations, "no match") is None
    assert routines.find_routine(automations, "") is None
