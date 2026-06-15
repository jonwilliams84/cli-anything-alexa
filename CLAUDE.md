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
- **Python version, precisely:** a fresh proxy/scripted login pickles its
  cookie on the user's own Python and unpickles fine on that same Python — so
  **3.10+ is enough for normal use**. The `partitioned` Morsel attr (added to
  `http.cookies.Morsel` in 3.14) only breaks unpickle when reading a pickle
  written on a *newer* Python — i.e. **importing HA's 3.14-written pickle on
  ≤3.13** raises `CookieError: Invalid attribute 'partitioned'`. So 3.14 is
  required ONLY for `import-pickle` from a 3.14 source. CLI imports/tests run on
  any 3.10+.
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
