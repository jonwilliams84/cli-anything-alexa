# Changelog

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
