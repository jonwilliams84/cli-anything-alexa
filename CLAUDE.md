# cli-anything-alexa

A `click`-based CLI + interactive REPL for managing **Amazon Alexa** over the
unofficial Alexa web API (the app's private endpoints), built on
[`alexapy`](https://pypi.org/project/alexapy/). Sibling of
`cli-anything-homeassistant` / `cli-anything-zigbee2mqtt` — same profile/JSON/
REPL pattern. **Python 3.10+** (a fresh proxy/scripted login round-trips its own
cookie on 3.10+; **3.14 is needed only to `import-pickle` a 3.14-written cookie**
— see Python-version note below). Every command supports `--json`. Primary auth
is a **browser-proxy login** that needs no Home Assistant.

## Layout
- `cli_anything/alexa/alexa_cli.py` — the Click CLI + REPL; all command wiring. Entry point `main`.
- `cli_anything/alexa/core/` — one module per surface:
  - `appliances.py` — **pure** logic: applianceId→entity parsing, whitelist load, prune planning. No deps. Unit-tested.
  - `formatting.py` — **pure** table/cell rendering. Unit-tested.
  - `session.py` — `alexapy.AlexaLogin` wrapper: **proxy browser login** (`proxy_login`, the primary `auth login` path — starts `AlexaProxy`, prints the access URL, polls `test_loggedin`, `finalize_login` → cookie + chmod 0600, always `stop_proxy`), scripted login (`fresh_login`, headless/CI fallback, TOTP via `set_totp`), cookie import, load/validate, csrf header, `proxy_access_url` (pure). `alexapy` imported lazily so the CLI loads without it.
  - `devices.py` — appliance list + raw `DELETE /api/phoenix/appliance/<id>` + raw `POST /api/phoenix/discovery` (discover).
  - `endpoints.py` — **canonical `endpoints` GraphQL query** (id + applianceId + manufacturer + display name + enablement) and all the pure resolution it powers: target resolution (applianceId→endpoint-id→exact-name→normalized-name, ambiguity-aware), entity/name resolvers, duplicate detection, `device_rows` filtering, `setEndpointFriendlyName` (rename) variables builder, **bulk/pattern rename planning** (`parse_sed`/`apply_sed`/`plan_pattern_renames`, `parse_rename_map`/`plan_map_renames`, `apply_renames`), **DACS speakable-name validation** (`speakable_name`/`is_speakable`/`speakable_warning`/`is_dacs_error`), and the **native-delete warning + re-sync verify** predicates (`native_delete_warning`, `reappeared_after_delete`). Network via `_static_request`; pure logic unit-tested.
  - `device_ref.py` — **pure** adapter: raw `get_devices()` dict → the *attribute* surface alexapy's device-bound methods read off `self._device`. Unit-tested against the alexapy contract.
  - `devices_meta.py` — physical Echo devices (announce/dnd/media/routine targets) + the static state reads: bluetooth pairings, wake words, DND status (pure row builders + thin fetchers).
  - `media.py` — Echo transport (`play`/`pause`/`next`/`previous`/`forward`/`rewind`/`stop`), volume, shuffle/repeat, `play_music`, and the `get_state` player read. Pure volume/provider/player-row helpers unit-tested.
  - `notifications.py` — alarms/timers/reminders: list + pure payload builders + POST/PUT/DELETE.
  - `routines.py` — behaviors list (with trigger utterance + best-effort `action_targets` summary) + trigger (device-bound `run_routine`). **Routine EDITS are not API-supported — Alexa-app-only** (see note below).
  - `control.py` — announce (`send_announcement`, chime + fan-out) + **speak** (`send_tts`, no chime, one speaker) + **push** (`send_mobilepush` / `send_dropin_notification` — lands in the Alexa APP, silent on the speakers) + dnd. Pure `normalize_push` unit-tested.
  - `smarthome.py` — smart-home **state reads + actuation** over `/api/phoenix/state`: `get_entity_state` (read) and `set_light_state` (the *generic* control call — a plug is a light with no brightness) + Guard (`static_set_guard_state`). Pure capability-state decoding / colour+brightness validation unit-tested.
  - `sequences.py` — the **behaviour** surface (`POST /api/behaviors/preview`): `run_command` (`run_custom` — literal text through Alexa's own parser), `run_sequence` (`Alexa.*.Play`), `run_skill`, `play_sound`, plus the sequence/sound catalogs. Pure normalisers unit-tested.
  - `activity.py` — voice **history**: privacy records (`get_customer_history_records`, the only feed with BOTH halves of a turn), legacy `/api/activities` (ids + status), `get_last_device_serial`, and `clear_history` (irreversible). Pure window/limit/row/filter logic unit-tested.
  - `bluetooth.py` — Echo **bluetooth writes**: `set_bluetooth` (connect an already-paired sink) + `disconnect_bluetooth` (all sinks). Pure MAC canonicalisation / per-Echo pairing extraction / target resolution unit-tested.
  - `groups.py` — device-groups (rooms) over **GraphQL** `/nexus/v1/graphql`: list/create/add/remove/set/delete, **including nested child groups** (`--child-group`, the rollup pattern). Pure variables-builders (member + `childDeviceGroupIds`) + name-normalize/lookup + entity→endpoint + child-group name→id resolution are unit-tested; network goes via `AlexaAPI._static_request`.
  - `project.py` — local profile (`~/.config/cli-anything-alexa/config.json`).
- `cli_anything/alexa/utils/repl_skin.py` — shared cli-anything REPL skin.
- `cli_anything/alexa/skills/SKILL.md` — packaged agent skill manifest.
- `tests/` — pytest: pure logic, the async wrappers against a fake `AlexaAPI`, and every CLI command path (no alexapy traffic / no live account).

## Build / test / run
```bash
pip install -e .                                    # console script
pip install -e '.[test]' && python3 -m pytest tests/ -v
cli-anything-alexa auth login                       # guided browser-proxy login (no HA)
cli-anything-alexa auth status
cli-anything-alexa devices list --json
```

## Conventions / gotchas
- **alexapy is async**; the CLI wraps each call in `session.run_async` (`asyncio.run`).
  Device-level ops (announce/dnd/run_routine) are alexapy *instance* methods
  (`AlexaAPI(device, login)`); graph/notification reads are *static* (`login` arg).
- **Auth = browser-proxy login (primary).** `auth login` with no `--password`
  runs `session.proxy_login`: it starts `alexapy.AlexaProxy`, prints a local
  URL, the user completes Amazon's own pages (captcha/2FA native), then we poll
  `login.test_loggedin()` and `finalize_login()` to persist the cookie. No HA.
  `--password [--otp-secret <base32>]` selects the scripted/headless fallback
  (`fresh_login`, TOTP via `login.set_totp`). `auth import-pickle` (reuse HA's
  `alexa_media` pickle) is a documented convenience, not the default.
- **HA-cookie reuse: read IN PLACE, don't copy.** HA's `alexa_media` rotates
  the cookie constantly, so an `import-pickle` *copy* goes stale within seconds
  (`auth status` flips `logged_in` true→false mid-session). Global `--cookie-dir
  <path>` (env `CLI_ALEXA_COOKIE_DIR`) points alexapy's `outputpath` at that dir
  so it reads/writes the cookie **in place** at
  `<dir>/.storage/alexa_media.<email>.pickle` — HA's exact layout (alexapy's
  `_cookiefile[0]`). `--cookie-dir /config` ⇒ HA's live pickle. `make_outputpath`
  takes `create=False` for read-in-place so we never mkdir/write a foreign dir;
  `cookie_path_in_dir(dir, email)` is the pure path helper.
- **Cookie/config-dir resolved ONCE** via `session.resolve_config_dir(cookie_dir)`:
  `--cookie-dir` flag → `CLI_ALEXA_COOKIE_DIR` env → `$HOME/.config/...` (only if
  `$HOME` is a real dir) → stable `/tmp/cli-anything-alexa` fallback. The
  fallback fixes the in-container bug where an unset/`"/"` `$HOME` made
  `Path.home()` write and read disagree. `import-pickle`, `auth status`, and
  every live command use the SAME resolved dir.
- **Stale-auth auto-recovery (bounded).** `load_session`/`test_loggedin`: one
  `login()`, then if `test_loggedin` is False, **re-`load_cookie()` from disk and
  re-test** up to `STALE_RELOAD_ATTEMPTS` (=3) with a short sleep — recovers the
  HA rotation race. NEVER re-`login()` in a loop (repeated logins throttle
  Amazon's auth — observed live).
- **Python version, precisely:** a fresh proxy/scripted login pickles its
  cookie on the user's own Python and unpickles fine on that same Python — so
  **3.10+ is enough for normal use**. The `partitioned` Morsel attr (added to
  `http.cookies.Morsel` in 3.14) only breaks unpickle when reading a pickle
  written on a *newer* Python — i.e. **importing HA's 3.14-written pickle on
  ≤3.13** raises `CookieError: Invalid attribute 'partitioned'`. So 3.14 is
  required ONLY for `import-pickle` from a 3.14 source. CLI imports/tests run on
  any 3.10+.
- **Device-bound calls MUST go through `DeviceRef` — never hand alexapy the raw
  dict.** `AlexaAPI.get_devices()` returns plain JSON dicts, but every *instance*
  method dereferences its target as **attributes**: `self._device.device_serial_number`,
  `._device_type` (set_media/set_dnd_state/stop/bluetooth), `._device_family` +
  `._cluster_members` (`process_targets`, WHA fan-out), `._locale`
  (send_announcement/send_tts/run_routine). alexapy is written against Home
  Assistant's `AlexaClient` entity object, which has them. Passing the dict raises
  `AttributeError` **from inside alexapy** — and its `_catch_all_exceptions`
  decorator only converts connection/login errors and `raise`s everything else, so
  it surfaces as a raw traceback, not a friendly message. `core/device_ref.py`
  does the translation (and refuses a record with no `serialNumber`, which cannot
  be addressed at all). `tests/test_device_ref.py` pins the attribute list, so a
  future alexapy change fails a unit test instead of a live call.
- **Volume is a fraction, not a percentage.** alexapy's `set_volume` multiplies by
  100, so it wants **0.0–1.0**. The CLI takes the human 0–100 and converts once in
  `media.normalize_volume` (which also rejects NaN/inf — they slip past a naive
  range check). Validate in the command *before* `_login` so a bad number fails
  identically with and without `--yes`.
- **`announce` vs `speak`.** `send_announcement` plays Alexa's announcement tone
  and honours `targets` (fan-out to all devices). `send_tts` does not chime, and
  alexapy documents its `targets` as **non-functional** — Amazon ignores it — so
  `speak` binds `AlexaAPI` to the requested device rather than passing targets.
- **Routine EDITS are not the only Alexa-app-only surface** — see the routines
  note below; media/transport, by contrast, is fully API-driven.
- **Mutations are dry-run-by-default + require `--yes`** (prune, delete, run,
  notifications add/delete, announce, speak, push, dnd, devices on/off/light,
  guard set, media *, run *, echos connect/disconnect, activity clear). Mirror
  this when adding commands, and **normalise/validate BEFORE `_login`** so bad
  input fails identically with and without `--yes`.
- **`run command` is the escape hatch.** `AlexaAPI.run_custom` sends literal text
  through Alexa's own parser, so anything Alexa understands by voice is reachable
  without a typed command. It answers OUT LOUD and returns **no payload** — the
  intended pairing is `run command … --yes` then `activity history`. In
  `sequences.py`: unknown *ids* (`Alexa.*`, raw soundbank ids) pass through
  because Amazon keeps adding them; unknown friendly **names** are refused
  locally with the alternatives, because the API answers an unknown sequence with
  a generic failure. `normalize_queue_delay` returns **`None`** for "unspecified"
  and the wrappers then OMIT the argument — alexapy's default differs per call
  (0 for text/skill, 1.5 for sound/sequence) and flattening it would change
  behaviour.
- **Activity: two feeds, on purpose.** `activity history` uses the privacy view
  (`/alexa-privacy/apd/rvh/customer-history-records`) because it is the only one
  returning the **transcript of both halves** of a turn; `activity records` keeps
  the legacy `/api/activities` feed for its per-activity **ids** (the delete
  source) and status. Timestamps are epoch **ms** rendered as tz-aware UTC (a
  naive `fromtimestamp` would re-read them in the host's zone). `--hours` is a
  server-side query **window**, `--device`/`--contains` are client-side filters.
  `clear_history` returning `False` means Amazon **refused at least one entry**
  (404, nothing to delete) — report the clear as partial, never as clean.
- **Bluetooth: connect ≠ pair, and disconnect is all-or-nothing.**
  `set_bluetooth(mac)` (`pair-sink`) only *connects* a sink that is **already
  paired**; the pairing handshake is Alexa-app/voice-only. Amazon answers
  `pair-sink` for an unknown address with a bare `200` and does nothing, so
  `bluetooth.connect` resolves the target against the Echo's own
  `pairedDeviceList` and refuses locally (listing what *is* paired) rather than
  posting into the void. **The `address` string Amazon reported is what gets
  posted** — `normalize_mac` exists only so `aa-bb-…`/`AABB…`/`AA:BB:…` compare
  equal when *finding* the pairing. `disconnect_bluetooth()` drops **every**
  connected sink (no per-sink endpoint), so the row says `all`.
- **`push` is the silent channel.** `send_mobilepush` /
  `send_dropin_notification` land in the Alexa **app**, not on a speaker — the
  right default for scripted/overnight notifications, where `announce`/`speak`
  would wake the house. Both are still *device-bound* (they ride `send_sequence`),
  so they resolve an Echo through `DeviceRef` like everything else.
- **applianceId → entity:** HA appliances encode the entity as `..._<domain>#<object_id>`.
  `appliances.parse_entity_id` splits domain at the last `_` before `#`; object_id
  (after `#`) may contain underscores. Only `manufacturerName=="Home Assistant"` is HA-sourced.
- **csrf header** required on every mutating raw call — `session.csrf_header(login)`
  pulls the `csrf` cookie off the authed aiohttp jar.
- **Never commit** the profile or cookie (gitignored — live Amazon session).
- **Device-groups = GraphQL**, not phoenix REST (`/api/phoenix/group` is dead — 401
  `'at' and 'ubid' values required`). Go through `AlexaAPI._static_request("post",
  login, "/nexus/v1/graphql", data={"query":..., "variables":...})` — it sets the
  correct nexus host/auth; do NOT hand-roll the host (the web host 401s for groups).
  Group id = `amzn1.alexa.endpointGroup.*`; member/endpoint id = `amzn1.alexa.endpoint.*`.
  Map HA entity→endpoint via the `endpoints` query (reuses `parse_entity_id`).
  **Two gotchas** (baked into `groups.py` + commented): (1) `memberDeviceIds` /
  `associatedUnitIds` are GraphQL `[String!]` — pass real Python lists so they
  serialize as JSON arrays; a lone `json.dumps`'d string is coerced to a 1-element
  list and the server **silently no-ops**. (2) Never send `associatedUnitIds` on
  **create** (BAD_REQUEST) — Alexa auto-associates the unit; create = friendlyName +
  memberDeviceIds only. Update uses `memberDeviceIdsUpdateOperation` ADD/REMOVE/REPLACE.
- **Canonical `endpoints` query = one source of truth** (in `endpoints.py`). It
  ties a device's three ids together: GraphQL **endpoint id** (`amzn1.alexa.endpoint.*`,
  used by groups + rename), **applianceId** (`legacyAppliance.applianceId`, used by
  phoenix DELETE; HA tail `_<domain>#<object_id>` decodes via `parse_entity_id`),
  and **display name** (`friendlyNameObject.value.text`). `manufacturerName=="Home
  Assistant"` ⇒ HA-sourced; anything else (Belkin/Tuya/…) is native (no HA entity →
  target it by display name).
- **Target resolution + ambiguity (rename / delete / groups --device).**
  `resolve_target` precedence: exact applianceId → exact endpoint id → exact
  display name → normalized display name. **A native + HA twin can share a name →
  >1 match → ABORT and list candidates** so the user disambiguates by id. The CLI
  helper `_resolve_one_or_abort` enforces this everywhere a name can resolve.
- **Rename = GraphQL `setEndpointFriendlyName`** (`input:{endpointId, friendlyName}`),
  by endpoint id (NOT applianceId). **Discover = raw `POST /api/phoenix/discovery`**
  (not GraphQL) on the web host with the csrf header → `200 {}`. Both dry-run+`--yes`.
- **Bulk rename = `--pattern 's/REGEX/REPL/[ig]'` or `--map <file>`.** `--pattern`
  applies a sed-style Python-`re` substitution (capture groups `\1`, flags `i`/`g`)
  to EVERY device's current name; the changed ones are the rename set. `--map` reads
  `current name => new name` (or `endpointId => new name`) lines (`#` comments).
  Both dry-run-by-default with a full `old -> new` preview table (the safety review
  for ~50 renames); no-ops (new==old) skipped; `--yes` executes (`apply_renames`
  captures per-entry errors so one bad name doesn't abort the batch). Single
  `devices rename <target> <new>` still works.
- **DACS rejects non-speakable rename names.** `setEndpointFriendlyName` validates
  through DACS and refuses hyphens / control chars → `"Invalid input. Invalid input
  from DACS"` (`errorCode BAD_REQUEST`). Proven: `"elt-k8s-1 Temperature"` refused,
  `"elt k8s 1 Temperature"` accepted. `speakable_name()` fixes it (hyphens→spaces,
  strip control chars e.g. a stray `\x05`, collapse whitespace); `--speakable`
  auto-applies it to all rename modes. Without `--speakable` the CLI pre-warns
  (`speakable_warning`) and `rename_endpoint` catches an actual DACS `BAD_REQUEST`
  (`is_dacs_error`) and re-raises a friendly `ValueError` suggestion, never the raw
  GraphQL blob.
- **Nested / child groups.** A group can contain other groups (the rollup pattern,
  e.g. "Downstairs" of room groups). `CreateDeviceGroupInput.childDeviceGroupIds:
  [String!]` and `UpdateDeviceGroupInput.childDeviceGroupIds: [String!]` +
  `childDeviceGroupIdsUpdateOperation: CollectionOperationOptions` (ADD/REMOVE/
  REPLACE) mirror the member fields. **SAME array gotcha — `childDeviceGroupIds`
  is a real `[String!]`; pass a Python list, never a `json.dumps`'d string (a lone
  string silently no-ops).** `--child-group "<name|id>"` on `groups create`/`add`/
  `remove`/`set` resolves by normalized group name → group id (`resolve_child_groups`,
  reusing `find_group`); the child-group field/op are OMITTED unless child ids are
  given, so member-only paths are unchanged. `groups list` shows each group's child
  groups (`childDeviceGroups{ id friendlyName{value{text}} }`).
- **Native devices re-sync — Alexa-only deletes don't stick.** A non-HA device
  (`manufacturerName != "Home Assistant"`) re-syncs from its cloud skill/bridge
  after deletion (proven: Tuya re-synced from Smart Life, Philips Hue from the
  bridge). `devices delete` **warns** (`native_delete_warning`) when a target is
  native, telling the user to remove it at source. `--verify` triggers a discovery,
  waits ~12s, re-queries `endpoints`, and reports which just-deleted devices
  **re-appeared** (`reappeared_after_delete`, by applianceId or normalized name) so
  the user knows which need source-side removal.
- **Reachability column SKIPPED (deliberate).** The `Endpoint` GraphQL type has
  `connections` / `endpointReports` / `enablement`. Only `enablement` introspected
  as a clean, consistently-present scalar enum (ENABLED/…), so `devices list`
  surfaces it as `enabled`; `connections`/`endpointReports` nested shapes were NOT
  consistently available on the live account, so a true online/reachability column
  was omitted rather than ship a flaky one.
- **Routine EDITS are not API-supported — Alexa-app-only.** Amazon hard-refuses:
  `updateAutomation` → "not supported for automation type: ROUTINE";
  `batchUpdateAutomations` needs an opaque scripted-source blob the read API won't
  return; REST `PUT` 404s. `routines list`/`run` work (list now includes a
  best-effort action-target summary); there is intentionally NO edit mutation.

## Verified
Live read-only validation (2026-06-15, amazon.co.uk account, HA cookie reused):
`auth status` → logged_in=true; the canonical `endpoints` query → 161 endpoints /
91 HA-sourced / 70 native. Resolvers proven against real data: `resolve_by_entity`
(`light.kitchen_big`→endpoint), `resolve_target` by endpoint id + applianceId,
`resolve_by_name` of a **native** plug (`JNG-PLUG-1`), `find_duplicates` → 9 clusters
incl. native+HA twins (`Patio Light 1/5`), `device_rows` filters (70 native-only,
13 Tuya), `enablement` consistently ENABLED. **No mutations executed** —
rename/delete/discover/group-writes are built but user-gated (`--yes`).

**Not live-validated (2026-08-11):** the device-bound surface (announce / speak /
push / dnd / routines run / all of `media` / all of `run` / `echos
connect`-`disconnect`) has never had a mutation executed against a real account,
and neither have the smart-home writes (`devices on/off/light`, `guard set`) or
`activity clear`. That is precisely how the raw-dict-vs-`DeviceRef` bug survived:
it is invisible to a read-only check and only fires on the first real
device-bound call. The adapter is covered by unit tests asserting alexapy's
attribute contract, but the next person with an account should run one `media
pause --yes` to close the loop, and ideally `run command "what's the time" --yes`
followed by `activity history` (which validates the behaviour surface *and* the
history read in one pass).

**Assumptions worth checking live, per surface** (each is inferred from alexapy's
implementation + Amazon's documented shapes, not observed):
- `echos connect` — that `pair-sink` needs the `pairedDeviceList[].address`
  string verbatim, and that a not-yet-paired address really is a silent no-op.
- `push` — that `send_mobilepush` reaches the app with no speaker output at all,
  and how Amazon renders a custom `title`.
- `activity clear` — that a partial refusal surfaces as alexapy returning
  `False` (rather than raising) on the 404 path.
