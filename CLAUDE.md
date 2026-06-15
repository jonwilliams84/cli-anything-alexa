# cli-anything-alexa

A `click`-based CLI + interactive REPL for managing **Amazon Alexa** over the
unofficial Alexa web API (the app's private endpoints), built on
[`alexapy`](https://pypi.org/project/alexapy/). Sibling of
`cli-anything-homeassistant` / `cli-anything-zigbee2mqtt` — same profile/JSON/
REPL pattern. Python 3.10+ for the CLI; live calls need Python 3.14+ at runtime
(cookie `partitioned` attr). Every command supports `--json`.

## Layout
- `cli_anything/alexa/alexa_cli.py` — the Click CLI + REPL; all command wiring. Entry point `main`.
- `cli_anything/alexa/core/` — one module per surface:
  - `appliances.py` — **pure** logic: applianceId→entity parsing, whitelist load, prune planning. No deps. Unit-tested.
  - `formatting.py` — **pure** table/cell rendering. Unit-tested.
  - `session.py` — `alexapy.AlexaLogin` wrapper: cookie import, load/validate, fresh login, csrf header. `alexapy` imported lazily so the CLI loads without it.
  - `devices.py` — appliance list + raw `DELETE /api/phoenix/appliance/<id>`.
  - `devices_meta.py` — physical Echo devices (announce/dnd/routine targets).
  - `notifications.py` — alarms/timers/reminders: list + pure payload builders + POST/PUT/DELETE.
  - `routines.py` — behaviors list + trigger (device-bound `run_routine`).
  - `control.py` — announce + dnd.
  - `groups.py` — device-groups (rooms) over **GraphQL** `/nexus/v1/graphql`: list/create/add/remove/set/delete. Pure variables-builders + name-normalize/lookup + entity→endpoint resolution are unit-tested; network goes via `AlexaAPI._static_request`.
  - `project.py` — local profile (`~/.config/cli-anything-alexa/config.json`).
- `cli_anything/alexa/utils/repl_skin.py` — shared cli-anything REPL skin.
- `cli_anything/alexa/skills/SKILL.md` — packaged agent skill manifest.
- `tests/` — pytest, **pure logic only** (no alexapy / no live account).

## Build / test / run
```bash
pip install -e .                                    # console script
pip install -e '.[test]' && python3 -m pytest tests/ -v
cli-anything-alexa --email you@x.com config save
cli-anything-alexa auth import-pickle /config/.storage/alexa_media.you@x.com.pickle --email you@x.com
cli-anything-alexa auth status
cli-anything-alexa devices list --json
```

## Conventions / gotchas
- **alexapy is async**; the CLI wraps each call in `session.run_async` (`asyncio.run`).
  Device-level ops (announce/dnd/run_routine) are alexapy *instance* methods
  (`AlexaAPI(device, login)`); graph/notification reads are *static* (`login` arg).
- **Auth = cookie reuse.** The HA `alexa_media` pickle is reusable verbatim;
  `auth import-pickle` copies it to `~/.config/cli-anything-alexa/alexa_media.<email>.pickle`.
- **Python 3.14 for live calls** — the cookie pickle's `partitioned` Morsel
  attr `KeyError`s on ≤3.13. CLI imports/tests run fine on 3.12.
- **Mutations are dry-run-by-default + require `--yes`** (prune, delete, run,
  notifications add/delete, announce, dnd). Mirror this when adding commands.
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

## Verified
Live read-only validation (2026-06-15, amazon.co.uk account, HA cookie reused):
`auth status` → logged_in=true; `devices list` → 178 appliances / 108 HA-sourced,
entity mapping correct. No mutations were executed — they are built but user-gated.
