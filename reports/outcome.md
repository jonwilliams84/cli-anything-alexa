# repl_skin.py — Top 3 Scanner Findings Fixed

## Findings Addressed

All three findings were in `cli_anything/alexa/utils/repl_skin.py`:

| # | Code | Location | Fix |
|---|------|----------|-----|
| 1 | F841 | line 291 — `accent_hex` in `prompt_tokens` | Removed the unused local assignment |
| 2 | F841 | line 449 — `sep_parts` in `table` separator | Removed the unused local assignment |
| 3 | I001  | line 493 — `prompt_toolkit` import block | Reordered imports alphabetically by module path |

## Root Cause & Fix Details

### F841 — `accent_hex` (prompt_tokens)
`prompt_tokens` built `class:`-style tokens for prompt_toolkit; the
`accent_hex = _ANSI_256_TO_HEX.get(self.accent, "#5fafff")` line computed a
hex colour that was never referenced. Removed the dead assignment. A
*separate* `accent_hex` in `get_prompt_style` is genuinely used (it feeds
`Style.from_dict`), so it was left untouched.

### F841 — `sep_parts` (table separator)
`table` computed `sep_parts = [self._c(_DARK_GRAY, _H_LINE * w) for w in col_widths]`
but only the immediately-following `sep_line` (which re-derives the widths
inline) is printed. Removed the dead `sep_parts` assignment; the printed
separator line is unchanged.

### I001 — prompt_toolkit import block
The four `from prompt_toolkit...` imports were out of module-path order.
Reordered to:
`prompt_toolkit` → `prompt_toolkit.auto_suggest` →
`prompt_toolkit.formatted_text` → `prompt_toolkit.history`.

## Verification

- `python3 -m ruff check --select F841,I001 ./cli_anything/alexa/utils/repl_skin.py`
  → **All checks passed** (exit 0).
- `python3 -m pytest tests/ -q` → **212 passed** (204 pre-existing + 8 new).

## Regression Tests

Added `tests/test_repl_skin_lint.py` (8 tests):

1. `test_prompt_tokens_has_no_unused_accent_hex` — source-level: `accent_hex`
   absent from the `prompt_tokens` body.
2. `test_prompt_tokens_returns_tokens` — behaviour preserved: returns a
   non-empty token list with `class:icon` first.
3. `test_ruff_no_f841_accent_hex` — ruff subprocess reports no F841 for
   `accent_hex`.
4. `test_table_separator_has_no_unused_sep_parts` — source-level: `sep_parts`
   absent from the `table` body.
5. `test_table_prints_separator` — behaviour preserved: `table` still prints
   the `───` box-drawing separator.
6. `test_ruff_no_f841_sep_parts` — ruff subprocess reports no F841 for
   `sep_parts`.
7. `test_repl_skin_import_block_is_sorted` — ruff `--select I001` passes on
   `repl_skin.py`.
8. `test_repl_skin_prompt_toolkit_imports_order` — the `prompt_toolkit`
   import block is ordered by module path.

## Commit

`b84f740` — "Fix F841 (accent_hex, sep_parts) and I001 in repl_skin.py"
(2 files changed, 118 insertions, 3 deletions).

## Notes

- The F401 finding (`FormattedText` imported but unused) was pre-existing
  and NOT among the reported top-3; it was left untouched to keep the fix
  minimal and targeted. The I001 regression test is scoped to
  `--select I001` so the unrelated F401 does not affect it.
- No `# nosec` suppressions were needed — all three findings were genuine
  and fixed at the source.
