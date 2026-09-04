# TEST.md — what is tested and how to run it

The CI gate (must exit 0):

```bash
python -m pytest tests --cov=cli_anything --cov-fail-under=87 -q --durations=10 \
  && ruff check cli_anything/ --output-format=github \
  && ruff format --check --diff cli_anything/ \
  && bandit -r cli_anything/ -ll -x '*/tests/*,*/test_*.py,*/conftest.py'
```

## Current state (after 0.3.0, 2026-09-04)

- **1398 tests pass**, 0 failures, in ~5 s (no live account, no network —
  alexapy is mocked; pure logic is tested without it).
- Coverage **90.6%** (gate: ≥87%). `core/notifications.py` at **100%**
  (statements + branches), including the recurrence surface.
- Lint (`ruff check`), format (`ruff format --check`) and bandit (-ll): clean.

## What the tests cover

Unit tests (per-module files under `tests/`):

- `test_notifications_edit.py` — the whole-record edit contract
  (pause/resume/reschedule/snooze/`repeat`), resolution by id-or-label with
  ambiguity refusal, timezone recomputation, verify semantics (`ok` True/False/
  None), and the **recurrence** helpers: `normalize_recurrence` /
  `normalize_recurrence_days` vocabularies, `build_recurrence_update`
  (set/weekly-with-days/clear, refusals), plan/apply round-trips.
- `test_pure_builders.py` — alarm/timer/reminder creation payload builders,
  row flatteners, group/routine/session pure helpers.
- `test_smarthome.py`, `test_media.py`, `test_endpoints.py`,
  `test_sequences.py`, `test_kids.py`, `test_bluetooth.py`, `test_activity.py`,
  `test_appliances.py`, `test_formatting.py`, `test_device_ref.py`,
  `test_session.py`, `test_rename_bulk.py`, … — one file per core module's
  pure logic.
- `test_security_fixes.py` — asserts the import block and redaction behaviour
  the security gate cares about.

CLI/E2E tests (`test_cli_*.py`): every command's observable contract —
preview-by-default / act-on-`--yes`, argument validation **before** any login
or network call, `--json` output shape, and which core coroutine each command
invokes (via stubbed `_run`). The notification-edit file
(`test_cli_notifications_edit_paths.py`) also covers `notifications repeat`
and the `--repeat` flags of `add-alarm`/`add-reminder`.

Workflow coverage: the apply path is asserted to feed the **planned** payload
verbatim into `apply_update`/`create_notification` (the dry-run and the
`--yes` run are the same plan), and the verify re-read pins the final `ok`.

## Not covered (known gaps)

- No live-account validation of `notifications repeat` yet — see
  `reports/outcome.md` and the SOP (`CLAUDE.md`) "assumptions worth checking
  live" section for the field shapes to confirm against a real account.
