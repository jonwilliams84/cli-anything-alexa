---
name: cli-anything-alexa
description: Manage Amazon Alexa from the shell — smart-home appliances (list/prune/delete), groups, routines, alarms/timers/reminders, announce, and do-not-disturb — over the unofficial Alexa web API via alexapy. Reuses an existing cookie session (e.g. Home Assistant's alexa_media pickle) so there's no per-call MFA. Use when an agent needs to inspect or tidy what Alexa knows without the app.
---

# cli-anything-alexa

CLI over the **unofficial Alexa web API** (`alexapy`). Stateless thin client.
Every command takes `--json`.

## Setup / auth
- Region matters: `--url amazon.co.uk` (default) or `amazon.com`.
- Reuse Home Assistant's cookie (no MFA):
  ```
  cli-anything-alexa --email you@x.com config save
  cli-anything-alexa auth import-pickle /config/.storage/alexa_media.you@x.com.pickle --email you@x.com
  cli-anything-alexa auth status        # logged_in: true
  ```
- Or `auth login --email you@x.com` (password + OTP; captcha-prone).
- **Live calls need Python 3.14+** (cookie `partitioned` attr `KeyError`s on ≤3.13).
  The Home Assistant container's Python works.

## Commands
- `devices list [--ha-only]` — appliances; HA-sourced rows show `entity_id`.
- `devices prune --whitelist <file>` — delete HA appliances whose entity isn't
  whitelisted. **Dry-run by default**; add `--no-dry-run --yes` to execute.
  Whitelist = one entity id per line, `#` comments allowed. Native (Hue/Wemo)
  appliances are never touched.
- `devices delete <applianceId...>` — delete by id (`--yes`).
- `echos list` — physical Echo devices.
- `groups list` — smart-home groups (create/delete = TODO).
- `routines list` / `routines run <name|id>` (`--yes`) — trigger via behaviors/preview.
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
