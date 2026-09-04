---
name: cli-anything-alexa
description: Manage Amazon Alexa from the shell — smart-home appliances (list/prune/delete), groups, routines, alarms/timers/reminders, Echo media playback, announce/speak, and do-not-disturb — over the unofficial Alexa web API via alexapy. Logs in via a browser-proxy flow (no Home Assistant needed; captcha/2FA handled by Amazon's own pages) and caches a local cookie so there's no per-call MFA. Use when an agent needs to inspect or tidy what Alexa knows without the app.
---

# cli-anything-alexa

CLI over the **unofficial Alexa web API** (`alexapy`). Stateless thin client.
Every command takes `--json`.

## Setup / auth
- Region matters: `--url amazon.co.uk` (default) or `amazon.com`.
- **Browser-proxy login (primary, no HA):**
  ```
  cli-anything-alexa auth login        # prints a local URL; open it, sign in to Amazon
  cli-anything-alexa auth status       # logged_in: true
  ```
  Captcha/2FA are handled on Amazon's own pages. From a headless box, add
  `--host 0.0.0.0` (or SSH-tunnel the port, default 3001).
- **Headless/CI:** `auth login --email you@x.com --password ... [--otp-secret <base32 TOTP>]`
  (scripted; Amazon may captcha-block — fall back to the proxy flow).
- **Reuse HA's LIVE cookie (recommended for HA reuse):** HA's `alexa_media`
  rotates the cookie constantly, so read it IN PLACE rather than copying a
  snapshot that goes stale within seconds:
  ```
  cli-anything-alexa --email you@x.com --cookie-dir /config auth status
  cli-anything-alexa --email you@x.com --cookie-dir /config devices list --json
  ```
  `--cookie-dir <dir>` (env `CLI_ALEXA_COOKIE_DIR`) reads/writes
  `<dir>/.storage/alexa_media.<email>.pickle` (HA's layout) — `/config` ⇒ HA's
  live pickle. The CLI auto-recovers the rotation race (re-reads + retries a
  couple of times, no login storm). Cookie-dir resolves: `--cookie-dir` > env >
  valid `$HOME/.config/cli-anything-alexa` > `/tmp/cli-anything-alexa` fallback
  (the fallback keeps write==read when `$HOME` is unset/`/` in containers).
- **Reuse HA's cookie as a one-off snapshot:**
  `auth import-pickle /config/.storage/alexa_media.you@x.com.pickle --email you@x.com`
  — copies once; **goes stale** if HA keeps rotating the cookie. Prefer
  `--cookie-dir` for active HA reuse.
- **Python 3.10+** is enough for a fresh login. **3.14 is needed only to
  `import-pickle` a 3.14-written pickle** (HA's): the cookie `partitioned` attr
  is unpicklable on ≤3.13. A login you perform yourself is unaffected.

## Commands
- `devices list [--ha-only | --native-only] [--manufacturer <substr>]` — devices
  with `manufacturer` + native-vs-HA `source` + `enabled`; HA rows show `entity_id`.
  (No reachability/online column — not exposed cleanly by the API; only the
  `enablement` enum is, surfaced as `enabled`.)
- `devices prune --whitelist <file>` — delete HA appliances whose entity isn't
  whitelisted. **Dry-run by default**; add `--no-dry-run --yes` to execute.
  Whitelist = one entity id per line, `#` comments allowed. Native (Hue/Wemo)
  appliances are never touched.
- `devices delete [<applianceId...>] [--entity ha.x] [--name "<display>"] [--verify]` —
  delete by id, HA entity, or display name (`--yes`). Name resolving to >1 device
  (native+HA twin) aborts and lists matches. **Native (non-HA) devices re-sync
  from their cloud skill/bridge** (proven: Tuya re-syncs from Smart Life, Philips
  Hue from the bridge), so deleting in Alexa alone may not stick — you're
  **warned** when a target isn't `manufacturerName=="Home Assistant"`, and
  `--verify` re-runs discovery, waits ~12s, re-queries, and reports which
  just-deleted devices **re-appeared** (remove those at source).
- `devices rename <target> <new-name>` (`--yes`) — `setEndpointFriendlyName`.
  target = applianceId / endpoint id / display name (exact → normalized);
  ambiguous name aborts + lists candidates.
  - **Bulk `--pattern 's/REGEX/REPL/[ig]'`** — apply a sed-style Python-`re`
    substitution to **every** device's current name; the ones that change form
    the rename set (capture groups `\1`, e.g. `'s/^Spots - (.*)/\1 Spots/'`,
    `'s/^TH - //'`). Full `old -> new` preview table; **dry-run unless `--yes`**.
  - **Bulk `--map <file>`** — lines of `current name => new name` (or
    `endpointId => new name`); `#` comments allowed.
  - **DACS validation (all rename modes):** Amazon's rename API validates via
    DACS and **rejects hyphens / non-speakable names** (`"Invalid input. Invalid
    input from DACS"` / `BAD_REQUEST`; proven: `"elt-k8s-1 Temperature"` refused,
    `"elt k8s 1 Temperature"` accepted). `--speakable` auto-fixes (hyphens→spaces,
    strip control chars, collapse whitespace); without it, non-speakable names
    are pre-warned and an actual DACS rejection is caught and shown as a friendly
    suggestion instead of the raw GraphQL error.
- `devices duplicates` — pairs/clusters where a display name is exposed twice
  (flags native+HA twins). Reports only; you choose which to `devices delete`.
- `discover` (`--yes`) — trigger a smart-home discovery sweep
  (`POST /api/phoenix/discovery`).
- `devices state [<target>...] [--all]` — **read** live smart-home state (power,
  brightness, colour, colour temperature…) over `/api/phoenix/state`. Targets
  resolve like `rename`. Read-only, no `--yes`.
- `devices on|off [<target>...] [--all]` (`--yes`) — power appliances.
- `devices light <target> [--on|--off] [--brightness 0-100] [--color <name>] [--color-temperature <name>]` (`--yes`) —
  colour names are a **closed snake_case vocabulary** (Alexa rejects anything
  else with a generic error, so the CLI validates locally and lists the palette).
- `guard status` / `guard set away|home` (`--yes`) — Alexa Guard arm state.
- `echos list` — physical Echo devices (the targets for announce/speak/media/dnd/routines).
- `echos bluetooth` — bluetooth devices paired to each Echo, account-wide (name, MAC, connected).
- `echos pairings [<device>]` — paired sinks for ONE Echo (default: first online),
  including the `address` string `echos connect` needs.
- `echos connect <name|mac> [--device ...]` (`--yes`) — connect an **already
  paired** sink (`pair-sink`). **It does not pair**: the initial handshake is
  Alexa-app/voice-only. A target not in `echos pairings` is refused locally with
  the paired list, because Amazon answers an unknown address with a bare `200`
  and does nothing. Any MAC spelling matches; the address Amazon reported is what
  is posted. An ambiguous friendly name aborts and lists the addresses.
- `echos disconnect [--device ...]` (`--yes`) — **all-or-nothing**: Amazon has no
  per-sink disconnect, so every connected sink on that Echo is dropped.
- `echos wake-words` — the configured wake word per Echo.
- `echos dnd` — **read** every Echo's do-not-disturb state (the `dnd` command writes it).
- `echos preferences [<device>]` — per-Echo `timeZoneId` / locale / temperature +
  distance units / postal code. The timezone here is the clock a notification
  reschedule/snooze writes an alarm's local wall-clock fields in.
- `echos wifi [<device>]` — one Echo's wifi details (SSID, signal, security,
  MAC/IP); default target is the first online Echo, like the rest of `echos`.
- `auth whoami` — who the saved cookie is actually logged in as (customer id,
  name, email, Prime Music). `auth status` validates the **cookie**; `whoami`
  validates that it still buys an **account**, and exits non-zero if not.
- `kids profiles` — Amazon Kids child profiles in the household (name, age, `directedId`).
- `kids status [<device>]` — Amazon Kids state per Echo; no argument = every Echo
  (two requests each), a name/serial = just that one. A **blank/null `kids`** means
  the state could not be read — that is NOT the same as `off`.
- `kids enable <device> --child <name|directedId>` (`--yes`) — assign an Echo to a
  child profile, turning Amazon Kids on. The target Echo is **required** (no
  first-online default) because kids mode changes what the speaker will do.
- `kids disable <device>` (`--yes`) — unassign the Echo, turning Amazon Kids off.
  Both writes **verify by re-reading**: alexapy returns `None` whether the call
  worked or was rejected, and the assign uses the parent-dashboard host's own
  `ft-panda-csrf-token` (missing → logged at debug only, so a rejection is
  silent). Trust the `ok` field, which comes from the re-read. An unknown child is
  refused locally with the known profiles; siblings sharing a first name abort
  with their `directedId`s.
- `groups list` — smart-home device-groups (rooms): name, id, member count/names,
  plus child-group count/names (nested groups).
- `groups create <name> [--entity ha.x ...] [--endpoint amzn1... ...] [--child-group "<name|id>" ...]` (`--yes`).
- `groups add|remove|set <group(name|id)> [--entity ...] [--endpoint ...] [--device "<name>"] [--child-group "<name|id>"]` (`--yes`) —
  ADD/REMOVE delta, `set` REPLACEs the whole set. **`--device` targets
  native/non-HA devices by display name** (e.g. Tasmota-Wemo plugs with no HA entity).
  **`--child-group` nests another group as a child — the rollup pattern** (e.g. a
  "Downstairs" group containing room groups). Resolved by normalized group name →
  group id (`amzn1.alexa.endpointGroup.*`).
- `groups delete <group(name|id)>` (`--yes`).
  Groups use the modern GraphQL `/nexus/v1/graphql` API (the legacy phoenix
  group REST is dead). **Gotchas:** member/child id lists are GraphQL `[String!]`
  and must be real JSON arrays (a lone string silently no-ops); never send
  `associatedUnitIds` on create (BAD_REQUEST — Alexa auto-associates the unit).
  Child groups use `childDeviceGroupIds` + `childDeviceGroupIdsUpdateOperation`
  (ADD/REMOVE/REPLACE), mirroring the member fields. All handled internally.
- `routines list` — routines with trigger utterance + best-effort action-target
  summary. `routines run <name|id>` (`--yes`) — trigger via behaviors/preview.
  **Do NOT script edits to an existing routine — brittle + destructive.**
  `updateAutomation` is refused and REST PUT 404s, but `batchUpdateAutomations`
  *does* mutate a `ROUTINE` and a malformed attempt partially applies (can strip
  its action), while the v2 read goes stale (can't verify). Edit routines in the
  Alexa app.
- `notifications list` / `show <id|label>` / `add-reminder` / `add-alarm` /
  `add-timer` / `delete` (`--yes`). `add-reminder`/`add-alarm` also take
  `--repeat daily|weekdays|weekends|weekly [--days Mon,Thu]` to create a
  recurring one (weekly names its days via `rRuleData.byWeekDays`).
- `notifications pause|resume <id|label>` (`--yes`) — `status: OFF`/`ON`; a
  paused alarm keeps its schedule, unlike a delete.
- `notifications reschedule <id|label> --in N|--at MS` and
  `notifications snooze <id|label> [--minutes N]` (`--yes`, default 9 min).
  **An edit is a whole-record PUT**, built from the record Amazon returned — a
  minimal hand-rolled body is accepted silently and drops recurrence/device.
  A reminder also fires off `originalDate`/`originalTime` (local wall clock, in
  the Echo's own `timeZoneId` from `echos preferences`), so those move with
  `alarmTime`; the reported `tz` says which clock was used. **Timers cannot be
  rescheduled or snoozed** (no `alarmTime` — delete and recreate). The PUT
  cannot report success, so every edit re-reads and sets `ok` from what Amazon
  holds; `ok: null` means the verify read was throttled, NOT that it failed.
- `notifications repeat <id|label> daily|weekdays|weekends|weekly|none
  [--days Mon,Thu]` (`--yes`) — set or **clear** a recurrence
  (`recurringPattern` + `rRuleData.byWeekDays`; `none` removes both).
  Timers can't repeat.
  Targets resolve by id or label, and an ambiguous label aborts with the ids.
- `media status [<device>]` — what an Echo is playing: state, title, artist,
  album, provider, volume, progress. Read-only, no `--yes`.
- `media play|pause|next|previous|forward|rewind [<device>]` (`--yes`) — transport.
- `media stop [<device>] [--all]` (`--yes`) — stop one Echo, or the whole house.
- `media volume [<device>] --level 0-100` (`--yes`) — **give a percentage**; the
  CLI converts to the 0.0-1.0 fraction alexapy wants.
- `media shuffle|repeat [<device>] --state on|off` (`--yes`).
- `media play-music "<phrase>" [--device ...] [--provider AMAZON_MUSIC|SPOTIFY|TUNEIN|...]` (`--yes`).
  **DEVICE is optional everywhere in `media` — omitted, it targets the first
  ONLINE Echo**, same as `announce`/`routines run`.
- `announce <text> [--device ...]` (`--yes`) — announcement **with Alexa's chime**,
  fans out to all devices by default.
- `speak <text> [--device ...]` (`--yes`) — plain TTS on ONE speaker, **no chime**.
  Prefer this for a quiet notification; prefer `announce` for house-wide.
- `push <text> [--title ...] [--device ...] [--dropin]` (`--yes`) — notification
  to the **Alexa app**, **silent on the speakers** — the right channel for a
  script running at 3am. `--dropin` sends the drop-in variant (offers to drop in
  on the resolved Echo). Still device-bound (it rides the behaviours API), so an
  Echo is resolved even though nothing plays on it.
- `dnd <device> on|off` (`--yes`) — write DND; `echos dnd` reads it back.
- `run command "<utterance>" [--device ...] [--queue-delay N]` (`--yes`) — run
  literal text **through Alexa's own parser**. Highest-leverage call on the
  account: anything Alexa understands by voice — skills, devices with no typed
  command here — is reachable. Alexa answers OUT LOUD and there is **no response
  payload**; read back what happened with `activity history`.
- `run sequence <name|Alexa.*.Play> [--device ...]` (`--yes`) — built-in behaviour
  (weather / traffic / flash-briefing / good-morning / good-night / joke / story /
  calendar-…). `run sound <alias|soundbank id>` (`--yes`) — Alexa soundbank.
  `run skill amzn1.ask.skill.<uuid>` (`--yes`). `run catalog [--kind ...]` lists
  the sequences + sound aliases and needs **no account**.
  **Unknown ids pass through, unknown friendly names are refused locally** (the
  API answers an unknown sequence with a generic failure that says nothing).
  `--queue-delay` batches everything issued in that window into ONE behaviour
  node; omit it and alexapy's per-call default (0 text/skill, 1.5 sound/sequence)
  applies.
- `activity history [--limit N] [--hours N] [--device ...] [--contains ...] [--include-noise]` —
  recent voice turns: **transcript + Alexa's reply**, from the privacy view
  (`customer-history-records`) — the only feed carrying both halves. `--hours` is
  a real query window. `DEVICE_ARBITRATION` rows (multi-Echo wake-word races) are
  dropped unless `--include-noise`. Read-only.
- `activity records [--limit N]` — the legacy `/api/activities` feed; keeps the
  per-activity **ids** and status the privacy view drops.
- `activity last [--limit N]` — the last Echo that answered and what it was asked.
- `activity clear [--items N]` (`--yes`) — **irreversible** deletion of recent
  voice recordings. When Amazon refuses an entry (404, nothing to delete) the
  result reports the clear as **partial**, never as clean.

## Safety
All mutating commands are **dry-run-by-default and require `--yes`**. Unofficial
API — endpoints may break; heavy use can trip Amazon's bot defences. The cookie
and profile live in `~/.config/cli-anything-alexa/` and are never committed.

## How it maps to HA
Each HA-sourced appliance's `applianceId` ends in `..._<domain>#<object_id>`,
decoded back to `<domain>.<object_id>`. `manufacturerName == "Home Assistant"`
marks HA-sourced. The HA `alexa: smart_home:` filter over-exposes entities;
`devices prune` is the scripted cleanup against your intended whitelist.
