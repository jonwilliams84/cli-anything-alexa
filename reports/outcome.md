# Refine Outcome — Echo bluetooth + app push, and tests for the behaviour/history surfaces

## Summary

Two things, one coherent pass over the **Echo device surface**:

1. **Closed the test gap on the last pass's code.** `core/sequences.py` (the
   `run` behaviour surface) and `core/activity.py` (voice history) shipped with
   **no tests at all** — 19% and 11% covered — which had taken the repo *below*
   its own CI gate (78% actual vs `--cov-fail-under=82`, i.e. `main` was red).
   Both are now at **100%**, and writing those tests found **two real bugs**
   (below).
2. **Added the next capability set from that pass's "not covered" list:** Echo
   **bluetooth writes** (`set_bluetooth` / `disconnect_bluetooth`) and **Alexa-app
   push** (`send_mobilepush` / `send_dropin_notification`) — 4 new commands.

Tests: **750 → 1108** (+358). Coverage: **78% → 88%**. CI `--cov-fail-under`
raised 82 → **85**. `alexapy` `AlexaAPI` methods actually invoked: 35 → **39** of 58 public ones (+`set_bluetooth`, `disconnect_bluetooth`, `send_mobilepush`, `send_dropin_notification`).

## 1. Bugs found by the new tests (`core/activity.py`)

| Bug | Symptom | Fix |
| --- | --- | --- |
| `activity_rows` assumed dict-or-list | `(payload or {}).get(...)` raised `AttributeError` on a **string** payload — exactly what an Alexa error body arrives as, and the docstring promised tolerance | explicit `isinstance` branches; anything else yields **no rows** |
| `normalize_limit` truncated floats | `--limit 1.5` → silently asked Amazon for **1** record, while the string `"1.5"` already raised | a non-integral float is now refused with the same message |

Both are the "junk in the payload must not cost the other 19 rows" class this
module documents; neither was reachable from a read-only live check.

## 2. New: Echo bluetooth control — `core/bluetooth.py`

`echos bluetooth` could only *read* pairings. Two device-bound calls now act on
them, with the pure layer (100% covered) doing the thinking:

| Command | Call | Notes |
| --- | --- | --- |
| `echos pairings [<device>]` | `get_bluetooth` | per-**Echo** view (the account-wide `echos bluetooth` is unchanged), exposing the `address` the write needs |
| `echos connect <name\|mac> [--device ...]` | `set_bluetooth` | dry-run + `--yes` |
| `echos disconnect [--device ...]` | `disconnect_bluetooth` | dry-run + `--yes` |

Three findings baked into the module and its docs:

* **`pair-sink` connects, it does not pair.** The initial handshake (pairing
  mode + code confirmation) is Alexa-app/voice-only. Worse, Amazon answers
  `pair-sink` for an unknown address with a bare `200` and does nothing — a
  silent no-op. So `connect` resolves the target against that Echo's own
  `pairedDeviceList` first and refuses locally, **listing what is paired**.
* **The address is Amazon's string, verbatim.** `normalize_mac` exists only so
  `aa-bb-cc-dd-ee-ff`, `AABBCCDDEEFF` and `AA:BB:CC:DD:EE:FF` compare equal when
  *finding* a pairing; what gets posted is the `address` the API reported (what
  Home Assistant's `alexa_media` does too). Not every Alexa sink id is a plain
  MAC, so a non-MAC target is still matched by name.
* **Disconnect is all-or-nothing.** There is no per-sink endpoint, so the result
  row says `disconnected: "all"` rather than implying a single target.

Ambiguity follows the harness rule: two sinks sharing a friendly name → abort and
list the addresses (same shape as `devices rename`).

## 3. New: `push` — the silent notification channel

`announce` chimes the house and `speak` talks on one speaker; neither is usable
from a script at 3am. `push` (`control.push`) sends the message to the **Alexa
app** instead:

```bash
cli-anything-alexa push "the washing machine finished" --yes
cli-anything-alexa push "check the nursery" --dropin --device "Nursery Echo" --yes
```

`--dropin` swaps `send_mobilepush` for `send_dropin_notification` (whose
notification offers to drop in on the resolved Echo). Both ride the behaviours
API and are therefore still **device-bound**, so they resolve an Echo through
`DeviceRef` even though nothing plays on it — the one non-obvious thing about
them. The default title is ours (`cli-anything-alexa`), not alexapy's
developer-facing `"AlexaAPI Message"`.

## 4. Tests

| File | Tests | Covers |
| --- | --- | --- |
| `tests/test_sequences.py` | 82 | every normaliser (text / sequence alias / soundbank alias / skill id / queue delay incl. NaN-inf-negative), the catalogs, and all four live ops against a fake `AlexaAPI` — including **`queue_delay` omitted when unspecified** (alexapy's per-call default must survive) and validate-before-network |
| `tests/test_activity.py` | 97 | tz-aware epoch-ms rendering, the query window (`--hours`), both feed flatteners incl. the JSON-encoded legacy `description`, noise/device/text filtering, partial-clear reporting, and every live wrapper's actual query parameters |
| `tests/test_bluetooth.py` | 93 | MAC canonicalisation, per-Echo pairing extraction from both payload shapes, 4-tier target resolution, the not-paired message, connect/disconnect/list against a fake `AlexaAPI`, plus `control.normalize_push`/`push` |
| `tests/test_cli_behavior_paths.py` | 56 | the `run` and `activity` CLI paths: dry-run-by-default on all four `run` verbs, validation *before* `_login` (identically with and without `--yes`), `--queue-delay` pass-through, `run catalog` needing no account, and `activity clear`'s irreversible guard |
| `tests/test_cli_bluetooth_push_paths.py` | 30 | the new commands' dry-run contract, argument plumbing, and that `echos bluetooth` still behaves (refine adds, never removes) |

Every assertion is on observable behaviour — exit code, JSON on stdout, which
core coroutine was called with what — never on source text.

Module coverage after: `activity.py` 11% → **100%**, `sequences.py` 19% →
**100%**, `control.py` 100%, `bluetooth.py` **100%**, `alexa_cli.py` 66% → **73%**.

## 5. Docs

The previous pass shipped `smarthome`/`guard`, `run` and `activity` **undocumented**
— none of the four docs mentioned them. All four are now current:

* `README.md` — table rows for `devices state/on/off/light`, `guard`, `run *`,
  `activity *`, `push`, `echos pairings/connect/disconnect`, plus new sections
  "Smart-home state & control", "Voice commands & behaviours — `run`", "Voice
  history — `activity`", the push-vs-announce-vs-speak distinction and the
  bluetooth connect-≠-pair rule.
* `cli_anything/alexa/README.md` — same commands, plus "Voice commands,
  behaviours & history" and "Bluetooth on an Echo".
* `CLAUDE.md` (SOP) — `smarthome.py` / `sequences.py` / `activity.py` /
  `bluetooth.py` added to the Layout, and four new gotcha entries (run-command as
  the escape hatch + the `queue_delay`-is-`None` rule, the two activity feeds and
  their epoch-ms/partial-clear traps, connect-≠-pair + all-or-nothing disconnect,
  push being silent yet device-bound). **Verified** now lists the per-surface
  assumptions still to check against a live account.
* `skills/SKILL.md` — the agent-facing command list covers all of the above.

## Gates

| Gate | Result |
| --- | --- |
| `pytest tests` | 1108 passed |
| `--cov-fail-under=85` | 88% (was failing at 78% vs 82) |
| `ruff check cli_anything/` | clean |
| `ruff format --check cli_anything/` | clean |
| `bandit -r cli_anything/ -ll` | 0 findings |

## Not covered (next refine pass)

`set_background` (Echo Show wallpaper), `get_device_preferences`,
`get_wifi_details`, child profiles / child mode
(`get_child_profiles`, `enable_child_mode`, `disable_child_mode`,
`get_child_mode`), `find_wake_word`, `force_logout`, `ping` (a cheap
connectivity probe that would make a good `auth status --ping`), and
`get_devices_gql` (the GraphQL device list, potentially a richer `echos list`).

Nothing in the harness has had a **mutation executed against a real account** —
see CLAUDE.md's Verified section for the per-surface list of assumptions that
only a live run can settle.
