"""Regression tests for UP045: modern X | None type annotations in groups.py.

The automated scanner flagged three UP045 findings in
cli_anything/alexa/core/groups.py.  This module pins the annotations so the
old ``Optional[X]`` style cannot regress.
"""

import ast
from pathlib import Path

from cli_anything.alexa.core import groups


SRC = Path(groups.__file__).read_text()
TREE = ast.parse(SRC)


def _function_node(name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(TREE):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found in groups.py")


def _annotation_source(node: ast.AST) -> str:
    return ast.unparse(node)


def test_find_group_return_uses_pipe_none():
    """find_group returns ``dict[str, Any] | None`` (UP045 line 121)."""
    fn = _function_node("find_group")
    assert _annotation_source(fn.returns) == "dict[str, Any] | None"


def test_build_create_variables_child_group_ids_uses_pipe_none():
    """build_create_variables child_group_ids is ``list[str] | None`` (UP045 line 213)."""
    fn = _function_node("build_create_variables")
    ann = fn.args.args[2].annotation
    assert _annotation_source(ann) == "list[str] | None"


def test_build_update_variables_child_group_ids_uses_pipe_none():
    """build_update_variables child_group_ids is ``list[str] | None`` (UP045 line 237)."""
    fn = _function_node("build_update_variables")
    ann = fn.args.args[3].annotation
    assert _annotation_source(ann) == "list[str] | None"


def test_no_optional_import_in_groups_module():
    """The ``Optional`` name must not be imported from typing in groups.py."""
    assert "from typing import Any, Optional" not in SRC
    assert "Optional" not in SRC


def test_build_create_variables_none_child_group_ids_omitted():
    """Behaviour preservation: ``None`` child ids are omitted from variables."""
    v = groups.build_create_variables("Den", ["amzn1.alexa.endpoint.a"])
    assert "childDeviceGroupIds" not in v["in"]


def test_build_update_variables_none_child_group_ids_omitted():
    """Behaviour preservation: ``None`` child ids omit child op fields."""
    v = groups.build_update_variables("g1", ["amzn1.alexa.endpoint.a"], "add")
    assert "childDeviceGroupIds" not in v["in"]
    assert "childDeviceGroupIdsUpdateOperation" not in v["in"]
