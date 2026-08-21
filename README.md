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

### 3. Reuse an existing Home Assistant cookie

If you already run the HA `alexa_media` integration you can reuse its cookie
instead of logging in again. **There are two ways, and which you pick matters
when HA is actively using the same account:**

#### 3a. Read HA's live cookie in place — `--cookie-dir` (recommended for HA reuse)

HA's `alexa_media` integration **rotates the cookie constantly**. So point the
CLI at HA's config base and it reads the cookie **in place** — always the
current, just-rotated copy:

```bash
cli-anything-alexa --email you@example.com --cookie-dir /config auth status
cli-anything-alexa --email you@example.com --cookie-dir /config devices list
```

`--cookie-dir <dir>` reads/writes the cookie at `<dir>/.storage/alexa_media.<email>.pickle`
(HA's own layout), so `--cookie-dir /config` resolves straight to HA's live
pickle. Nothing is copied, so it never goes stale. Env equivalent:
`CLI_ALEXA_COOKIE_DIR=/config`. The CLI also auto-recovers the rotation race
(if the cookie is rewritten between read and use it re-reads and retries, a
couple of times, without hammering Amazon's login).

#### 3b. Copy a snapshot — `auth import-pickle`

Copies HA's cookie into the CLI's own config dir once:

```bash
cli-anything-alexa auth import-pickle \
  /config/.storage/alexa_media.you@example.com.pickle --email you@example.com
cli-anything-alexa auth status            # -> {"email": ..., "logged_in": true}
```

> **Heads-up:** this is a one-time *snapshot*. If HA is actively using the same
> account, the copy goes **stale within seconds** as HA rotates the cookie, and
> `auth status` can flip `logged_in: true → false` mid-session. For active HA
> reuse use `--cookie-dir` (3a); use `import-pickle` only for a standalone copy
> you then keep fresh with your own `auth login`.

> **Python heads-up:** a pickle written by a *newer* Python can't be read by an
> older one — see [Python version](#python-version). If HA runs Python 3.14,
> run the CLI on Python 3.14 (or just use the proxy login instead).

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
| `auth import-pickle <path>` | Copy an existing alexapy cookie (e.g. HA's) into the local config dir (snapshot — goes stale if HA keeps rotating it; prefer `--cookie-dir`) |
| `auth status` | Validate the saved cookie (`test_loggedin`) |
| `auth whoami` | Show WHO the cookie is logged in as (`/api/users/me`: customer id, name, email, Prime Music) — exits non-zero if it no longer buys an account |
| `config show` / `config save` | Show / persist the connection profile (email + region) |
| `devices list [--ha-only \| --native-only] [--manufacturer <substr>]` | List smart-home devices with manufacturer + native-vs-HA `source` marker (each HA device shows its mapped entity id) |
| `devices prune --whitelist <file>` | Delete HA-sourced appliances whose entity isn't whitelisted (dry-run default; `--no-dry-run --yes` to execute) |
| `devices delete [<applianceId...>] [--entity <ha.id>] [--name "<display>"] [--verify]` | Delete appliances by id, HA entity, or Alexa display name (`--yes`). Warns on native devices; `--verify` re-discovers and reports which re-synced |
| `devices rename <target> <new-name>` | Rename a device — target = applianceId / endpoint id / display name (`--yes` to execute) |
| `devices rename --pattern 's/RE/REPL/[ig]'` | **Bulk** rename: sed-style regex over every device name (capture groups `\1`); dry-run preview, `--yes` to execute |
| `devices rename --map <file>` | **Bulk** rename from `current name => new name` (or `endpointId => new name`) lines (`#` comments) |
| `devices rename ... --speakable` | Auto-fix new names DACS would reject (hyphens→spaces, strip control chars) |
| `devices duplicates` | Detect devices exposed twice (native + HA twin, or any shared display name) |
| `devices state [<target>...] [--all]` | Read live smart-home state (power, brightness, colour, temperature…) |
| `devices on\|off [<target>...] [--all]` | Turn appliances on / off (`--yes` to execute) |
| `devices light <target> [--on\|--off] [--brightness N] [--color <name>] [--color-temperature <name>]` | Drive a light's power / brightness / colour (`--yes` to execute) |
| `discover` | Trigger Alexa smart-home device discovery (`--yes` to execute) |
| `guard status` | Read Alexa Guard's arm state (away vs home) |
| `guard set away\|home` | Arm / disarm Alexa Guard (`--yes` to execute) |
| `echos list` | List the physical Echo devices on the account |
| `echos bluetooth` | Show the bluetooth devices paired to each Echo, account-wide (name, MAC, connected) |
| `echos pairings [<device>]` | Paired sinks for **one** Echo, with the `address` that `echos connect` takes |
| `echos connect <name\|mac> [--device ...]` | Connect an already-paired bluetooth device to an Echo (`--yes` to execute) |
| `echos disconnect [--device ...]` | Disconnect **every** bluetooth sink from an Echo (`--yes` to execute) |
| `echos wake-words` | Show the configured wake word per Echo |
| `echos dnd` | Read the current do-not-disturb state of every Echo |
| `echos preferences [<device>]` | Per-Echo preferences: **timezone**, locale, temperature/distance units, postal code |
| `echos wifi [<device>]` | One Echo's wifi details (SSID, signal, security, MAC/IP) |
| `kids profiles` | List the Amazon Kids child profiles in the household (name, age, directedId) |
| `kids status [<device>]` | Amazon Kids state per Echo — every Echo, or one named speaker |
| `kids enable <device> --child <name\|id>` | Turn Amazon Kids ON for an Echo by assigning it to a child profile (`--yes` to execute) |
| `kids disable <device>` | Turn Amazon Kids OFF for an Echo, unassigning it (`--yes` to execute) |
| `groups list` | List device-groups (rooms): name, id, member count/names, child-group count/names |
| `groups create <name> [--entity ... \| --endpoint ... \| --child-group ...]` | Create a device-group with members and/or nested child groups (`--yes`) |
| `groups add <group> [--entity ... \| --endpoint ... \| --device ... \| --child-group ...]` | Add members / child groups to a group by name/id (`--yes`) |
| `groups remove <group> [--entity ... \| --endpoint ... \| --device ... \| --child-group ...]` | Remove members / child groups from a group by name/id (`--yes`) |
| `groups set <group> [--entity ... \| --endpoint ... \| --device ... \| --child-group ...]` | Replace a group's entire member + child-group set (`--yes`) |
| `groups delete <group>` | Delete a device-group by name/id (`--yes` to execute) |
| `routines list` | List Alexa routines (behaviors) with trigger utterance + action-target summary |
| `routines run <name\|id>` | Trigger a routine via `behaviors/preview` (`--yes` to execute) |
| `notifications list` | List alarms / timers / reminders |
| `notifications add-reminder <label> --device ... [--in N \| --at MS]` | Create a reminder (`--yes` to execute) |
| `notifications add-alarm --device ... [--in N \| --at MS]` | Create an alarm (`--yes` to execute) |
| `notifications add-timer --device ... --duration N` | Create a timer (`--yes` to execute) |
| `notifications show <id\|label>` | Show one notification (row + the raw record an edit is built from) |
| `notifications pause <id\|label>` | Pause an alarm/reminder (status `OFF`) without deleting it (`--yes` to execute) |
| `notifications resume <id\|label>` | Re-enable a paused alarm/reminder (`--yes` to execute) |
| `notifications reschedule <id\|label> --in N \| --at MS` | Move an alarm/reminder to a new time (`--yes` to execute) |
| `notifications snooze <id\|label> [--minutes N]` | Push an alarm/reminder further out — default 9 min, Amazon's own snooze (`--yes`) |
| `notifications delete <id>` | Delete a notification (`--yes` to execute) |
| `media status [<device>]` | Show what an Echo is playing (state, title, artist, album, provider, volume) |
| `media play\|pause\|next\|previous\|forward\|rewind [<device>]` | Transport control on an Echo (`--yes` to execute) |
| `media stop [<device>] [--all]` | Stop playback on one Echo, or every device with `--all` (`--yes`) |
| `media volume [<device>] --level 0-100` | Set an Echo's volume (`--yes` to execute) |
| `media shuffle\|repeat [<device>] --state on\|off` | Toggle shuffle / repeat (`--yes` to execute) |
| `media play-music <phrase> [--device ...] [--provider ...]` | Play music by search phrase from a provider (`--yes` to execute) |
| `announce <text> [--device ...]` | Speak an announcement on all (or one) Echo (`--yes` to execute) |
| `speak <text> [--device ...]` | Say text on one Echo via TTS — **no announcement chime** (`--yes` to execute) |
| `push <text> [--title ...] [--device ...] [--dropin]` | Push a notification to the **Alexa app** — silent in the house (`--yes` to execute) |
| `dnd <device> on\|off` | Toggle do-not-disturb on a device (`--yes` to execute) |
| `run command "<utterance>" [--device ...]` | Run literal text as if spoken to Alexa — reaches everything (`--yes` to execute) |
| `run sequence <name> [--device ...]` | Run a built-in behaviour (weather, joke, good-night…) (`--yes` to execute) |
| `run sound <alias\|id> [--device ...]` | Play a soundbank sound (`--yes` to execute) |
| `run skill <amzn1.ask.skill...> [--device ...]` | Launch a skill by id (`--yes` to execute) |
| `run catalog [--kind sequences\|sounds]` | List the known sequences / sound aliases (no account needed) |
| `activity history [--limit N] [--hours N] [--device ...] [--contains ...]` | Recent voice turns: what was said and what Alexa replied |
| `activity records [--limit N]` | The legacy activity feed (carries per-activity ids + status) |
| `activity last [--limit N]` | The last Echo that answered, and what it was asked |
| `activity clear [--items N]` | Delete recent voice recordings — irreversible (`--yes` to execute) |
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

**Nested / child groups (the rollup pattern).** A group can contain *other
groups* — e.g. a "Downstairs" group made of your per-room groups. Use
`--child-group "<name|id>"` (repeatable, resolved by normalized group name → id)
on `create` / `add` / `remove` / `set`. `groups list` shows each group's child
groups alongside its devices.

```bash
cli-anything-alexa groups create "Downstairs" --child-group "Living Room" --child-group "Kitchen" --yes
cli-anything-alexa groups add "Downstairs" --child-group "Hallway" --yes     # ADD a child group
cli-anything-alexa groups remove "Downstairs" --child-group "Kitchen" --yes  # REMOVE a child group
```

Groups are looked up by id or by friendly name (case/space/punctuation
insensitive). Three API gotchas are handled internally and worth knowing:

- **Member / child-group id lists are GraphQL `[String!]` arrays.** They must
  serialize as real JSON arrays. Passing a single `json.dumps`'d string makes
  GraphQL coerce it into a 1-element list and the server **silently no-ops** (no
  error, nothing changes). The variables builders pass real Python lists.
- **Child groups use `childDeviceGroupIds` + `childDeviceGroupIdsUpdateOperation`**
  (ADD/REMOVE/REPLACE), exactly mirroring the member fields; the child field/op
  are omitted entirely on a member-only call.
- **`create` must not send `associatedUnitIds`** — doing so triggers
  `BAD_REQUEST`. Alexa auto-associates the unit from the member devices, so
  create takes `friendlyName` + `memberDeviceIds` (+ optional
  `childDeviceGroupIds`) only.

### Renaming, duplicates & discovery

```bash
cli-anything-alexa devices rename "Lounge Twigs" "Lounge Lights"        # preview
cli-anything-alexa devices rename light.kitchen_big "Kitchen Spots" --yes
# bulk rename — sed-style regex over EVERY device name (dry-run preview first):
cli-anything-alexa devices rename --pattern 's/^Spots - (.*)/\1 Spots/'
cli-anything-alexa devices rename --pattern 's/^TH - //' --yes
cli-anything-alexa devices rename --map renames.txt                      # 'old => new' lines
cli-anything-alexa devices duplicates                                  # find double-exposed devices
cli-anything-alexa devices delete --name "Old Plug" --verify --yes    # delete + re-check it didn't re-sync
cli-anything-alexa discover --yes                                      # trigger a device-discovery sweep
```

- **`devices rename <target> <new-name>`** renames via the GraphQL
  `setEndpointFriendlyName` mutation. `<target>` resolves in precedence order:
  exact applianceId → exact endpoint id → exact display name → normalized
  (case/space/punctuation-insensitive) display name. **If the target matches
  more than one device** (a native appliance and its Home Assistant twin can
  share a name) the command **aborts and lists the matches** so you disambiguate
  by applianceId or endpoint id.
- **Bulk rename — `--pattern` / `--map`.** `--pattern 's/REGEX/REPL/[ig]'`
  applies a sed-style Python-`re` substitution (capture groups `\1`, flags
  `i` case-insensitive / `g` global) to **every** device's current name; the
  ones that change form the rename set. `--map <file>` reads
  `current name => new name` (or `endpointId => new name`) lines (`#` comments).
  Both print a full `old -> new` **preview table** and are **dry-run by default**
  — re-run with `--yes` to execute. No-op renames (new == old) are skipped.
- **DACS rejects non-speakable names.** Amazon's rename API validates the new
  name through DACS and **refuses hyphens / non-speakable strings**
  (`"Invalid input. Invalid input from DACS"`, `BAD_REQUEST` — e.g.
  `"elt-k8s-1 Temperature"` is refused while `"elt k8s 1 Temperature"` is
  accepted). Pass `--speakable` to auto-transform new names (hyphens → spaces,
  strip stray control chars, collapse whitespace). Without it, the CLI **pre-warns**
  about non-speakable names and, on an actual DACS rejection, shows a friendly
  suggestion instead of the raw GraphQL error.
- **`devices duplicates`** lists every display name exposed by more than one
  endpoint, flagging the classic *native + HA twin* (the same physical device
  surfaced both natively and via the HA bridge). It only reports — you decide
  which copy to drop, then `devices delete` it.
- **`devices delete`** still takes positional applianceId(s), and now also
  `--entity <ha.id>` and `--name "<display>"`, which resolve to the applianceId
  via the endpoints query (same ambiguity-abort rule as rename). **Native
  (non-HA) devices re-sync from their cloud skill/bridge** (e.g. Tuya re-syncs
  from Smart Life, Philips Hue from the bridge), so deleting them in Alexa alone
  often doesn't stick — the command **warns** when a target isn't
  `manufacturerName=="Home Assistant"`, and **`--verify`** re-runs discovery,
  waits ~12s, re-queries, and reports which just-deleted devices **re-appeared**
  (those need removing at source).
- **`discover`** triggers a smart-home discovery sweep
  (`POST /api/phoenix/discovery`).

### Smart-home state & control

`devices state` reads live capability values over `/api/phoenix/state`, and
`devices on/off/light` writes them back. Targets resolve exactly like `rename`
(applianceId / endpoint id / display name, ambiguity aborts), and `--all` is
available on the read and the on/off writes.

```bash
cli-anything-alexa devices state "Kitchen Lamp" --json
cli-anything-alexa devices state --all --json
cli-anything-alexa devices off "Lounge Plug" --yes
cli-anything-alexa devices light "Kitchen Lamp" --on --brightness 40 --color soft_white --yes
```

`guard` reads and writes Alexa Guard's arm state:

```bash
cli-anything-alexa guard status
cli-anything-alexa guard set away --yes
```

### Voice commands & behaviours — `run`

`run command` sends **literal text through Alexa's own parser**, which makes it
the single highest-leverage call on the account: anything Alexa understands by
voice — including skills and devices this CLI has no typed command for — is
reachable through it. `run sequence` / `run sound` / `run skill` trigger the
built-in behaviours, the soundbank and a skill by id. Alexa answers *out loud*
and the behaviours endpoint returns no payload, so read back what happened with
`activity history`.

```bash
cli-anything-alexa run catalog                      # the known sequences + sounds
cli-anything-alexa run command "turn off the kitchen lights" --yes
cli-anything-alexa run sequence good-night --device "Bedroom Echo" --yes
cli-anything-alexa run sound doorbell --yes
cli-anything-alexa run skill amzn1.ask.skill.<uuid> --yes
```

Unknown *ids* are passed through (Amazon keeps adding behaviours and sounds), but
an unknown friendly **name** is refused locally with the alternatives listed —
the API answers an unknown sequence with a generic failure that tells you
nothing. `--queue-delay` batches everything issued within that window into one
behaviour node; omit it and alexapy's own per-call default (0 for text/skills,
1.5 for sounds/sequences) applies.

### Voice history — `activity`

```bash
cli-anything-alexa activity history --hours 2 --json            # transcript + Alexa's reply
cli-anything-alexa activity history --device "Kitchen Echo" --contains lights
cli-anything-alexa activity records --limit 50                  # legacy feed, has ids
cli-anything-alexa activity last                                # who answered last
cli-anything-alexa activity clear --items 20 --yes              # irreversible
```

`activity history` uses the privacy view
(`/alexa-privacy/apd/rvh/customer-history-records`) because it is the only feed
carrying **both halves** of a turn. `--hours` is a real query window, not a
client-side filter. `DEVICE_ARBITRATION` rows — the wake-word races Amazon
records when several Echos hear the same "Alexa" — are dropped unless you pass
`--include-noise`. `activity clear` deletes real recordings; when Amazon refuses
an entry (a 404: nothing to delete) the result reports the clear as **partial**
rather than clean.

### Media & voice on Echo devices

`media` drives the *physical* Echo speakers (see `echos list`), not smart-home
appliances. `DEVICE` is an accountName or serialNumber; **omit it and the first
online Echo is used**, matching how `announce` and `routines run` pick a runner.

```bash
# what is the kitchen Echo doing right now?
cli-anything-alexa media status "Kitchen Echo" --json

# transport control (dry-run by default, like every other mutation)
cli-anything-alexa media pause "Kitchen Echo"          # preview
cli-anything-alexa media pause "Kitchen Echo" --yes    # execute
cli-anything-alexa media next --yes                    # first online Echo

# volume is a human 0-100 here (alexapy wants 0.0-1.0 — the CLI converts)
cli-anything-alexa media volume "Kitchen Echo" --level 35 --yes

cli-anything-alexa media shuffle "Kitchen Echo" --state on --yes
cli-anything-alexa media play-music "miles davis" --provider SPOTIFY --yes

# stop everything in the house
cli-anything-alexa media stop --all --yes
```

**`announce` vs `speak`.** `announce` (`send_announcement`) plays Alexa's
announcement tone and can fan out to every device. `speak` (`send_tts`) is the
plain say-something path on a **single** speaker with no chime — alexapy
documents TTS `targets` as non-functional, so the CLI binds to the requested
device instead of passing targets.

```bash
cli-anything-alexa speak "the oven is done" --device "Kitchen Echo" --yes
```

**`push` — the silent channel.** `push` sends the message to the **Alexa app**
on your phone (`send_mobilepush`) rather than out of a speaker, which is what you
want from a script that might run at 3am. `--dropin` uses
`send_dropin_notification` instead, whose notification offers to drop in on the
resolved Echo. Both ride the behaviours API, so they still resolve an Echo (the
first online one by default) even though nothing plays on it.

```bash
cli-anything-alexa push "the washing machine finished" --yes
cli-anything-alexa push "check the nursery" --dropin --device "Nursery Echo" --yes
```

Echo state lives under `echos`:

```bash
cli-anything-alexa echos bluetooth --json     # paired phones/laptops, account-wide
cli-anything-alexa echos pairings "Kitchen Echo"   # one Echo, with the addresses
cli-anything-alexa echos wake-words           # ALEXA / ECHO / COMPUTER per device
cli-anything-alexa echos dnd                  # current DND state (the `dnd` command writes it)
```

**Bluetooth: connect / disconnect, not pair.** `echos connect` calls
`pair-sink`, which connects a sink that is **already paired** — the initial
pairing handshake (putting the phone in pairing mode, confirming the code) is
Alexa-app/voice-only. A target that isn't in `echos pairings` is refused locally
with the list of what *is* paired, because Amazon answers `pair-sink` for an
unknown address with a bare `200` and does nothing. The name or any spelling of
the MAC resolves the pairing; the `address` string Amazon reported is what gets
posted. `echos disconnect` is **all-or-nothing** — Amazon has no per-sink
disconnect endpoint — so it drops every connected sink on that Echo.

```bash
cli-anything-alexa echos pairings "Kitchen Echo" --json
cli-anything-alexa echos connect "Jon's Phone" --device "Kitchen Echo" --yes
cli-anything-alexa echos connect aa-bb-cc-dd-ee-ff --yes    # any MAC spelling works
cli-anything-alexa echos disconnect --device "Kitchen Echo" --yes
```

> **Device records are adapted, not passed through.** alexapy's device-bound
> methods read `device_serial_number` / `_device_type` / `_device_family` /
> `_locale` as **attributes** off the object handed to `AlexaAPI(device, login)`,
> but `get_devices()` returns plain dicts. `core/device_ref.py` translates one
> into the other; passing the raw dict raises `AttributeError` from inside
> alexapy (its `_catch_all_exceptions` decorator re-raises anything that isn't a
> connection/login error). Every device-bound call site goes through `DeviceRef`.

### Amazon Kids (child mode)

`kids` reads the household's child profiles and turns Amazon Kids on/off per
Echo by assigning the speaker to a child. Because that changes what the speaker
will *do* (kid-safe content, restricted purchasing/calling), `enable`/`disable`
require an explicit target Echo rather than defaulting to the first online one.

```bash
cli-anything-alexa kids profiles --json          # name, age, directedId
cli-anything-alexa kids status                   # every Echo
cli-anything-alexa kids status "Playroom Echo"   # just one
cli-anything-alexa kids enable "Playroom Echo" --child Alice --yes
cli-anything-alexa kids disable "Playroom Echo" --yes
```

**These writes report nothing, so the CLI verifies.**
`enable_child_mode`/`disable_child_mode` are declared `-> None` and wrapped in
alexapy's `_catch_all_exceptions`, so a rejected request and a successful one
look identical at the call site. Worse, the assign rides a *different* host (the
localized parent dashboard, `parents.amazon.co.uk` / `eltern.amazon.de`) with a
*different* csrf token (`ft-panda-csrf-token` echoed into `x-amzn-csrf`), and
alexapy only logs at **debug** if that token is missing — so a rejected assign is
silent. Every write here therefore re-reads the device state afterwards and
reports `ok` from what Amazon actually holds. An unknown child name is refused
locally with the known profiles, and siblings sharing a first name abort with
their `directedId`s.

> **"Unknown" is not "off".** `get_child_mode` returns `None` when the state
> could not be read, which is a different answer from `False`. `kids status`
> leaves the column blank in that case rather than reporting kids mode as off.

### Routines

`routines list` surfaces each routine's trigger utterance and a best-effort
**action-target** summary (action-node type / operations / SmartHome target id),
parsed from `/api/behaviors/v2/automations`.

> **Editing an existing routine is brittle and destructive — do it in the Alexa
> app.** It is not cleanly impossible: `updateAutomation` is rejected (*"not
> supported for automation type: ROUTINE"*) and a REST `PUT` 404s, but
> `batchUpdateAutomations` *does* mutate a `ROUTINE` — it needs an opaque
> scripted-source blob the read API won't return, so a malformed attempt
> **partially applies** (it can strip the routine's action and leave it
> action-less), and the `/api/behaviors/v2/automations` read goes **stale** so
> you can't even trust it to verify. This CLI therefore **list**s and
> **trigger**s routines but does not edit them.

### Alarms, timers & reminders

`notifications` lists, creates, **edits** and deletes the `/api/notifications`
surface. An edit targets a notification by **id or label** and is planned
before it is written: the dry-run prints the exact `field: from -> to` diff, and
`--yes` applies that same plan.

```bash
cli-anything-alexa notifications list --json
cli-anything-alexa notifications show "Wake up"          # row + the raw record
cli-anything-alexa notifications pause "Wake up"         # preview the diff
cli-anything-alexa notifications pause "Wake up" --yes   # status ON -> OFF
cli-anything-alexa notifications resume "Wake up" --yes
cli-anything-alexa notifications snooze "Wake up" --minutes 15 --yes
cli-anything-alexa notifications reschedule "Wake up" --in 3600 --yes
cli-anything-alexa notifications delete <id> --yes
```

**An edit is a whole-record PUT, not a patch.** `/api/notifications` *replaces*
the notification with the body it is given, so every edit starts from the record
Amazon returned and changes one or two fields. A hand-rolled minimal body is
accepted silently and quietly drops what it omitted (recurrence, the owning
device) — which is why `notifications show` exposes the raw record.

**A reminder fires off its LOCAL wall-clock fields.** Alongside `alarmTime`
(epoch ms), a record carries `originalDate`/`originalTime` — the date and
time-of-day *in the owning Echo's timezone* that the app shows and the schedule
is rebuilt from. Moving `alarmTime` alone leaves those stale, so
`reschedule`/`snooze` rewrite them using that Echo's own `timeZoneId` from
`echos preferences`; the `tz` field in the output says which clock was used, and
falls back to `UTC` (visibly) when the preference can't be read.

**Pause ≠ delete, and a paused alarm keeps its schedule.** `pause` sets
`status: OFF`, leaving the alarm (and its recurrence) in the list; `resume` puts
it back. `snooze` defaults to Amazon's own **9 minutes** and measures from the
alarm's time when that is still ahead, or from *now* when it has already fired.

> **Timers cannot be rescheduled or snoozed.** A timer counts down via
> `remainingTime` from the moment it was set and has no `alarmTime` to move, so
> the CLI refuses locally (before any write) and tells you to delete and
> recreate it.

> **The PUT can't tell you it worked, so the CLI re-reads.** `set_notifications`
> is wrapped in alexapy's `_catch_all_exceptions`: a rejected request and an
> accepted one both come back empty. Every edit therefore re-reads the
> notification afterwards and reports `ok` from what Amazon actually holds —
> and `ok: null` (with a note) when Amazon throttled the verify read, because
> "could not check" is not "did not work".

Ambiguity is refused, never guessed: two alarms sharing the label *"Wake up"*
abort with both ids so you can pick one, the same rule `devices rename` follows.

### Account & device introspection

```bash
cli-anything-alexa auth whoami --json      # customer id / name / email / Prime Music
cli-anything-alexa echos preferences       # timezone + locale + units per Echo
cli-anything-alexa echos wifi "Kitchen Echo"
```

`auth status` checks the **cookie**; `auth whoami` checks that the cookie still
buys an **account** (`/api/users/me`) and exits non-zero when it doesn't — the
sharper signal when a rotated Home Assistant cookie has gone stale underneath
you. `echos preferences` is also the read that explains a notification edit's
`tz`.

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

**Where the cookie lives** is resolved once, deterministically, by this
precedence: `--cookie-dir <path>` flag → `CLI_ALEXA_COOKIE_DIR` env →
`$HOME/.config/cli-anything-alexa` (only when `$HOME` is a real directory) → a
stable `/tmp/cli-anything-alexa` fallback. The fallback matters in containers
where `$HOME` is unset or `/`: without it a write (`import-pickle`) and a later
read (`auth status`) could disagree on the directory. With `--cookie-dir`/env
set the CLI reads the cookie **in place** at `<dir>/.storage/alexa_media.<email>.pickle`
(HA's layout) and never copies into it.

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

The tests cover the **pure logic** — appliance-id → entity parsing, whitelist
filtering / prune planning, table formatting, notification payload builders +
edit planning (id/label resolution, whole-record diffs, local wall-clock
recomputation, snooze arithmetic),
device-group GraphQL variables builders / name-normalization / lookup /
entity→endpoint resolution, smart-home capability-state decoding, sequence /
sound / skill-id normalisation, activity-feed flattening, bluetooth address
canonicalisation and proxy-URL formatting — plus the live wrappers against a fake
`AlexaAPI` and every CLI command path (including the **dry-run-by-default**
contract on all mutating commands). No `alexapy` traffic and no live account.

```bash
python3 -m pytest tests --cov=cli_anything --cov-report=term-missing
```

1329 tests, 90% statement/branch coverage; CI fails the build under 87%.

## License

MIT — see [LICENSE](LICENSE).
