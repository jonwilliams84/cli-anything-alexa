# Changelog

## [0.11.0] — 2026-10-07

### Added — selective voice-history deletion (`activity clear --device/--contains/--ids`, filters on `activity records`)

`activity clear` could previously delete only the N *most recent* recordings
as a block. But the endpoint underneath is a **per-id**
`DELETE /api/activities/<id>` — the same activity id the legacy feed already
carries — so the deletion can be named exactly, and the actual privacy use
case ("wipe this Echo's recordings", "wipe every turn about X") was unexpressed.
The bulk block stays; the selective modes are added alongside it:

New `activity` coverage (`--json` throughout):

| Command | Wraps | Notes |
| --- | --- | --- |
| `activity clear --items N` | `AlexaAPI.clear_history` | unchanged bulk behaviour |
| `activity clear --device <name> [--contains <text>]` | `get_activities` + one `DELETE /api/activities/<id>` per id | fetches the legacy feed (`--limit`, default 100), selects with the **same client-side predicates** `activity history` uses, previews the exact rows |
| `activity clear --contains <text>` | same | text-only selective |
| `activity clear --ids <id[,id...]>` | one `DELETE /api/activities/<id>` per id | explicit ids (from `activity records`), fetched records are not re-read |
| `activity records [--device ...] [--contains ...]` | `get_activities` | the same filters, so the ids a selective clear will delete can be listed first |

Three conventions carried over from the lock/thermostat/scene surfaces:

* **One plan: the preview is the execution.** A selective run builds its
  selection once (`plan_clear`) — the rows the dry-run shows are the rows
  `--yes` deletes. All mode validation (`--items` is bulk-only; `--ids` is
  exclusive with the filters; `--limit` needs filters; an empty `--ids`)
  happens **before** `_login`, so bad input fails identically with and
  without `--yes`.
* **Deletions are three-valued per id.** 200 → deleted; **404 → refused**
  ("no recording behind that id — remove it manually in the Alexa app");
  anything else / no answer → **unconfirmed** (`deleted: null` — the id may
  still exist; re-check with `activity records`). `cleared` is true only when
  every id is confirmed gone — never a quiet pass.
* **Nothing matched is an answer, not an error.** An empty selection reports
  `matching: 0 / deleted: 0` and deletes nothing, instead of failing.

Tests: 1731 → **1776** (+45: pure + wrapper tests appended to
`test_activity.py`, CLI paths + preview→`--yes`→per-id-DELETE workflows in the
new `tests/test_cli_activity_clear_paths.py`). One pre-existing expectation
was updated for the `activity records` filter parameters — no test weakened.
Coverage holds at **97.3%** (gate ≥87% unchanged). Version bumped 0.10.0 →
**0.11.0** (by the release runner).

## [0.10.0] — 2026-09-30

### Added — smart-home scenes (`scenes list` / `scenes activate`)

The last one-tap surface the harness could inventory but never actuate:
**scenes** — the Alexa app's scene buttons. Scenes surface to the smart-home
graph as appliances whose `applianceTypes` carry the `SCENE` marker (plus the
sibling `SMARTPLAY` "smart play" markers the app treats the same way), so they
have been visible in `devices list` from day one — but there was no way to
*tap* one. alexapy owns none of the `Alexa.SceneController` surface, so —
exactly like the 0.8.0 lock and 0.9.0 thermostat surfaces — the request is
built locally and sent through `AlexaAPI._static_request`: one `Activate`
controlRequest per scene, on the same `PUT /api/phoenix/state` the lights,
Guard, locks and thermostats already ride:

```json
{"controlRequests": [{"entityId": "…", "entityType": "ENTITY",
                      "parameters": {"action": "Activate"}}]}
```

New commands (`--json`):

| Command | Wraps | Notes |
| --- | --- | --- |
| `scenes list` | the canonical `endpoints` query, filtered to `SCENE`/`SMARTPLAY` appliances | Name, phoenix entityId, applianceId/endpointId, `HA` vs `native` source; sorted by name. A scene with `entityId: null` is listed but **not activatable** |
| `scenes activate <target...> [--all]` | `PUT /api/phoenix/state`, `Alexa.SceneController` `Activate` | `--yes` to execute; `--all` for every scene (careful). Targets resolve like every control command — **scoped to scenes only**, a light is not a scene and refuses locally |

Two honest-reporting notes, mirroring the lock/thermostat conventions:

* **No verify re-read, on purpose.** A scene is *instantaneous* — there is no
  held state to read back after the write, so inventing one would be a lie.
  Instead `ok` is read off the write's own response
  (`activate_verify`): `true` when every `controlResponses[].code` is
  `SUCCESS`, `false` on any failure code, and `null` when the response carries
  no `controlResponses` at all — "nothing to check", never a silent pass.
* **Preview by default.** `scenes activate` is dry-run unless `--yes`, prints
  the scene names it would tap plus the `--yes` hint, and sends nothing in
  preview. Ambiguous names abort with the matches listed (same rule as
  `devices rename`), and an account with no scenes aborts with a plain
  "no scenes" rather than a silent empty run.

The detection/filter/row/wire-format/verify layer is pure
(`is_scene` — string-tolerant, `scene_rows` — sorted and entityless-safe,
`scene_control_request`, `activate_verify`) and the network is the usual thin
`_static_request` pair: in the new `core/scenes.py`, unit-tested in
`tests/test_scenes.py` (+35) with the CLI paths in
`tests/test_cli_scenes_paths.py` (+13, including a dry-run → `--yes`
round-trip shape, `--all`, and every abort path).

Tests: 1679 → **1727** (+48). Coverage holds at **97.2%** (gate ≥87%
unchanged). Version bumped 0.9.0 → **0.10.0** (by the release runner).

## [0.9.0] — 2026-09-22

### Added — thermostat control (`devices temperature`)

The last smart-home capability the harness could read but never actuate:
**thermostats**. `devices state` already surfaces the live
`Alexa.ThermostatController.targetSetpoint`, and the phoenix `controlRequests`
PUT that drives lights (`turnOn`/`setBrightness`), Guard
(`controlSecurityPanel`) and locks (`lock`/`unlock`) carries the thermostat
verbs too — `setTargetSetpoint`, `adjustTargetTemperature`, `setMode` — but
alexapy's request builder has no thermostat actions, so (exactly like the 0.8.0
lock surface) `core/smarthome.py` builds the app's own request shape and sends
it through `AlexaAPI._static_request`:

```json
{"controlRequests": [{"entityId": "…", "entityType": "ENTITY",
                      "parameters": {"action": "setTargetSetpoint",
                                     "targetSetpoint": {"value": "21.0", "scale": "CELSIUS"}}}]}
```

New command (`--json`, dry-run by default + `--yes`):

| Command | Wraps | Notes |
| --- | --- | --- |
| `devices temperature [<target>...] [--all] --setpoint N` | `PUT /api/phoenix/state`, `setTargetSetpoint` | Absolute target; `--scale celsius\|fahrenheit` (default celsius) |
| `devices temperature … --adjust N` | `adjustTargetTemperature` | Relative nudge; **mutually exclusive** with `--setpoint` |
| `devices temperature … --mode heat\|cool\|auto\|off\|eco\|custom` | `setMode` | May ride along with either temperature verb ("set to 21 and switch to heat") |

Targets resolve exactly like `devices on/off/lock` (applianceId / endpoint id /
display name; ambiguity aborts). Every bad value — setpoint + adjust together,
no verb at all, a non-numeric temperature, an unknown mode or scale — is
refused at the parser **before `_login`**, so it fails identically with and
without `--yes`, and the dry-run and the `--yes` run share one
`plan_thermostat_change`.

A thermostat write's response answers nothing useful, so every executed write
is **verified by a fresh re-read** of `targetSetpoint` (and `thermostatMode`
when a mode was asked), three-valued like the lock/kids/notifications verifies:
`ok` is `true`/`false` from what Amazon holds, `null` when the verify read
answered nothing — "could not check", never a silent pass. Two honest-verify
details worth knowing:

* **`--adjust` is checked against the pre-write setpoint.** A relative write
  can only be verified against what the thermostat held *before* the PUT, so
  the CLI reads it first; if that pre-read answers nothing, the write still
  happens and `ok` is `null` — never a guessed pass.
* **Scale differences don't fake a failure.** A thermostat reporting 69.8 °F
  is exactly what a 21 °C ask looks like — the verify converts (±0.5°
  cross-scale slack vs ±0.05° same-scale) instead of reporting a mismatch.

Pure helpers (`normalize_scale`, `normalize_temperature`,
`normalize_thermostat_mode`, `plan_thermostat_change`, `thermostat_state`,
`thermostat_mode_state`, `thermostat_verify`) and the network pair
(`set_thermostat_state`, `verify_thermostat_write`) live in
`core/smarthome.py`; unit tests in `tests/test_smarthome.py`, CLI paths plus a
preview → `--yes` round-trip (executed actions == previewed actions) and an
adjust-reads-the-pre-write-setpoint workflow in
`tests/test_cli_smarthome_paths.py`.

Tests: 1613 → **1679** (+66). Coverage holds at **97.2%** (gate ≥87%
unchanged). Version bumped 0.8.0 → **0.9.0** (by the release runner).

## [0.8.0] — 2026-09-15

### Added — lock control (`devices lock` / `devices unlock`)

The one smart-home capability the harness could inventory and read but never
actuate: **locks**. All 58 public `AlexaAPI` methods in alexapy are already
wrapped, and none of them is a lock write — so the lock surface rides
`AlexaAPI._static_request("put", login, "/api/phoenix/state", …)` directly
(the same reuse-the-helper pattern as groups and lists) with the same
`controlRequests` shape the light verbs (`turnOn`/`setBrightness`) and the
Guard arm (`controlSecurityPanel`) already use:

```json
{"controlRequests": [{"entityId": "…", "entityType": "ENTITY",
                      "parameters": {"action": "lock"}}]}
```

New commands (both `--json`, both dry-run by default + `--yes`):

| Command | Wraps | Notes |
| --- | --- | --- |
| `devices lock [<target>...] [--all]` | `PUT /api/phoenix/state`, `action: "lock"` | Targets resolve exactly like `devices on/off` (applianceId / endpoint id / display name; ambiguity aborts) |
| `devices unlock [<target>...] [--all]` | `PUT /api/phoenix/state`, `action: "unlock"` | Same contract, opposite verb |

A lock write's response answers nothing useful — a bare control response, no
state — so every executed write is **verified by a fresh re-read** of
`Alexa.LockController.lockState` (the established kids/notifications
write-verify pattern): `ok` is `true`/`false` from what Amazon actually holds,
and `null` when the verify read answered nothing (device unreachable or
throttled) — "could not check", never a silent pass. `JAMMED` is a real state:
reported verbatim as `lockState` and counted as `ok: false`, never collapsed
into UNLOCKED. The write is refused up front for a device with no phoenix
`entityId` (the state API would accept it and quietly do nothing).

Pure helpers (`lock_action`, `lock_state`, `lock_verify`) and the network pair
(`set_lock_state`, `verify_lock_write`) live in `core/smarthome.py`; unit
tests in `tests/test_smarthome.py` (exact controlRequests body, verb mapping,
three-valued verify, non-JSON/missing-response survival), CLI paths + a
lock → unlock round-trip workflow in `tests/test_cli_smarthome_paths.py`.

Tests: 1586 → **1613** (+27). Coverage: 97.19% → **97.20%** (gate ≥87%
unchanged); `core/smarthome.py` at 99%. Version bumped 0.7.0 → **0.8.0**
(by the release runner).

## [0.7.0] — 2026-09-09

### Added — session lifecycle (`auth ping` / `auth refresh` / `auth logout` / `auth totp`)

The auth surface ended at `status`/`whoami`: alexapy's own session-lifecycle
calls were unwrapped, and there was no way to log out at all. New commands
(all `--json`):

| Command | Wraps | Notes |
| --- | --- | --- |
| `auth ping` | `AlexaAPI.ping` (`/api/ping`) | The app's own authenticated health check — answers "does this session still buy live API traffic" (`auth status` only tests the cookie). `ok` + raw `detail`; exits non-zero when dead. Read-only, no `--yes`. |
| `auth refresh` | `AlexaLogin.refresh_access_token` | OAuth `/auth/token` exchange from the cookie's refresh token — renews the access token without re-login or touching the cookie. Exits non-zero when the cookie has no refresh token (refused up front, no doomed network call) or the exchange fails. |
| `auth logout` | `AlexaLogin._cookiefile` deletion | **Destructive**, preview by default + `--yes`: deletes every cookie file alexapy maintains (versioned `.storage/...cookies` jar, both pickles, legacy txt) and reports `verified` from a fresh disk re-read — an unlink that fails, or a directory squatting on the path, is a failure, never a silent success. **Refused under `--cookie-dir`** (read-in-place): that cookie belongs to another tool (e.g. HA) — deleting it would break their session. |
| `auth totp` | `pyotp` (the `set_totp`/`get_totp_token` half of alexapy) | Current 2FA code + seconds left for an `--otp-secret` — the code the scripted `auth login` flow will send, computable standalone for scripted/CI. No session, no network. |

Pure helpers (`ping_row`, `refresh_row`, `cookie_paths_in_dir`, `logout_plan`,
`logout_session`, `totp_row`) live in `core/session.py`; unit tests in
`tests/test_auth_lifecycle.py`, CLI paths in
`tests/test_cli_auth_lifecycle_paths.py` (the `--yes` logout path runs the
real filesystem delete, plus an end-to-end ping → refresh → logout workflow).

Tests: 1548 → 1586 (+38). Version bumped 0.6.0 → **0.7.0**.

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
