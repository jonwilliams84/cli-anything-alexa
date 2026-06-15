"""Unit tests for the pure table/formatting helpers."""

from cli_anything.alexa.core import formatting


def test_table_columns_order_and_skip_underscore():
    rows = [
        {"a": 1, "b": 2, "_hidden": 9},
        {"a": 3, "c": 4},
    ]
    assert formatting.table_columns(rows) == ["a", "b", "c"]


def test_table_columns_max_cols():
    rows = [{f"k{i}": i for i in range(20)}]
    assert len(formatting.table_columns(rows, max_cols=5)) == 5


def test_render_table_empty():
    assert formatting.render_table([]) == ""


def test_render_table_alignment_and_header():
    rows = [
        {"name": "Kitchen", "ok": True},
        {"name": "Hall", "ok": False},
    ]
    out = formatting.render_table(rows)
    lines = out.splitlines()
    assert lines[0].split() == ["name", "ok"]
    # bool renders as yes/no
    assert "yes" in out and "no" in out
    # column is wide enough for the longest value "Kitchen"
    assert "Kitchen" in lines[2] or "Kitchen" in lines[3]


def test_render_table_truncates_long_cells():
    rows = [{"v": "x" * 100}]
    out = formatting.render_table(rows)
    assert "..." in out


def test_fmt_cell_none_and_float():
    assert formatting._fmt_cell(None) == "-"
    assert formatting._fmt_cell(3.14159) == "3.14"
    assert formatting._fmt_cell(True) == "yes"
    assert formatting._fmt_cell(False) == "no"
