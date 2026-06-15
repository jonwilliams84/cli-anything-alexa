# cli-anything-alexa

Manage **Amazon Alexa** from the command line — smart-home appliances, groups
(rooms), routines, alarms/timers/reminders, announcements and do-not-disturb —
built on the unofficial Alexa web API (the same private endpoints the Alexa app
uses) via the [`alexapy`](https://pypi.org/project/alexapy/) library.

A `click`-based CLI plus an interactive REPL. Every command supports `--json`
for machine-readable output. Sibling of
[`cli-anything-homeassistant`](https://github.com/jonwilliams84/cli-anything-homeassistant)
and `cli-anything-zigbee2mqtt` — same Click + REPL + `--json` conventions, and
it pairs naturally with `cli-anything-homeassistant` if you expose HA entities
to Alexa.

> **Unofficial API caveat.** Amazon publishes no official Alexa
> device-management API. This drives the private web endpoints the app uses.
> They can change or break without notice, and aggressive use may trip Amazon's
> bot defences. No credentials are stored — only a session cookie, locally, in
> `~/.config/cli-anything-alexa/` (never committed). All mutating commands are
> **dry-run-by-default** and require `--yes`.

## Quick start (no Home Assistant needed)

```bash
pip install -e .                       # installs the `cli-anything-alexa` command
cli-anything-alexa auth login          # guided browser login (see below)
cli-anything-alexa devices list        # you're in
```

That's it. `auth login` walks you through a browser sign-in — captcha and 2FA
included — and saves the session locally. Requires **Python 3.10+** (see
[Python version](#python-version) for the one edge case that needs 3.14).

## Auth

### 1. Guided browser login — `auth login` (recommended)

The default flow needs no Home Assistant and handles captcha / 2FA natively,
because **you complete Amazon's own login pages in a browser**. It works exactly
like the Home Assistant `alexa_media` integration's setup: a tiny local web
proxy stands between your browser and Amazon, captures the session on success,
and saves the cookie.

```bash
cli-anything-alexa auth login
```

What it does:

```
Amazon account email: you@example.com
Account region host [amazon.co.uk]:

Browser login — three steps:
  1. Open this URL in a browser:  http://127.0.0.1:3001
  2. Sign in to Amazon as you normally would (captcha / 2FA
     are handled by Amazon's own pages).
  3. When it says you can close the window, you are done.

Waiting for login to complete... (Ctrl-C to cancel)
Logged in as you@example.com (amazon.co.uk). You're all set — try `cli-anything-alexa devices list`.
```

- Open the printed URL **in a browser on the same machine** (the proxy binds
  `127.0.0.1` by default). On a headless box you SSH into, either tunnel the
  port (`ssh -L 3001:127.0.0.1:3001 host`) or run with `--host 0.0.0.0` and
  open `http://<that-host>:3001` from your laptop.
- Pick your **region** when prompted (`amazon.co.uk`, `amazon.com`,
  `amazon.de`, …). It's persisted, so you only do this once.
- Flags: `--email`, `--url <region>`, `--host`, `--port` (default `3001`),
  `--timeout` (seconds to wait, default 600).

### 2. Scripted login (headless / CI) — `auth login --password ...`

For automation where no browser is available. Amazon frequently captcha-blocks
this; when it does, fall back to the proxy flow.

```bash
# Interactive 2FA prompt:
cli-anything-alexa auth login --email you@example.com --password 'secret'

# Fully non-interactive (TOTP base32 secret for 2FA):
cli-anything-alexa auth login --email you@example.com \
  --password 'secret' --otp-secret 'JBSWY3DPEHPK3PXP'
```

Passing `--password` switches to the scripted path. If Amazon returns a
captcha, the command tells you to use the proxy flow instead.

### 3. Import an existing Home Assistant cookie — `auth import-pickle`

A convenience if you already run the HA `alexa_media` integration — reuse its
cookie instead of logging in again:

```bash
cli-anything-alexa auth import-pickle \
  /config/.storage/alexa_media.you@example.com.pickle --email you@example.com
cli-anything-alexa auth status            # -> {"email": ..., "logged_in": true}
```

> **Heads-up:** a pickle written by a *newer* Python can't be read by an older
> one — see [Python version](#python-version). If HA runs Python 3.14, import
> it on Python 3.14 (or just use the proxy login instead).

### Checking / re-authenticating

```bash
cli-anything-alexa auth status            # validates the saved cookie
```

If a cookie expires, any command fails with a friendly message pointing you
back at `auth login`.

## Python version

- **Fresh logins (proxy or scripted) work on Python 3.10+.** alexapy saves the
  cookie on *your* Python and reads it back on the same Python — no version
  mismatch.
- **Python 3.14 is needed only to `import-pickle` a cookie written on Python
  3.14.** The cookie's `partitioned` attribute is added to `http.cookies.Morsel`
  in 3.14; unpickling such a cookie on an older interpreter raises
  `CookieError: Invalid attribute 'partitioned'` (a.k.a. the `partitioned`
  `KeyError`). This *only* affects importing a newer pickle — it never affects a
  login you performed yourself. Home Assistant's pickle is the usual culprit, as
  recent HA images ship Python 3.14.

If you don't use `import-pickle`, ignore all of this and run on any 3.10+.

## Commands

Every command supports a global `--json` flag for clean machine-readable output.

| Command | Description |
| --- | --- |
| `auth login` | **Guided browser login** (default). `--password`/`--otp-secret` for scripted/CI. |
| `auth import-pickle <path>` | Import an existing alexapy cookie (e.g. HA's) into the local config dir |
| `auth status` | Validate the saved cookie (`test_loggedin`) |
| `config show` / `config save` | Show / persist the connection profile (email + region) |
| `devices list [--ha-only \| --native-only] [--manufacturer <substr>]` | List smart-home devices with manufacturer + native-vs-HA `source` marker (each HA device shows its mapped entity id) |
| `devices prune --whitelist <file>` | Delete HA-sourced appliances whose entity isn't whitelisted (dry-run default; `--no-dry-run --yes` to execute) |
| `devices delete [<applianceId...>] [--entity <ha.id>] [--name "<display>"]` | Delete appliances by id, HA entity, or Alexa display name (`--yes` to execute) |
| `devices rename <target> <new-name>` | Rename a device — target = applianceId / endpoint id / display name (`--yes` to execute) |
| `devices duplicates` | Detect devices exposed twice (native + HA twin, or any shared display name) |
| `discover` | Trigger Alexa smart-home device discovery (`--yes` to execute) |
| `echos list` | List the physical Echo devices on the account |
| `groups list` | List Alexa smart-home device-groups (rooms): name, id, member count/names |
| `groups create <name> [--entity ... \| --endpoint ...]` | Create a device-group with the given members (`--yes` to execute) |
| `groups add <group> [--entity ... \| --endpoint ... \| --device ...]` | Add members to a group by name/id (`--yes`) |
| `groups remove <group> [--entity ... \| --endpoint ... \| --device ...]` | Remove members from a group by name/id (`--yes`) |
| `groups set <group> [--entity ... \| --endpoint ... \| --device ...]` | Replace a group's entire member set (`--yes`) |
| `groups delete <group>` | Delete a device-group by name/id (`--yes` to execute) |
| `routines list` | List Alexa routines (behaviors) with trigger utterance + action-target summary |
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
addressed by Alexa endpoint id (`amzn1.alexa.endpoint.*`), by Home Assistant
`--entity` id (resolved to its endpoint via the `endpoints` query — the same
`..._<domain>#<object_id>` tail parse used for appliances), or by Alexa display
name with `--device`. **`--device` is how you target native / non-HA devices**
(e.g. Tasmota-Wemo plugs) that have no HA entity — it resolves a device by its
normalized display name; an ambiguous name aborts and lists the matches.

```bash
cli-anything-alexa groups list
cli-anything-alexa groups create "Den" --entity light.den_lamp --entity media_player.den_tv   # preview
cli-anything-alexa groups create "Den" --entity light.den_lamp --yes                           # execute
cli-anything-alexa groups add "Den" --entity switch.den_fan --yes      # ADD delta (HA entity)
cli-anything-alexa groups add "Den" --device "Lounge Plug" --yes      # ADD a native (non-HA) device by name
cli-anything-alexa groups remove "Den" --entity light.den_lamp --yes   # REMOVE delta
cli-anything-alexa groups set "Den" --entity light.den_lamp --yes      # REPLACE whole member set
cli-anything-alexa groups delete "Den" --yes
```

Groups are looked up by id or by friendly name (case/space/punctuation
insensitive). Two API gotchas are handled internally and worth knowing:

- **Member id lists are GraphQL `[String!]` arrays.** They must serialize as
  real JSON arrays. Passing a single `json.dumps`'d string makes GraphQL coerce
  it into a 1-element list and the server **silently no-ops** (no error, nothing
  changes). The variables builders pass real Python lists.
- **`create` must not send `associatedUnitIds`** — doing so triggers
  `BAD_REQUEST`. Alexa auto-associates the unit from the member devices, so
  create takes `friendlyName` + `memberDeviceIds` only.

### Renaming, duplicates & discovery

```bash
cli-anything-alexa devices rename "Lounge Twigs" "Lounge Lights"        # preview
cli-anything-alexa devices rename light.kitchen_big "Kitchen Spots" --yes
cli-anything-alexa devices duplicates                                  # find double-exposed devices
cli-anything-alexa devices delete --name "Old Plug" --yes             # or --entity / positional applianceId
cli-anything-alexa discover --yes                                      # trigger a device-discovery sweep
```

- **`devices rename <target> <new-name>`** renames via the GraphQL
  `setEndpointFriendlyName` mutation. `<target>` resolves in precedence order:
  exact applianceId → exact endpoint id → exact display name → normalized
  (case/space/punctuation-insensitive) display name. **If the target matches
  more than one device** (a native appliance and its Home Assistant twin can
  share a name) the command **aborts and lists the matches** so you disambiguate
  by applianceId or endpoint id.
- **`devices duplicates`** lists every display name exposed by more than one
  endpoint, flagging the classic *native + HA twin* (the same physical device
  surfaced both natively and via the HA bridge). It only reports — you decide
  which copy to drop, then `devices delete` it.
- **`devices delete`** still takes positional applianceId(s), and now also
  `--entity <ha.id>` and `--name "<display>"`, which resolve to the applianceId
  via the endpoints query (same ambiguity-abort rule as rename).
- **`discover`** triggers a smart-home discovery sweep
  (`POST /api/phoenix/discovery`).

### Routines

`routines list` surfaces each routine's trigger utterance and a best-effort
**action-target** summary (action-node type / operations / SmartHome target id),
parsed from `/api/behaviors/v2/automations`.

> **Editing an existing routine is Alexa-app-only.** Amazon hard-refuses every
> API write path for `ROUTINE`-type automations: `updateAutomation` returns
> *"not supported for automation type: ROUTINE"*, `batchUpdateAutomations`
> requires an opaque scripted-source blob the read API never returns, and a REST
> `PUT` 404s. So this CLI can **list** and **trigger** routines, but not edit
> them — make routine edits in the Alexa app.

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

## Gotchas

- **Unofficial API.** Endpoints can change; heavy use can trip Amazon's bot
  defences. Treat it as best-effort.
- **Region matters.** A `amazon.co.uk` account talks to `alexa.amazon.co.uk`.
  Set it with `--url amazon.co.uk` / `--url amazon.com` and persist via
  `auth login` or `config save`.
- **Mutations are guarded.** Everything that changes state is dry-run by default
  and needs `--yes`.
- **Proxy URL reachability.** The login proxy binds loopback by default; from a
  remote/headless host, SSH-tunnel the port or use `--host 0.0.0.0`.

## Tests

```bash
pip install -e '.[test]'
python3 -m pytest tests/ -v
```

The unit tests cover the **pure logic** — appliance-id → entity parsing,
whitelist filtering / prune planning, table formatting, notification payload
builders, device-group GraphQL variables builders / name-normalization /
lookup / entity→endpoint resolution, and proxy-URL formatting — with no
`alexapy` dependency and no live account.

## License

MIT — see [LICENSE](LICENSE).
