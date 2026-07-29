# Test Coverage Improvement Outcome

## Summary

Raised the repo's test coverage from 68% to 70.55% by adding 18 behavioural
tests targeting the lowest-coverage modules containing real logic. The CI
`--cov-fail-under` gate was raised from 68 to 70 (1 point below the achieved
figure, providing a buffer against rounding/flakes).

## What Was Done

### 1. Identified Lowest-Coverage Modules

Ran the repo's own coverage command (`python -m pytest tests --cov=cli_anything
--cov-report=term-missing`) and identified:

- `cli_anything/alexa/alexa_cli.py` — 26% coverage (lowest, contains real
  formatting/resolution logic in `emit`, `_abort`, `_resolve_one_or_abort`)
- `cli_anything/alexa/core/endpoints.py` — 85% coverage (uncovered error paths
  in `apply_renames`, `resolve_by_entity` edge cases, `find_duplicates`)

### 2. New Test File: `tests/test_cli_helpers.py` (18 tests)

All tests assert **behaviour** (stdout/stderr/exit codes/return values), not
source text. No test asserts on line numbers, suppression comments, or
hardcoded paths.

**`emit()` — all output branches:**
- `test_emit_json_serialises_and_sorts_keys` — JSON output with sorted keys
- `test_emit_none_produces_no_output` — None produces empty output
- `test_emit_string_echoes_directly` — string echoed verbatim
- `test_emit_list_of_dicts_renders_table` — list of dicts renders as table
- `test_emit_list_of_scalars_prints_each_on_own_line` — list of scalars, one per line
- `test_emit_dict_with_nested_value_uses_json_for_nested` — nested dict JSON-dumped
- `test_emit_dict_with_list_value_uses_json_for_list` — list value JSON-dumped
- `test_emit_fallback_str_for_unknown_type` — fallback to str() for float

**`_abort()`:**
- `test_abort_writes_error_prefix_and_exits_nonzero` — "error:" prefix + exit code 1

**`_resolve_one_or_abort()` — zero/one/many match paths:**
- `test_resolve_one_or_abort_returns_single_match` — single match returned
- `test_resolve_one_or_abort_zero_matches_aborts` — zero matches → exit 1 + error message
- `test_resolve_one_or_abort_multiple_matches_text_mode_lists_candidates` — ambiguous in text mode lists both candidates
- `test_resolve_one_or_abort_multiple_matches_json_mode_emits_structured_error` — ambiguous in JSON mode emits structured error JSON

**`endpoints.apply_renames` error paths:**
- `test_apply_renames_missing_endpoint_id_records_error` — missing endpointId → ok=False
- `test_apply_renames_empty_plan_returns_empty_list` — empty plan → empty list

**`endpoints.resolve_by_entity` edge cases:**
- `test_resolve_by_entity_empty_string_returns_empty` — empty entity_id → empty list
- `test_resolve_by_entity_none_records_returns_empty` — None records → empty list

**`endpoints.find_duplicates`:**
- `test_find_duplicates_two_native_same_name_no_twin_flag` — two native devices same name → native_plus_ha=False

### 3. CI Gate Raised

In `.github/workflows/ci.yml`, `--cov-fail-under` raised from 68 to 70.
Set 1 point below the achieved 70.55% to avoid red pipelines from rounding.

### 4. Housekeeping

- Removed `.coverage` binary artifact from git tracking
- Added `.coverage` to `.gitignore`

## Verification

```
359 passed in 2.81s
Required test coverage of 70% reached. Total coverage: 70.55%
```

The verify command (`python -m pytest tests --cov=cli_anything --cov-report=term-missing
--cov-report=xml --cov-fail-under=70 -q --durations=10`) exits 0.

## Coverage by Module (After)

| Module | Before | After |
|--------|--------|-------|
| alexa_cli.py | 26% | 33% |
| endpoints.py | 85% | 89% |
| **TOTAL** | **68%** | **71%** |
