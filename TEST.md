# TEST.md — what is tested and how to run it

The CI gate (must exit 0):

```bash
python -m pytest tests --cov=cli_anything --cov-fail-under=87 -q --durations=10 \
  && ruff check cli_anything/ --output-format=github \
  && ruff format --check --diff cli_anything/ \
  && bandit -r cli_anything/ -ll -x '*/tests/*,*/test_*.py,*/conftest.py'
```

## Current state (after 0.14.0 refine, 2026-10-28)

- **1907 tests pass**, 0 failures, in ~8 s (no live account, no network —
  alexapy is mocked; the push-event websocket is driven by a fake
  `WebsocketEchoClient`, pure logic without it).
- Coverage **97.3%** (gate: ≥87%). The 0.13.0 **push-event stream** wiring —
  `core/events.py` plus its `events status` / `events listen` CLI surface —
  is covered by `test_events.py` + `test_cli_events_paths.py` (module **96%**
  +); the CLI layer (`alexa_cli.py`) ~95%, the REPL skin ~97%.
- Lint (`ruff check`), format (`ruff format --check`) and bandit (-ll): clean.

## Added in 0.14.0 — the notification-volume surface

`echos notification-volume` (read) and `echos set-notification-volume`
(write) expose alexapy's `get_notification_states` / `set_notification_volume`
— the "Alarms, Timers, and Notifications" volume each Echo plays alerts at,
previously reachable only in the Alexa app:

- `tests/test_echo_state_reads.py` (notification-volume section) —
  `notification_volume_rows` (envelope *and* bare-list shapes, junk entries
  skipped, serial→name join, empty payloads empty), `normalize_notification_volume`
  (whole numbers 0–100 only: fractions like `30.5` refused rather than
  rounded, NaN/inf/bools/non-numeric refused, in-range ints and strings
  accepted), `resolve_notification_target` (name/serial match, unknown target
  refused **listing the account's device names**), `notification_request`
  (the exact PUT body; a record missing serial/deviceType/softwareVersion
  refused — the URL path embeds all three), `observed_notification_volume`
  (three-valued verify read: junk volume → `None`), and the async
  `fetch_notification_states` / `set_notification_volume` against a faked
  `AlexaAPI`: login passed through, the record's exact fields in the write,
  verify-by-reread (`ok` True when Amazon agrees, False when it differs,
  `None` when the verify read answers nothing), refusal of unknown devices /
  bad levels / half-addressable records **before any write**.
- `tests/test_cli_notification_volume_paths.py` — `echos notification-volume`
  (account rows, per-device filter, unknown-device abort) and `echos
  set-notification-volume` (dry-run preview with the normalized integer,
  `--yes` executes and reports the verified row, bad levels refused **before
  `_login`** identically with and without `--yes`, non-numeric levels refused
  by Click's own type, and the read → set → read workflow on one stubbed
  session consuming every staged answer).

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
- `test_refine_surfaces.py` — the refine-round surfaces: `endpoints.
  gql_appliance_rows` (alexapy `get_devices_gql` flattening, incl. capability
  counting + non-dict skipping), `devices_meta.fetch_wake_word` (per-Echo
  `find_wake_word`: serial passed verbatim, `None` kept as `None`, first-online
  default), `background_url_problem` (https-only validation) and
  `set_background` (url verbatim, `ok` straight from alexapy's bool,
  `DeviceRef` binding), plus the CLI paths (`echos wake-word`, `echos
  background` dry-run/`--yes`/pre-login https refusal, `devices capabilities`).
- `test_security_fixes.py` — asserts the import block and redaction behaviour
  the security gate cares about.
- `test_auth_lifecycle.py` — the 0.7.0 session-lifecycle surface in
  `core/session.py`: `ping_row` (ok from a live body; empty/error/None
  refused), `session_ping` against a faked `AlexaAPI.ping`;
  `refresh_access_token` (refused without a refresh token — no network call
  attempted, expiry iso-timestamp reported on success, `None` on failure or a
  garbled `expires_in`); `cookie_paths_in_dir` (mirrors alexapy's
  `_cookiefile` list, email sanitized), `logout_plan` (present vs absent) and
  `logout_session` (removes every cookie file, `verified` from a fresh disk
  re-read, a directory squatting on the path reported as `failed` — never a
  silent success); `totp_row` (code pinned to an instant, `valid_for`
  windowing, empty/non-base32 secrets raise `AlexaSessionError`).

- `test_events.py` (0.13.0) — the **push-event stream** core:
  `websocket_url` (region allow-list reused, `dp-gw-na-js` for `amazon.com`,
  unknown region refused), `mac_dms_ready` (both halves required — one half
  or blank is "half-baked", never ready; a hostile login object returns False,
  never a traceback), `channel_name` (0x361/0x362/0x65 → handshake/events/
  heartbeat, hex-string + whole-float forms accepted, unknown stays reportable
  as raw hex, junk → `None`), `event_row` (type-safe flattening: content
  attribute or dict forms, `json_payload` preferred, submessage type carried,
  junk skipped as `None`), `filter_events`/`json_blob_row` (case-insensitive
  combinable selection reaching into payload JSON, pass-through returns a
  copy), `listen_summary`, `validate_listen_options` (defaults, refusals), and
  `listen_events` against a fake `WebsocketEchoClient` fed from a background
  task: rows collected + socket closed, junk skipped, `--max-events` cap,
  timeout, close-when-peer-didn't, a resistant close surviving cleanly,
  protocol failures landing in the summary `error`, and the up-front refusals
  (no `mac_dms`, no alexapy module — before any socket).

CLI/E2E tests (`test_cli_*.py`): every command's observable contract —
preview-by-default / act-on-`--yes`, argument validation **before** any login
or network call, `--json` output shape, and which core coroutine each command
invokes (via stubbed `_run`). The notification-edit file
(`test_cli_notifications_edit_paths.py`) also covers `notifications repeat`
and the `--repeat` flags of `add-alarm`/`add-reminder`.
`test_cli_auth_lifecycle_paths.py` (0.7.0) covers `auth ping` (row +
non-zero exit when dead), `auth refresh` (row + non-zero exit when the
exchange fails), `auth logout` (preview with `would_remove` + hint, the
`--yes` path runs the REAL filesystem delete and reports `verified`, a
blocked delete exits non-zero, the read-in-place `--cookie-dir` refusal, the
missing-email abort) and `auth totp` (six-digit code + validity, clean abort
on a bad secret), plus an end-to-end ping → refresh → logout workflow over
one cookie story.
- `test_smarthome.py` / `test_cli_smarthome_paths.py` (0.8.0 additions) — the
  **lock** surface: `lock_action` (verb mapping), `lock_state` (`lockState`
  out of a capability read, entity-scoped, `None` when Amazon reports nothing,
  non-string values ignored), `lock_verify` (three-valued: True/False —
  `JAMMED` is a False — and **`None` when the read answered nothing**), and the
  async pair against a fake `_static_request`: `set_lock_state` posts the
  exact `controlRequests` body (`lock`/`unlock`, `entityType: ENTITY`) via
  `PUT /api/phoenix/state`, survives a non-JSON body, raises on a missing
  response; `verify_lock_write` re-reads and reports `ok` from the fresh
  payload. CLI: both verbs preview by default, `--yes` executes write → verify
  (row carries `lockState` + three-valued `ok`), `ok: null` is never a silent
  pass, targeting/ambiguity/no-entityId refusals, `--all`, and a
  lock → unlock round-trip workflow.
- `test_smarthome.py` / `test_cli_smarthome_paths.py` (0.9.0 additions) — the
  **thermostat** surface: `normalize_scale` / `normalize_temperature` /
  `normalize_thermostat_mode` vocabularies (empty refused with "required",
  unknown refused with the alternatives), `plan_thermostat_change`
  (setpoint/adjust mutual exclusion, nothing-to-change refusal, `--mode` riding
  along, exact `targetSetpoint`/`targetSetpointDelta` parameter shapes),
  `thermostat_state` / `thermostat_mode_state` (entity-scoped reads out of the
  capability payload, `None` when Amazon reports nothing or the value is
  unparseable), `thermostat_verify` (three-valued; cross-scale comparison
  converts with ±0.5° slack; `--adjust` needs the pre-write setpoint or the
  verify is honestly `None`), and the async pair against a fake
  `_static_request`: `set_thermostat_state` posts the exact `controlRequests`
  body (`setTargetSetpoint` / `adjustTargetTemperature` with the value nested
  in the request entry, `setMode`, `entityType: ENTITY`) via
  `PUT /api/phoenix/state`, survives a non-JSON body, raises on a missing
  response; `verify_thermostat_write` re-reads and reports `ok` from the fresh
  payload. CLI: previews by default (actions listed), `--yes` executes write →
  verify (row carries the held `targetSetpoint`, `mode` and three-valued
  `ok`), all five parser-level validations fire BEFORE `_login`, targeting /
  unknown-device / no-entityId refusals, `--all`, the `ok: null` path, and a
  preview → `--yes` round-trip pinning that the executed actions are exactly
  the previewed ones.
- `test_scenes.py` / `test_cli_scenes_paths.py` (0.10.0 additions) — the
  **scenes** surface: `is_scene` (`SCENE`/`SMARTPLAY` markers, case-tolerant,
  string/list/tuple forms, junk refused without tracebacks), `scene_rows`
  (filter + case-insensitive name sort, the row shape, `HA` vs `native`
  source, entityless scenes kept visible with `entityId: null`), the exact
  `Activate` wire format (`scene_control_request`), `activate_verify`
  (three-valued from `controlResponses[].code`, case-insensitive, `None` when
  the response answers nothing — never a silent pass), `fetch_scenes` (the
  endpoints read filtered to scenes) and the async `activate_scene` against a
  fake `_static_request` (exact `PUT /api/phoenix/state` body, non-JSON body
  → `ok: None`, missing response raises). CLI: `scenes list` (sorted rows,
  read-only, empty account), `scenes activate` preview by default with the
  `--yes` hint and no write coroutine in dry-run, `--yes` executes and reports
  the response-derived `ok`, appliance-id targeting, `--all`, the
  abort paths (ambiguity, unknown target, non-scene device, no scenes on the
  account, `--all`+targets, no targets).
- `test_cli_events_paths.py` (0.13.0) — the `events` CLI contract:
  `events status` (`--json` readiness row + text render + the
  `--url`-selected gateway, a key-less session hinted at `re-authenticate`),
  `events listen` (read-only — never needs `--yes`, defaults `timeout`
  30 s honoured, `--timeout`/`--max-events` passed through, `--json` summary
  with events embedded, text summary + row table), the client-side filters
  (`--service`/`--channel`/`--contains` on what was collected, `filters`
  reported), a socket failure reported verbatim in `error`, every
  `--timeout`/`--max-events` refusal firing BEFORE `_login`, and the
  status → listen workflow on one session.

`test_cli_refine_paths.py` closes the remaining CLI-layer gaps: both
`auth login` flows (scripted + guided proxy) and `auth import-pickle` success,
bulk `rename --pattern` / `--map` and single renames (dry-run + `--yes`),
`devices prune` / `delete --entity|--name --verify` (incl. native warnings),
`duplicates`, `discover --yes`, `echos list/bluetooth`, group create/add/
remove/set/delete execute paths (incl. `--device` display-name resolution),
routines and notifications execute paths, `announce`/`speak`/`dnd` under
`--yes`, the REPL loop (help / blank / unknown command / `exit` / `quit` /
Ctrl-C), `main()` + `python -m cli_anything.alexa`, and the ReplSkin banner /
history bootstrap / colour detection / prompt-toolkit fallbacks.

Workflow coverage: the apply path is asserted to feed the **planned** payload
verbatim into `apply_update`/`create_notification` (the dry-run and the
`--yes` run are the same plan), and the verify re-read pins the final `ok`.

- `tests/test_activity.py` — plus the 0.11.0 **selective clear** layer:
  `parse_ids` (comma/whitespace splitting, order-preserving dedupe, refusal of
  an empty selection), `id_preview_rows` (ids-only previews never guess),
  `plan_clear` (filter selection reusing the `activity history` predicates,
  id-less rows never selected, explicit ids overriding, empty selection as a
  valid answer), `delete_envelope` (three-valued per-id verdict: 200 → True,
  404 → False, anything else/no answer → `None`), `selective_clear_summary`
  (clean / refused-with-the-app-remedy / unconfirmed-with-the-
  re-check-with-`records` remedy / mixed), and the async pair against a fake:
  `delete_activities` sends one URL-quoted `DELETE /api/activities/<id>` per
  id, `apply_clear` executes a plan and reports nothing extra.
- `tests/test_cli_activity_clear_paths.py` — the selective `activity clear`
  CLI contract: the bulk path unchanged (preview needs no fetch; `--yes`
  still routes to `clear_history`), every mode conflict refused **before**
  `--login` (`--items` vs selective, `--ids` vs `--device/--contains`,
  `--limit` without a filter, an empty `--ids`), the ids preview that never
  fetches, the filter preview that fetches but never deletes (pass-through
  `--limit`, default 100), preview → `--yes` executing **exactly the ids the
  preview advertised**, device+contains combined, "nothing matched" reported
  rather than deleted-into, partial refusal / unconfirmation reporting, and
  the companion filter options on `activity records`.
- `tests/test_lists.py` — the `lists` surface's pure half (list/item row
  flattening, `find_list`/`resolve_item` with ambiguity refusal, the
  add/update payload builders, `add_verify_summary`'s three-valued report)
  and the network wrappers against a fake `_static_request`: the **www
  subdomain** every lists call must ride, path/limit/`nextToken` shapes,
  pagination clamping, non-JSON/missing-response errors, and every write's
  payload + version gating + verify semantics (`ok` True/False/None,
  `verified` from absence).
- `tests/test_cli_lists_paths.py` — every `lists` command's observable
  contract: reads, dry-run previews (list id + item id + **the version the
  `--yes` run will send** + payload), `--yes` execute paths (which core
  coroutine, with which args), `--status` validation **before** login, and
  unknown list/item refusals.
- `tests/test_lists_workflow.py` — multi-command workflows against a
  state-machine fake of the v2 lists API: the add → items → check → rename →
  remove round-trip, **stale-version refusal** (a concurrent edit bumps the
  version; the API answers non-JSON and the module raises), ambiguous list
  names never writing, pagination across pages, and the
  "added but off page 1" verify `null`.

## Not covered (known gaps)

- No live-account validation of `notifications repeat` yet — see
  `reports/outcome.md` and the SOP (`CLAUDE.md`) "assumptions worth checking
  live" section for the field shapes to confirm against a real account.
