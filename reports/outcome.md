# Refine Outcome — Echo runtime control (media / voice / state)

## Summary

Expanded the harness from **~12 of alexapy's 57 `AlexaAPI` methods** to ~27, by
adding the entire **device-bound Echo runtime surface** (media transport, volume,
shuffle/repeat, play-music, player state, TTS) plus three static state reads
(bluetooth, wake words, DND status).

While inventorying that surface a **latent correctness bug** was found and fixed:
every device-bound call already in the CLI (`announce`, `dnd`, `routines run`)
would have raised `AttributeError` from inside alexapy on a live account.

Tests: **429 → 632** (+203). Coverage: **81% → 84%**; CI `--cov-fail-under`
raised 79 → 82.

## 1. Correctness fix — `core/device_ref.py`

`AlexaAPI.get_devices()` returns plain JSON **dicts**. Every alexapy *instance*
method dereferences its target as **attributes** off `self._device`:

| attribute | read by |
| --- | --- |
| `device_serial_number` | `set_media`, `set_dnd_state`, `stop`, `process_targets`, bluetooth, `run_routine` |
| `_device_type` | same |
| `_device_family` | `process_targets` (WHA whole-home-audio fan-out) |
| `_cluster_members` | `process_targets` |
| `_locale` | `send_announcement`, `send_tts`, `run_routine` |

alexapy is written against Home Assistant's `AlexaClient` entity object, which
has those attributes. The harness was passing the raw dict, so the first
attribute access raises `AttributeError` — and alexapy's `_catch_all_exceptions`
decorator only converts connection/login errors and `raise`s everything else, so
it would have surfaced as a raw traceback rather than a friendly message.

This was invisible to the project's live validation because that run was
**read-only** (`CLAUDE.md` "Verified": *"No mutations executed"*), and the bug
only fires on a device-bound call.

`DeviceRef` is a pure adapter performing that translation. It also refuses a
record with no `serialNumber` — such a device cannot be addressed at all, so
failing loudly beats sending `deviceSerialNumber: null` to Amazon. All three
existing call sites now go through it.

`tests/test_device_ref.py` pins the attribute list as an explicit **contract
test**, so a future alexapy change fails a unit test instead of a live call.

## 2. New domain — `core/media.py` + the `media` command group

| Command | alexapy |
| --- | --- |
| `media status [<device>]` | `get_state` |
| `media play\|pause\|next\|previous\|forward\|rewind` | `play`/`pause`/… |
| `media stop [--all]` | `stop(all_devices=)` |
| `media volume --level 0-100` | `set_volume` |
| `media shuffle\|repeat --state on\|off` | `shuffle`/`repeat` |
| `media play-music <phrase> [--provider]` | `play_music` |

Design notes worth keeping:

* **Volume is a fraction, not a percentage.** alexapy multiplies by 100, so it
  wants 0.0–1.0. The CLI takes the human 0–100 and converts once in
  `normalize_volume`, which also rejects NaN/inf — those slip past a naive
  `0 <= v <= 100` check. Validation runs *before* `_login`, so a bad number fails
  identically with and without `--yes`.
* **`player_row` is defensive by design.** An idle Echo returns `playerInfo: {}`
  or a bare `{}`; every lookup degrades to `None` and the key set is constant so
  the rendered table stays aligned across devices.
* **`stop` is deliberately not in `TRANSPORT_COMMANDS`** — it goes through the
  sequence API and takes `all_devices`, unlike the zero-arg `/api/np/command`
  verbs. A test asserts that separation.

## 3. `speak` — `send_tts`

Sibling of `announce`, but distinct: `send_announcement` plays Alexa's chime and
fans out to all devices; `send_tts` is silent-prefix and single-speaker. alexapy
documents TTS `targets` as **non-functional** (Amazon ignores it), so `speak`
binds `AlexaAPI` to the requested device instead of passing targets.

## 4. Read-only state — `echos bluetooth` / `wake-words` / `dnd`

Three static endpoints with inconsistent envelopes (alexapy pre-unwraps
`wakeWords` but returns the full document for bluetooth and DND). `_unwrap`
accepts either shape plus `None`. All rows join `serialNumber → accountName` so
output is readable rather than a wall of serials. An Echo with nothing paired
still gets a row — omitting it would look like the device was missed rather than
empty.

## 5. Tests (+203)

| File | Tests | Covers |
| --- | --- | --- |
| `tests/test_device_ref.py` | 25 | the alexapy attribute contract, field translation, locale fallback, WHA clusters, record isolation, and that all three call sites bind a `DeviceRef` |
| `tests/test_media.py` | 67 | volume conversion incl. NaN/inf, provider normalisation, `player_row` on idle/partial/garbage payloads, device resolution, every transport verb, stop/`--all`, play-music arg order |
| `tests/test_echo_state_reads.py` | 31 | both payload envelopes for all three reads, name joins, empty/non-dict entries, and `control.speak` |
| `tests/test_cli_media_paths.py` | 80 | the dry-run contract across **every** new mutating command (parametrised, so a future command that forgets `--yes` fails), argument validation, execution dispatch, read-only paths |

Two behavioural contracts are asserted *generically* rather than per-command, so
they cannot rot: every new mutating command previews without `--yes`, and none of
them reaches a core coroutine in dry-run.

`tests/test_cli_media_paths.py` uses a `_stub_run` helper that **closes** the
coroutine handed to the patched `_run`, and `_stub_core` which substitutes a
plain `MagicMock` (since `patch.object` auto-detects coroutine functions and
installs an `AsyncMock`, whose un-awaited return value warns exactly like the
real thing). Result: the new suites add zero `RuntimeWarning` noise.

### Fixture correction

Three `tests/test_coverage_gaps.py` routine fixtures had device records with no
`serialNumber`. Real `get_devices()` always returns one; `DeviceRef` correctly
refuses to target an unaddressable device, so the fixtures were made realistic
rather than the check weakened.

## 6. Docs

`README.md`, the packaged `cli_anything/alexa/README.md`, `CLAUDE.md` (the SOP)
and the packaged `skills/SKILL.md` all updated: new command tables, a "Media &
voice on Echo devices" section, the `announce` vs `speak` distinction, and the
`DeviceRef` gotcha written up alongside the existing GraphQL-array and DACS ones.

`CLAUDE.md`'s **Verified** section now explicitly records that the device-bound
surface is *not* live-validated — that gap is what let the `DeviceRef` bug
survive, so it is documented as the next live check to run.

## Gates

| Gate | Result |
| --- | --- |
| `pytest tests` | 632 passed |
| `--cov-fail-under=82` | 83.79% |
| `ruff check cli_anything/` | clean |
| `ruff format --check cli_anything/` | clean |
| `bandit -r cli_anything/ -ll` | 0 findings |

## Not covered (next refine pass)

Alexa Guard (`get`/`set_guard_state`, `get_guard_details`), activity history
(`get_customer_history_records`, `get_activities`, `clear_history`),
`set_light_state`/`get_entity_state`, `run_skill`/`run_custom`/`send_sequence`,
`set_background`, `send_mobilepush`/`send_dropin_notification`,
`set_bluetooth`/`disconnect_bluetooth` (writes), `get_device_preferences`,
`get_network_details`.
