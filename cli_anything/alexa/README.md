# cli-anything-alexa

Programmatic management of **Amazon Alexa** from the command line, built on
the unofficial Alexa web API (the same one the Alexa app uses) via the
[`alexapy`](https://pypi.org/project/alexapy/) library. Sibling of
`cli-anything-homeassistant` and `cli-anything-zigbee2mqtt` — same Click +
REPL + `--json` conventions.

Manage smart-home **appliances** (incl. pruning Home-Assistant-sourced
orphans), **groups**, **routines**, **alarms/timers/reminders**, plus
**announce** and **do-not-disturb** on your Echo devices.

> **Unofficial API caveat.** Amazon publishes no official Alexa device-management
> API. This drives the private web endpoints the app uses. They can change or
> break without notice, and aggressive use may trip Amazon's bot defences.
> Auth reuses an existing cookie session (no credentials stored beyond the
> cookie pickle). Mutating commands are dry-run-by-default and require `--yes`.

## Install

```bash
pip install -e .            # exposes the `cli-anything-alexa` console script
```

Requires Python **3.10+** for the CLI itself. **Live calls additionally need
Python 3.14+** at runtime: the saved cookie pickle carries the `partitioned`
cookie attribute that only `http.cookies.Morsel` on Python 3.14+ understands
(older Pythons raise `KeyError: 'partitioned'` when `alexapy` reads the
cookie). The Home Assistant container ships a compatible Python — running
there, or under a 3.14 venv, is the supported path.

## Auth

There are two ways to get an authenticated session. Both avoid per-call MFA.

### (a) Reuse Home Assistant's cookie (recommended)

If you already run the `alexa_media_player` HA integration, import its cookie:

```bash
cli-anything-alexa --email you@example.com config save
cli-anything-alexa auth import-pickle \
  /config/.storage/alexa_media.you@example.com.pickle --email you@example.com
cli-anything-alexa auth status            # -> {"email": ..., "logged_in": true}
```

`import-pickle` copies the pickle into `~/.config/cli-anything-alexa/` under
the `alexa_media.<email>.pickle` name `alexapy` expects, then validates it.

### (b) Fresh login

```bash
cli-anything-alexa auth login --email you@example.com
# prompts for password, then OTP/2FA if required
```

Amazon often gates fresh logins behind a captcha — importing HA's cookie is the
reliable route when available.

The account **region** matters: a `amazon.co.uk` account uses base
`https://alexa.amazon.co.uk`. Set it with `--url amazon.co.uk` (the default) or
`--url amazon.com`, and persist via `config save`.

## Commands

Every command supports a global `--json` flag for machine-readable output.

| Command | Description |
| --- | --- |
| `auth import-pickle <path>` | Import an existing alexapy cookie (e.g. HA's) into the local config dir |
| `auth login` | Fresh email/password/OTP login, persisting the cookie |
| `auth status` | Validate the saved cookie (`test_loggedin`) |
| `config show` / `config save` | Show / persist the connection profile (email + region) |
| `devices list [--ha-only]` | List smart-home appliances (each HA appliance shows its mapped entity id) |
| `devices prune --whitelist <file>` | Delete HA-sourced appliances whose entity isn't whitelisted (dry-run default; `--no-dry-run --yes` to execute) |
| `devices delete <applianceId...>` | Delete appliances by id (`--yes` to execute) |
| `echos list` | List the physical Echo devices on the account |
| `groups list` | List Alexa smart-home device-groups (rooms): name, id, member count/names |
| `groups create <name> [--entity ... \| --endpoint ...]` | Create a device-group with the given members (`--yes` to execute) |
| `groups add <group> [--entity ... \| --endpoint ...]` | Add members to a group by name/id (`--yes`) |
| `groups remove <group> [--entity ... \| --endpoint ...]` | Remove members from a group by name/id (`--yes`) |
| `groups set <group> [--entity ... \| --endpoint ...]` | Replace a group's entire member set (`--yes`) |
| `groups delete <group>` | Delete a device-group by name/id (`--yes` to execute) |
| `routines list` | List Alexa routines (behaviors) |
| `routines run <name\|id>` | Trigger a routine via `behaviors/preview` (`--yes` to execute) |
| `notifications list` | List alarms / timers / reminders |
| `notifications add-reminder <label> --device ... [--in N \| --at MS]` | Create a reminder (`--yes` to execute) |
| `notifications add-alarm --device ... [--in N \| --at MS]` | Create an alarm (`--yes` to execute) |
| `notifications add-timer --device ... --duration N` | Create a timer (`--yes` to execute) |
| `notifications delete <id>` | Delete a notification (`--yes` to execute) |
| `announce <text> [--device ...]` | Speak an announcement on all (or one) Echo (`--yes` to execute) |
| `dnd <device> on\|off` | Toggle do-not-disturb on a device (`--yes` to execute) |
| `repl` | Interactive shell (default when no subcommand) |

### Prune housekeeping

The `devices prune` flow is the scripted version of the manual orphan-cleanup:
HA's `alexa: smart_home:` filter over-exposes entities, creating hundreds of
appliances. Maintain a whitelist of entity ids you actually want exposed (one
per line, `#` comments allowed) and prune the rest:

```bash
cli-anything-alexa devices prune --whitelist exposed-entities.txt          # preview
cli-anything-alexa devices prune --whitelist exposed-entities.txt --no-dry-run --yes
```

Only `manufacturerName == "Home Assistant"` appliances are candidates; native
Hue/Wemo/Tuya appliances are never touched.

### Device-groups (rooms)

`groups` manages Alexa **device-groups** (the "rooms" / groups you see in the
app) over the modern **GraphQL** API at `/nexus/v1/graphql` (the legacy
`/api/phoenix/group` REST endpoint is dead — it hard-401s). Members are
addressed either by Alexa endpoint id (`amzn1.alexa.endpoint.*`) or, more
conveniently, by Home Assistant `--entity` id, which is resolved to its
endpoint via the `endpoints` query (the same `..._<domain>#<object_id>` tail
parse used for appliances).

```bash
cli-anything-alexa groups list
cli-anything-alexa groups create "Den" --entity light.den_lamp --entity media_player.den_tv   # preview
cli-anything-alexa groups create "Den" --entity light.den_lamp --yes                           # execute
cli-anything-alexa groups add "Den" --entity switch.den_fan --yes      # ADD delta
cli-anything-alexa groups remove "Den" --entity light.den_lamp --yes   # REMOVE delta
cli-anything-alexa groups set "Den" --entity light.den_lamp --yes      # REPLACE whole member set
cli-anything-alexa groups delete "Den" --yes
```

Groups are looked up by id or by friendly name (case/space/punctuation
insensitive). Two API gotchas are handled internally and worth knowing:

- **Member id lists are GraphQL `[String!]` arrays.** They must serialize as
  real JSON arrays. Passing a single `json.dumps`'d string makes GraphQL
  coerce it into a 1-element list and the server **silently no-ops** (no error,
  nothing changes). The variables builders pass real Python lists.
- **`create` must not send `associatedUnitIds`** — doing so triggers
  `BAD_REQUEST`. Alexa auto-associates the unit from the member devices, so
  create takes `friendlyName` + `memberDeviceIds` only.

## Whitelist file format

```
# lights + switches I expose to Alexa
light.kitchen_big
switch.barista_machine_power   # inline comments allowed
sensor.master_bedroom_sensor_temperature
```

## Config / profile

`~/.config/cli-anything-alexa/config.json` (mode 0600) stores only the account
email and region. The cookie pickle sits alongside it as
`alexa_media.<email>.pickle`. **Neither is ever committed.** Per-key env
overrides: `CLI_ALEXA_EMAIL`, `CLI_ALEXA_URL`.

## Tests

```bash
pip install -e '.[test]'
python3 -m pytest tests/ -v
```

The unit tests cover the **pure logic** — appliance-id → entity parsing,
whitelist filtering / prune planning, table formatting, the notification
payload builders, and the device-group GraphQL variables builders /
name-normalization / lookup / entity→endpoint resolution — with no `alexapy`
dependency and no live account.
