# cli-anything-alexa

Programmatic management of **Amazon Alexa** from the command line, built on
the unofficial Alexa web API (the same one the Alexa app uses) via the
[`alexapy`](https://pypi.org/project/alexapy/) library. Sibling of
`cli-anything-homeassistant` and `cli-anything-zigbee2mqtt` — same Click +
REPL + `--json` conventions.

Manage smart-home **appliances** (incl. pruning Home-Assistant-sourced
orphans, reading live state and turning things on/off), **groups**,
**routines**, **alarms/timers/reminders** and **Alexa Guard**, plus everything
an Echo can do: **media** transport, **announce** / **speak** / **push**,
**bluetooth** connect-disconnect, arbitrary **voice commands** (`run command`)
and the **voice history** (`activity`).

> **Unofficial API caveat.** Amazon publishes no official Alexa device-management
> API. This drives the private web endpoints the app uses. They can change or
> break without notice, and aggressive use may trip Amazon's bot defences.
> No credentials are stored — only a local session cookie. Mutating commands
> are dry-run-by-default and require `--yes`.

It pairs naturally with `cli-anything-homeassistant` if you expose HA entities
to Alexa.

## Install

```bash
pip install -e .            # exposes the `cli-anything-alexa` console script
```

Requires **Python 3.10+**. A fresh login (proxy or scripted) saves the cookie
on *your* Python and reads it back on the same Python, so 3.10+ is enough.
Python **3.14** is needed **only** to `import-pickle` a cookie written on
Python 3.14 (e.g. Home Assistant's): that cookie's `partitioned` attribute is
unpicklable on older interpreters (`CookieError: Invalid attribute
'partitioned'`). It never affects a login you performed yourself.

## Auth

### 1. Guided browser login — `auth login` (recommended, no HA)

The default flow needs no Home Assistant and handles captcha / 2FA natively
because you complete Amazon's own login pages in a browser. A tiny local web
proxy (alexapy's `AlexaProxy`, the same mechanism HA's `alexa_media` config
flow uses) captures the session and saves the cookie.

```bash
cli-anything-alexa auth login
# prompts for email + region, prints a local URL to open in a browser,
# then waits for you to finish signing in to Amazon.
```

- Open the printed URL in a browser **on the same machine** (the proxy binds
  `127.0.0.1` by default). On a headless box, SSH-tunnel the port
  (`ssh -L 3001:127.0.0.1:3001 host`) or pass `--host 0.0.0.0` and open
  `http://<host>:3001` from your laptop.
- Flags: `--email`, `--url <region>`, `--host`, `--port` (default `3001`),
  `--timeout`.

### 2. Scripted login (headless / CI) — `auth login --password ...`

```bash
cli-anything-alexa auth login --email you@example.com --password 'secret'
# fully non-interactive (TOTP base32 for 2FA):
cli-anything-alexa auth login --email you@example.com \
  --password 'secret' --otp-secret 'JBSWY3DPEHPK3PXP'
```

Passing `--password` selects the scripted path. Amazon often captcha-blocks it;
when it does, the command points you back at the proxy flow.

### 3. Reuse Home Assistant's cookie — `auth import-pickle` (convenience)

If you already run the HA `alexa_media` integration, reuse its cookie instead of
logging in again:

```bash
cli-anything-alexa --email you@example.com config save
cli-anything-alexa auth import-pickle \
  /config/.storage/alexa_media.you@example.com.pickle --email you@example.com
cli-anything-alexa auth status            # -> {"email": ..., "logged_in": true}
```

`import-pickle` copies the pickle into `~/.config/cli-anything-alexa/` under the
`alexa_media.<email>.pickle` name `alexapy` expects, then validates it. (See the
[Install](#install) note about Python 3.14 if HA's pickle won't load.)

The account **region** matters: a `amazon.co.uk` account uses base
`https://alexa.amazon.co.uk`. Set it with `--url amazon.co.uk` (the default) or
`--url amazon.com`, and persist via `auth login` / `config save`.

## Commands

Every command supports a global `--json` flag for machine-readable output.

| Command | Description |
| --- | --- |
| `auth login` | **Guided browser login** (default). `--password`/`--otp-secret` for scripted/CI. |
| `auth import-pickle <path>` | Import an existing alexapy cookie (e.g. HA's) into the local config dir |
| `auth status` | Validate the saved cookie (`test_loggedin`) |
| `config show` / `config save` | Show / persist the connection profile (email + region) |
| `devices list [--ha-only \| --native-only] [--manufacturer <substr>]` | List smart-home devices with manufacturer + native-vs-HA `source` marker (each HA device shows its mapped entity id) |
| `devices prune --whitelist <file>` | Delete HA-sourced appliances whose entity isn't whitelisted (dry-run default; `--no-dry-run --yes` to execute) |
| `devices delete [<applianceId...>] [--entity <ha.id>] [--name "<display>"] [--verify]` | Delete appliances by id, HA entity, or display name (`--yes`). Warns on native devices; `--verify` re-discovers and reports which re-synced |
| `devices rename <target> <new-name>` | Rename a device — target = applianceId / endpoint id / display name (`--yes` to execute) |
| `devices rename --pattern 's/RE/REPL/[ig]' \| --map <file> [--speakable]` | **Bulk** rename via sed-style regex over every name, or a `current => new` file. Dry-run preview, `--yes` to execute; `--speakable` fixes DACS-rejected (hyphen) names |
| `devices duplicates` | Detect devices exposed twice (native + HA twin, or any shared display name) |
| `devices state [<target>...] [--all]` | Read live smart-home state (power, brightness, colour, temperature…) |
| `devices on\|off [<target>...] [--all]` | Turn appliances on / off (`--yes` to execute) |
| `devices light <target> [--on\|--off] [--brightness N] [--color <name>] [--color-temperature <name>]` | Drive a light's power / brightness / colour (`--yes` to execute) |
| `discover` | Trigger Alexa smart-home device discovery (`--yes` to execute) |
| `guard status` / `guard set away\|home` | Read / set Alexa Guard's arm state (`--yes` to set) |
| `echos list` | List the physical Echo devices on the account |
| `echos bluetooth` | Bluetooth devices paired to each Echo, account-wide (name, MAC, connected) |
| `echos pairings [<device>]` | Paired sinks for **one** Echo, with the `address` `echos connect` takes |
| `echos connect <name\|mac> [--device ...]` | Connect an already-paired bluetooth sink to an Echo (`--yes` to execute) |
| `echos disconnect [--device ...]` | Disconnect **every** bluetooth sink from an Echo (`--yes` to execute) |
| `echos wake-words` | Configured wake word per Echo |
| `echos dnd` | Read the current do-not-disturb state of every Echo |
| `kids profiles` | List the Amazon Kids child profiles in the household (name, age, directedId) |
| `kids status [<device>]` | Amazon Kids state per Echo — every Echo, or one named speaker |
| `kids enable <device> --child <name\|id>` | Turn Amazon Kids ON for an Echo by assigning it to a child (`--yes` to execute) |
| `kids disable <device>` | Turn Amazon Kids OFF for an Echo (unassign it) (`--yes` to execute) |
| `groups list` | List device-groups (rooms): name, id, member count/names, child-group count/names |
| `groups create <name> [--entity ... \| --endpoint ... \| --child-group ...]` | Create a device-group with members and/or nested child groups (`--yes`) |
| `groups add <group> [--entity ... \| --endpoint ... \| --device ... \| --child-group ...]` | Add members / child groups to a group by name/id (`--yes`) |
| `groups remove <group> [--entity ... \| --endpoint ... \| --device ... \| --child-group ...]` | Remove members / child groups from a group by name/id (`--yes`) |
| `groups set <group> [--entity ... \| --endpoint ... \| --device ... \| --child-group ...]` | Replace a group's entire member + child-group set (`--yes`) |
| `groups delete <group>` | Delete a device-group by name/id (`--yes` to execute) |
| `routines list` | List Alexa routines (behaviors) with trigger utterance + action-target summary (editing a routine is brittle/destructive via API — do it in the app) |
| `routines run <name\|id>` | Trigger a routine via `behaviors/preview` (`--yes` to execute) |
| `notifications list` | List alarms / timers / reminders |
| `notifications add-reminder <label> --device ... [--in N \| --at MS]` | Create a reminder (`--yes` to execute) |
| `notifications add-alarm --device ... [--in N \| --at MS]` | Create an alarm (`--yes` to execute) |
| `notifications add-timer --device ... --duration N` | Create a timer (`--yes` to execute) |
| `notifications delete <id>` | Delete a notification (`--yes` to execute) |
| `media status [<device>]` | What an Echo is playing (state, title, artist, album, provider, volume) |
| `media play\|pause\|next\|previous\|forward\|rewind [<device>]` | Transport control on an Echo (`--yes` to execute) |
| `media stop [<device>] [--all]` | Stop playback on one Echo, or every device with `--all` (`--yes`) |
| `media volume [<device>] --level 0-100` | Set an Echo's volume (`--yes` to execute) |
| `media shuffle\|repeat [<device>] --state on\|off` | Toggle shuffle / repeat (`--yes` to execute) |
| `media play-music <phrase> [--device ...] [--provider ...]` | Play music by search phrase from a provider (`--yes` to execute) |
| `announce <text> [--device ...]` | Speak an announcement on all (or one) Echo (`--yes` to execute) |
| `speak <text> [--device ...]` | Say text on one Echo via TTS — no announcement chime (`--yes` to execute) |
| `push <text> [--title ...] [--device ...] [--dropin]` | Push a notification to the **Alexa app** — silent in the house (`--yes` to execute) |
| `dnd <device> on\|off` | Toggle do-not-disturb on a device (`--yes` to execute) |
| `run command "<utterance>" [--device ...]` | Run literal text as if spoken to Alexa — reaches everything (`--yes` to execute) |
| `run sequence <name> [--device ...]` | Built-in behaviour: weather / joke / good-night / … (`--yes` to execute) |
| `run sound <alias\|id> [--device ...]` | Play a soundbank sound (`--yes` to execute) |
| `run skill <amzn1.ask.skill...> [--device ...]` | Launch a skill by id (`--yes` to execute) |
| `run catalog [--kind sequences\|sounds]` | List the known sequences / sound aliases (no account needed) |
| `activity history [--limit N] [--hours N] [--device ...] [--contains ...]` | Recent voice turns: what was said and what Alexa replied |
| `activity records [--limit N]` | Legacy activity feed (carries per-activity ids + status) |
| `activity last [--limit N]` | The last Echo that answered, and what it was asked |
| `activity clear [--items N]` | Delete recent voice recordings — irreversible (`--yes` to execute) |
| `repl` | Interactive shell (default when no subcommand) |

### Media & voice on Echo devices

```bash
cli-anything-alexa media status "Kitchen Echo" --json
cli-anything-alexa media pause "Kitchen Echo" --yes
cli-anything-alexa media volume "Kitchen Echo" --level 35 --yes   # 0-100, converted to alexapy's 0.0-1.0
cli-anything-alexa media play-music "miles davis" --provider SPOTIFY --yes
cli-anything-alexa media stop --all --yes
cli-anything-alexa speak "the oven is done" --device "Kitchen Echo" --yes
cli-anything-alexa push "the washing machine finished" --yes    # phone, not speakers
cli-anything-alexa echos bluetooth --json
```

`DEVICE` is an Echo accountName or serialNumber; omit it and the first **online**
Echo is used. `announce` plays the announcement chime and can hit every device;
`speak` is plain TTS on one speaker; `push` makes no sound at all (it lands in
the Alexa app, `--dropin` for the drop-in variant). Device records are adapted through
`core/device_ref.py` — alexapy reads `device_serial_number`/`_device_type` as
*attributes*, while `get_devices()` returns dicts, so the raw dict would raise
`AttributeError` from inside alexapy.

### Voice commands, behaviours & history

`run command` sends **literal text through Alexa's own parser** — anything Alexa
understands by voice, including skills and devices with no typed command here, is
reachable through it. Alexa answers out loud and the endpoint returns no payload,
so read back what happened with `activity history` (that pairing is the intended
workflow).

```bash
cli-anything-alexa run catalog                    # known sequences + sound aliases
cli-anything-alexa run command "turn off the kitchen lights" --yes
cli-anything-alexa run sequence good-night --device "Bedroom Echo" --yes
cli-anything-alexa run sound doorbell --yes
cli-anything-alexa activity history --hours 2 --json
cli-anything-alexa activity last
```

Unknown *ids* pass through (Amazon keeps adding behaviours/sounds); an unknown
friendly **name** is refused locally with the alternatives, because the API
answers an unknown sequence with a generic failure. `--queue-delay` batches
commands into one behaviour node; omitted, alexapy's own per-call default applies.
`activity history` drops `DEVICE_ARBITRATION` wake-word races unless
`--include-noise`; `activity clear` is irreversible and reports a **partial**
clear when Amazon refuses an entry.

### Bluetooth on an Echo

`echos connect` calls `pair-sink`, which connects an **already paired** sink —
the initial pairing handshake is Alexa-app/voice-only. A target that is not in
`echos pairings` is refused locally with the list of what is paired (Amazon
answers `pair-sink` for an unknown address with a bare `200` and does nothing).
Any MAC spelling resolves the pairing, but the `address` Amazon reported is what
gets posted. `echos disconnect` is all-or-nothing — there is no per-sink
disconnect endpoint.

```bash
cli-anything-alexa echos pairings "Kitchen Echo" --json
cli-anything-alexa echos connect "Jon's Phone" --device "Kitchen Echo" --yes
cli-anything-alexa echos disconnect --device "Kitchen Echo" --yes
```

### Amazon Kids (child mode)

`kids` assigns an Echo to a child profile, which puts the speaker into Amazon
Kids mode (kid-safe content, restricted purchasing/calling). Because that
changes what a speaker will *do*, `enable`/`disable` name their target Echo
explicitly instead of defaulting to the first online one.

**A kids write reports nothing**, so these commands verify. alexapy's
`enable_child_mode`/`disable_child_mode` return `None` whether they succeeded or
were rejected, and the assign rides the parent-dashboard host with its own
`ft-panda-csrf-token` (a missing token is only logged at debug, so a rejected
assign is silent). Every write therefore re-reads the device's state afterwards
and reports `ok` from what Amazon *actually holds*, not from "nothing raised".

In `kids status`, an empty `kids` column means the state could not be read for
that speaker — which is **not** the same answer as `off`.

```bash
cli-anything-alexa kids profiles --json          # name, age, directedId
cli-anything-alexa kids status                   # every Echo
cli-anything-alexa kids status "Playroom Echo"   # just one
cli-anything-alexa kids enable "Playroom Echo" --child Alice --yes
cli-anything-alexa kids disable "Playroom Echo" --yes
```

An unknown child name is refused locally with the known profiles (Amazon rejects
an unknown `childDirectedId` without a message reaching the caller), and two
siblings sharing a first name abort with their `directedId`s to pick from.

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
`--entity` id (resolved via the `endpoints` query — the same
`..._<domain>#<object_id>` tail parse used for appliances), or by Alexa display
name with `--device`. **`--device` targets native / non-HA devices** (e.g.
Tasmota-Wemo plugs) that have no HA entity; an ambiguous name aborts + lists
matches.

```bash
cli-anything-alexa groups list
cli-anything-alexa groups create "Den" --entity light.den_lamp --entity media_player.den_tv   # preview
cli-anything-alexa groups create "Den" --entity light.den_lamp --yes                           # execute
cli-anything-alexa groups add "Den" --entity switch.den_fan --yes      # ADD delta (HA entity)
cli-anything-alexa groups add "Den" --device "Lounge Plug" --yes      # ADD a native device by name
cli-anything-alexa groups remove "Den" --entity light.den_lamp --yes   # REMOVE delta
cli-anything-alexa groups set "Den" --entity light.den_lamp --yes      # REPLACE whole member set
cli-anything-alexa groups delete "Den" --yes
# nested / child groups — the rollup pattern (a group of groups):
cli-anything-alexa groups create "Downstairs" --child-group "Living Room" --child-group "Kitchen" --yes
cli-anything-alexa groups add "Downstairs" --child-group "Hallway" --yes
```

Groups are looked up by id or by friendly name (case/space/punctuation
insensitive). Gotchas handled internally and worth knowing:

- **Member / child-group id lists are GraphQL `[String!]` arrays.** They must
  serialize as real JSON arrays. Passing a single `json.dumps`'d string makes
  GraphQL coerce it into a 1-element list and the server **silently no-ops** (no
  error, nothing changes). The variables builders pass real Python lists.
- **Child groups** use `childDeviceGroupIds` + `childDeviceGroupIdsUpdateOperation`
  (ADD/REMOVE/REPLACE), mirroring the member fields, resolved from a group name/id.
- **`create` must not send `associatedUnitIds`** — doing so triggers
  `BAD_REQUEST`. Alexa auto-associates the unit from the member devices, so
  create takes `friendlyName` + `memberDeviceIds` (+ optional `childDeviceGroupIds`).

## Bulk rename, DACS names & native re-sync

```bash
# bulk rename — sed-style regex over EVERY device name (dry-run preview first):
cli-anything-alexa devices rename --pattern 's/^Spots - (.*)/\1 Spots/'
cli-anything-alexa devices rename --map renames.txt --yes      # 'old name => new name' lines
```

- **`--pattern 's/REGEX/REPL/[ig]'`** applies a Python-`re` substitution to every
  device's current name (capture groups `\1`, flags `i`/`g`); changed names form
  the rename set. **`--map <file>`** reads `current name => new name` (or
  `endpointId => new name`) lines (`#` comments). Both dry-run by default with a
  full `old -> new` table; `--yes` executes, no-ops skipped.
- **DACS rejects non-speakable names.** `setEndpointFriendlyName` refuses hyphens /
  control chars (`"Invalid input from DACS"`, `BAD_REQUEST`; `"elt-k8s-1 Temperature"`
  refused, `"elt k8s 1 Temperature"` accepted). `--speakable` auto-fixes them;
  otherwise the CLI pre-warns and translates a DACS rejection into a friendly
  suggestion.
- **Native devices re-sync.** `devices delete` warns when a target isn't
  `manufacturerName=="Home Assistant"` (it re-syncs from its source bridge/skill —
  Tuya from Smart Life, Hue from the bridge); `--verify` re-discovers and reports
  which just-deleted devices re-appeared.

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

The tests cover the **pure logic** — appliance-id → entity parsing, whitelist
filtering / prune planning, table formatting, the notification payload builders,
the device-group GraphQL variables builders / name-normalization / lookup /
entity→endpoint resolution, the `DeviceRef` adapter contract, the media
volume/provider/player-state helpers, smart-home capability-state decoding,
sequence / sound / skill-id normalisation, activity-feed flattening and
bluetooth address canonicalisation — plus the async layers and every CLI command
path against fakes (including the dry-run-by-default contract on every mutating
command). No `alexapy` traffic and no live account.
