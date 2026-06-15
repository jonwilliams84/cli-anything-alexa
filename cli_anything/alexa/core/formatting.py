"""Pure table/JSON formatting helpers shared by the CLI.

Kept dependency-free (no click, no alexapy) so the row-building and column
logic can be unit-tested directly.
"""

from __future__ import annotations

import json
from typing import Any


def _fmt_cell(value: Any, max_width: int = 40) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.2f}"
    if isinstance(value, (list, dict)):
        s = json.dumps(value, default=str)
    else:
        s = str(value)
    return s if len(s) <= max_width else s[: max_width - 3] + "..."


def table_columns(rows: list[dict], max_cols: int = 10) -> list[str]:
    """Ordered list of column keys across rows (skipping `_`-prefixed)."""
    keys: list[str] = []
    for r in rows:
        for k in r.keys():
            if k not in keys and not str(k).startswith("_"):
                keys.append(k)
    return keys[:max_cols]


def render_table(rows: list[dict], max_cols: int = 10) -> str:
    """Render a list of dict rows as an aligned text table (no color)."""
    if not rows:
        return ""
    keys = table_columns(rows, max_cols=max_cols)
    widths = {
        k: max(len(k), max((len(_fmt_cell(r.get(k))) for r in rows), default=0))
        for k in keys
    }
    lines = [
        "  ".join(k.ljust(widths[k]) for k in keys),
        "  ".join("-" * widths[k] for k in keys),
    ]
    for r in rows:
        lines.append("  ".join(_fmt_cell(r.get(k)).ljust(widths[k]) for k in keys))
    return "\n".join(lines)
