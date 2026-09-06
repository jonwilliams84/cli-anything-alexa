# Changelog

## [0.5.0] — 2026-09-06

- Updated `claude.md`, `readme.md`, `test.md`, `cli_anything/alexa/alexa_cli.py`, `cli_anything/alexa/core/devices_meta.py`, `cli_anything/alexa/core/endpoints.py`. (7 files changed, 228 insertions(+), 5 deletions(-))

## Unreleased — refine round (2026-09-06)

### Added — the last unwrapped alexapy surfaces

Three `AlexaAPI` calls the first build did not wrap, found by diffing alexapy's
public surface against the CLI:

- `echos wake-word [<device>]` — one Echo's wake word via `AlexaAPI.
  find_wake_word` (device-bound; answers from the same feed as
  `echos wake-words`, targeted at a single speaker; an unreadable word stays
  `None` rather than implying "alexa").
- `devices capabilities` — per-appliance **detail** read via alexapy's own
  GraphQL smart-home query (`get_devices_gql`): the only read returning
  `applianceTypes`, the capability list (counted), `modelName`, `connectedVia`
  and the HA `entityId` per appliance. Read-only enrichment — targeting still
  resolves through the canonical `endpoints` query.
- `echos background <url> [--device ...]` — set an Echo Show's background to
  an https image URL (`--yes` to execute). The URL is validated **before**
  login, because alexapy merely warns about plain `http://` and posts it
  anyway; `ok` comes straight from alexapy's success bool (no verify re-read
  needed, unlike the Kids writes).

New pure helpers: `devices_meta.wake_word_row`, `devices_meta.
background_url_problem`, `endpoints.gql_appliance_rows`; new wrappers
`devices_meta.fetch_wake_word` / `set_background`,
`endpoints.fetch_appliance_details`.

Two alexapy methods are deliberately NOT wrapped, now documented in CLAUDE.md:
`force_logout()` (a stub that unconditionally raises a swallowed
`AlexapyLoginError` — no server-side effect) and `update_login()` (internal
plumbing).

Tests: 1456 → 1487 (+31, `tests/test_refine_surfaces.py`). Coverage:
97.25% → 97.29%. No existing test weakened.

## [0.4.0] — 2026-09-06

- Updated `test.md`. (1 file changed, 14 insertions(+), 3 deletions(-))

## 0.3.0 — 2026-09-04

### Added — recurring alarms & reminders (the recurrence surface)

Alarms and reminders can now repeat, the way the Alexa app's "Repeats" picker
does. Previously the harness *preserved* a record's recurrence (every edit is
a whole-record PUT) but could neither create nor change it.

- `notifications add-alarm --repeat daily|weekdays|weekends|weekly
  [--days Mon,Thu]` — create a recurring alarm (`--yes` to execute).
- `notifications add-reminder <label> ... --repeat ...` — same for reminders.
- `notifications repeat <id|label> daily|weekdays|weekends|weekly|none
  [--days Mon,Thu]` — set or **clear** the recurrence on an existing
  alarm/reminder; dry-run diff by default, `--yes` applies and re-reads to
  verify (same plan → apply → verify cycle as every other edit).
- `notifications list` gains a `recurring` column (the record's
  `recurringPattern`).

Under the hood: Amazon's `recurringPattern` vocabulary (`DAILY`/`WEEKDAYS`/
`WEEKENDS`/`WEEKLY`) plus `rRuleData.byWeekDays` for a named-day `weekly`
rule; `none` clears both fields (the explicit absence the app writes). A
weekday list with a fixed-day pattern (`daily`/`weekdays`/`weekends`) is
refused, as is repeating a timer. Repeat words are normalised
case-insensitively and validated **before** any login/network call.

New pure helpers in `core/notifications.py`: `normalize_recurrence`,
`normalize_recurrence_days`, `is_recurring`, `build_recurrence_update`,
`set_recurrence`; `build_alarm`/`build_reminder` gained optional
`recurring_pattern`/`recurrence_days`.

Tests: 1329 → 1398 (+69, all unit + CLI-path; no existing test weakened — the
one updated expectation is `notifications list`'s new `recurring` row field).
Coverage: 89.9% → 90.6%. `core/notifications.py` stays at 100%.
alexapy surface unchanged (recurrence rides the existing
`get_notifications`/`set_notifications` calls).

## 0.2.0 — earlier releases

See `git log` for the history up to 0.2.0 (browser-proxy login, notifications
edit surface, account/device introspection, Amazon Kids, device reads).
