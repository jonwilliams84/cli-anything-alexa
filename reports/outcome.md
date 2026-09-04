# Refine Outcome — notification EDITS + account/device introspection

## Summary

One coherent pass over the two clusters the previous pass flagged as "not
covered": **editing an existing alarm/timer/reminder** (`set_notifications`,
the only write in `/api/notifications` the harness never used) and the
**diagnostics reads** (`get_authentication`, `get_device_preferences`,
`get_wifi_details`). They landed together on purpose — a reminder's schedule is
expressed in the owning Echo's *timezone*, which is exactly what
`get_device_preferences` returns, so the edit surface needs the read.

Nine new commands, no command changed or removed:

| Command | alexapy call(s) | Notes |
| --- | --- | --- |
| `notifications show <id\|label>` | `get_notifications` | display row + the **raw record** an edit is built from |
| `notifications pause\|resume <id\|label>` | `set_notifications` | `status: OFF`/`ON`; dry-run + `--yes`, **verified** |
| `notifications reschedule <id\|label> --in N\|--at MS` | `set_notifications` (+ `get_device_preferences`) | moves `alarmTime` **and** the local wall-clock fields; dry-run + `--yes`, **verified** |
| `notifications snooze <id\|label> [--minutes N]` | `set_notifications` (+ `get_device_preferences`) | default 9 min (Amazon's own); dry-run + `--yes`, **verified** |
| `auth whoami` | `get_authentication` | customer id / name / email / Prime Music; exits non-zero when the cookie no longer buys an account |
| `echos preferences [<device>]` | `get_device_preferences` | `timeZoneId`, locale, temperature/distance units, postal code |
| `echos wifi [<device>]` | `get_wifi_details` | device-bound, so it goes through `DeviceRef` |

Tests: **1187 → 1329** (+142). Coverage: **89% → 90%**, `core/notifications.py`
at **100%** (statements *and* branches), `core/devices_meta.py` 97% → **98%**.
CI `--cov-fail-under` raised 86 → **87**. Public `alexapy` `AlexaAPI` methods
referenced by the harness: **48 → 52 of 58** (+`set_notifications`,
`get_authentication`, `get_device_preferences`, `get_wifi_details`).

## 1. The finding that shaped the module: an edit is a whole-record PUT

`/api/notifications` does not patch — it **replaces** the notification with the
body it is given, and accepts a short body silently. Building a "minimal" edit
payload (`{id, status}`) therefore looks like it worked while quietly dropping
the record's recurrence rule and owning device. Every builder in
`notifications.py` starts from a **copy of the record Amazon returned**, which
is also why `fetch_notifications` (raw) exists alongside the pre-existing
`list_notifications` (display rows): the rows are lossy and must never be an
edit source. `notifications show` exposes the raw record for the same reason.

## 2. A reminder fires off LOCAL wall-clock fields, not just `alarmTime`

A reminder/alarm record carries `alarmTime` (epoch ms) *and*
`originalDate`/`originalTime` — the date and time-of-day, **in the owning
Echo's timezone**, that the app displays and the schedule is rebuilt from.
Moving `alarmTime` alone leaves those stale.

`build_reschedule` therefore recomputes them from the new instant using that
Echo's own `timeZoneId` (read via the newly wrapped `get_device_preferences`),
and:

* rewrites them **only when the record already had them** — adding them to a
  record Amazon sent without them would invent a schedule shape;
* falls back to **UTC and says so** (`tz: UTC` in the output) when the
  preferences read fails or the zone is unknown to the host, rather than
  silently writing the wrong local time;
* never lets a preferences failure block the edit (it is enrichment, not a gate).

**Timers are excluded.** A timer counts down via `remainingTime` from the moment
it was set and has no `alarmTime` to move, so `reschedule`/`snooze` refuse it
locally — before any write — and say "delete it and create a new one".

## 3. The PUT cannot report success, so the writes verify

`AlexaAPI.set_notifications` is wrapped in alexapy's `_catch_all_exceptions`: a
rejected request and an accepted one both come back empty. Following the rule
`kids enable` and `devices delete --verify` already established, `apply_update`
re-reads the notification list afterwards and sets `ok` from what Amazon
actually holds.

The refinement over `kids`: `/api/notifications` is **rate-limited** and alexapy
returns `None` when throttled, so a verify read can fail for reasons that have
nothing to do with the edit. `ok` is therefore three-valued — `True`, `False`,
or **`None` with a note** when the record could not be re-read. Collapsing that
into `False` would report a successful edit as a failure whenever Amazon
throttled.

## 4. Safety decisions

* **Plan, then apply the same plan.** `plan_update` does all resolution and
  validation and returns the payload *plus* a readable `change` diff; the
  dry-run prints that diff and `--yes` applies that exact plan. So an unknown
  target, an ambiguous label or a timer-reschedule fails **identically with and
  without `--yes`**, and the thing reviewed is the thing executed.
* **The dry-run shows the diff, not the record.** A 30-key whole-record payload
  is unreviewable; `field: from -> to` is. The payload/`before` keys are
  stripped from the preview (a test pins this).
* **Ambiguity aborts.** Two alarms can honestly share the label "Wake up", so
  >1 match lists the ids to pick from — the harness rule, and the id tier
  resolves first so only a genuine collision aborts.
* **A no-op edit is reported, never written.** Pausing an already-paused alarm
  has an empty diff: the CLI says so and does not PUT.
* **`auth whoami` exits non-zero** on `authenticated: false`, so it is usable as
  a scripted liveness check that is sharper than `auth status` (cookie valid) —
  the two answers can genuinely differ when a rotated HA cookie goes stale.

## 5. Tests

| File | Tests | Covers |
| --- | --- | --- |
| `tests/test_notifications_edit.py` | 75 | the pure layer (id/label extraction, status synonyms, 3-tier resolution + ambiguity + the "known:" message, local wall-clock computation incl. BST and unknown-zone fallback, the whole-record builders incl. not mutating the source and not inventing local fields, timer refusals, snooze arithmetic from the alarm-time vs from now, the diff, tz-aware rendering) and every live wrapper against a fake `AlexaAPI` — plan-without-writing, the timezone lookup and its fallbacks, and the verify reporting `True` / `False` / `None`-when-unreadable |
| `tests/test_cli_notifications_edit_paths.py` | 45 | the CLI paths: the dry-run contract on all four edits (preview the diff, no apply without `--yes`, `--yes` applies the *same* plan object), that each edit asks for exactly one kind of change, `--in`/`--at` exclusivity refused **before** `_login`, the no-op path, target-required parsing, plus `notifications show`, `auth whoami` (incl. the non-zero exit) and `echos preferences`/`wifi` |
| `tests/test_device_reads_account.py` | 22 | the new pure row builders (preferences incl. both wake-word-confirmation spellings and the bare-list shape, `device_timezone`, the wifi envelope unwrap and all-`None` empty payload, `account_row`) and the wrappers — including that `echos wifi` binds `AlexaAPI` to a **`DeviceRef`**, not a raw dict |

Every assertion is on observable behaviour — exit code, JSON on stdout, which
core coroutine was called with what — never on source text.

## 6. Docs

* `README.md` — 8 table rows, an "Alarms, timers & reminders" section (whole-
  record PUT, local wall-clock fields, pause ≠ delete, the timer refusal, the
  three-valued `ok`) and an "Account & device introspection" section; test/
  coverage counts refreshed.
* `cli_anything/alexa/README.md` — the matching rows.
* `CLAUDE.md` (SOP) — `notifications.py` / `devices_meta.py` / `session.py`
  Layout entries extended, a new gotcha entry for the whole-record-PUT +
  local-fields + throttled-verify rules and the `auth status` vs `auth whoami`
  distinction, and a new entry under **Verified → assumptions worth checking
  live**.
* `skills/SKILL.md` — the agent-facing command list, flagging the whole-record
  rule, the timer refusal and `ok: null` ≠ failure.

## Gates

| Gate | Result |
| --- | --- |
| `pytest tests` | **1329 passed**, 0 failed |
| `--cov-fail-under=87` | **90%** (raised from 86) |
| `ruff check cli_anything/` | clean |
| `ruff format --check cli_anything/` | clean |
| `bandit -r cli_anything/ -ll` | 0 findings |

No regressions: all 1187 pre-existing tests still pass.

## Not covered (next refine pass)

* `get_devices_gql` — the GraphQL device list; the canonical `endpoints` query
  in `endpoints.py` already covers the smart-home graph, so the only gain would
  be a richer `echos list`.
* `set_background` (Echo Show wallpaper) — device-bound and niche, but the last
  unwrapped *write*.
* `ping` — a cheap liveness probe; would fit as `auth status --ping` now that
  `auth whoami` exists.
* `find_wake_word` / `force_logout` / `update_login` — subsumed by
  `echos wake-words` / not a real API call (`force_logout` just raises) /
  internal to alexapy's own session handling. Deliberately skipped.

That leaves 6 of alexapy's 58 public `AlexaAPI` methods unreferenced, 3 of them
deliberately.

Still true, and still the single biggest gap in confidence: **nothing in the
harness has had a mutation executed against a real account.** The notification
edits add four more to that list — see CLAUDE.md's Verified section, where
`notifications show` is flagged as the safe read to try first.

---

# Refine Outcome 2 — recurring alarms & reminders (the recurrence surface)

## Summary

One coherent gap closed: **recurrence**. Alexa alarms/reminders carry
`recurringPattern` (`DAILY`/`WEEKDAYS`/`WEEKENDS`/`WEEKLY`) and — for a named
weekly rule — `rRuleData.byWeekDays`. The harness *preserved* those fields on
every whole-record edit but had no way to create or change them. Now:

| Command | alexapy call(s) | Notes |
| --- | --- | --- |
| `notifications add-alarm --repeat daily\|weekdays\|weekends\|weekly [--days Mon,Thu]` | `get_notifications`+`get_devices` (as before) + raw POST create | stamps `recurringPattern` (+`rRuleData.byWeekDays`) at creation |
| `notifications add-reminder ... --repeat ...` | same | same for reminders |
| `notifications repeat <id\|label> <pattern\|none> [--days ...]` | `set_notifications` | set or **clear** the rule; dry-run diff → `--yes` → re-read verify, like every other edit |
| `notifications list` | `get_notifications` | new `recurring` column |

Pure helpers: `normalize_recurrence`, `normalize_recurrence_days`,
`is_recurring`, `build_recurrence_update`, `set_recurrence`;
`build_alarm`/`build_reminder` gained `recurring_pattern`/`recurrence_days`.

Design notes worth keeping:
- A `none`-word (or `--repeat none`) CLEARS by removing both fields — an
  explicit absence, matching how the app writes "no repeat".
- A weekday list with a fixed-day pattern (`daily`/`weekdays`/`weekends`) is
  refused — those patterns name their own days.
- Timers are refused (they count down exactly once).
- Repeat words are validated in the CLI BEFORE `_login`, so bad input fails
  identically with and without `--yes`.

Tests: **1329 → 1398** (+69). Coverage: **89.9% → 90.6%**;
`core/notifications.py` stays **100%**. One existing expectation updated for
the new `recurring` row field (`notifications list` schema addition) — no test
weakened. Version bumped **0.2.0 → 0.3.0** (minor: new commands).
