# Changelog

## [0.6.0] — 2026-09-06

### Added — Alexa shopping & to-do lists (`lists`)

The one big Alexa surface no earlier round had touched: the app's **shopping
list, to-do list and custom lists**. Amazon serves them at
`/alexashoppinglists/api/v2/...` on the **www** host (not `alexa.amazon.<tld>`)
and alexapy does not wrap them, so `core/lists.py` talks to that surface via
`AlexaAPI._static_request(..., sub_domain="www")` — session, headers and the
401-retry stay correct. New commands (all `--json`, all writes dry-run by
default + `--yes`):

| Command | Endpoint(s) | Notes |
| --- | --- | --- |
| `lists list` | `POST lists/fetch` | shopping / to-do / custom rows |
| `lists items <list> [--limit N] [--pages N] [--status active\|complete] [--contains text]` | `POST lists/<id>/items/fetch` | paged (`nextToken`, max 100/page), filtered |
| `lists add <list> <text>...` | `POST lists/<id>/items` | `KEYWORD` items; verified by re-read (`found`) |
| `lists check\|uncheck <list> <item>` | `PUT lists/<id>/items/<id>?version=V` | version-gated; `ok` from re-read |
| `lists rename <list> <item> <new-name>` | `PUT lists/<id>/items/<id>?version=V` | version-gated; `ok` from re-read |
| `lists remove <list> <item>` | `DELETE lists/<id>/items/<id>?version=V` | version-gated; `verified` from absence |

Lists and items resolve by **name or id** (ambiguity refused with the ids).
Every write re-reads: `found`/`ok`/`verified` are three-valued — `null` means
"the item is not on page 1 (yet)", never silently success or failure. Pure
helpers (`list_rows`, `item_rows`, `find_list`, `resolve_item`,
`build_add_payload`, `build_attributes_update`, `add_verify_summary`,
`filter_items`, `normalize_status_word`) are unit-tested in
`tests/test_lists.py`; CLI paths in `tests/test_cli_lists_paths.py`; the
cross-command contracts (fresh-version threading, stale-version refusal,
pagination, ambiguity) run against a state-machine fake of the API in
`tests/test_lists_workflow.py`.

Tests: 1487 → 1548 (+61). Coverage: 97.29% → **97.13%** total (new module at
95%, gate ≥87% unchanged). Version bumped 0.5.0 → **0.6.0**.

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
