---
name: cli-anything-alexa
description: Manage Amazon Alexa from the shell — smart-home appliances (list/prune/delete), groups, routines, alarms/timers/reminders, announce, and do-not-disturb — over the unofficial Alexa web API via alexapy. Logs in via a browser-proxy flow (no Home Assistant needed; captcha/2FA handled by Amazon's own pages) and caches a local cookie so there's no per-call MFA. Use when an agent needs to inspect or tidy what Alexa knows without the app.
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
- `devices delete [<applianceId...>] [--entity ha.x] [--name "<display>"]` —
  delete by id, HA entity, or display name (`--yes`). Name resolving to >1 device
  (native+HA twin) aborts and lists matches.
- `devices rename <target> <new-name>` (`--yes`) — `setEndpointFriendlyName`.
  target = applianceId / endpoint id / display name (exact → normalized);
  ambiguous name aborts + lists candidates.
- `devices duplicates` — pairs/clusters where a display name is exposed twice
  (flags native+HA twins). Reports only; you choose which to `devices delete`.
- `discover` (`--yes`) — trigger a smart-home discovery sweep
  (`POST /api/phoenix/discovery`).
- `echos list` — physical Echo devices.
- `groups list` — smart-home device-groups (rooms): name, id, member count/names.
- `groups create <name> [--entity ha.x ...] [--endpoint amzn1... ...]` (`--yes`).
- `groups add|remove|set <group(name|id)> [--entity ...] [--endpoint ...] [--device "<name>"]` (`--yes`) —
  ADD/REMOVE delta, `set` REPLACEs the whole member set. **`--device` targets
  native/non-HA devices by display name** (e.g. Tasmota-Wemo plugs with no HA entity).
- `groups delete <group(name|id)>` (`--yes`).
  Groups use the modern GraphQL `/nexus/v1/graphql` API (the legacy phoenix
  group REST is dead). **Gotchas:** member id lists must be real JSON arrays
  (a lone string silently no-ops); never send `associatedUnitIds` on create
  (BAD_REQUEST — Alexa auto-associates the unit). Both handled internally.
- `routines list` — routines with trigger utterance + best-effort action-target
  summary. `routines run <name|id>` (`--yes`) — trigger via behaviors/preview.
  **Do NOT script edits to an existing routine — brittle + destructive.**
  `updateAutomation` is refused and REST PUT 404s, but `batchUpdateAutomations`
  *does* mutate a `ROUTINE` and a malformed attempt partially applies (can strip
  its action), while the v2 read goes stale (can't verify). Edit routines in the
  Alexa app.
- `notifications list` / `add-reminder` / `add-alarm` / `add-timer` / `delete` (`--yes`).
- `announce <text> [--device ...]` (`--yes`) — TTS on all/one Echo.
- `dnd <device> on|off` (`--yes`).

## Safety
All mutating commands are **dry-run-by-default and require `--yes`**. Unofficial
API — endpoints may break; heavy use can trip Amazon's bot defences. The cookie
and profile live in `~/.config/cli-anything-alexa/` and are never committed.

## How it maps to HA
Each HA-sourced appliance's `applianceId` ends in `..._<domain>#<object_id>`,
decoded back to `<domain>.<object_id>`. `manufacturerName == "Home Assistant"`
marks HA-sourced. The HA `alexa: smart_home:` filter over-exposes entities;
`devices prune` is the scripted cleanup against your intended whitelist.
