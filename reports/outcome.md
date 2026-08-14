# Refine Outcome — Amazon Kids (child mode)

## Summary

One coherent pass over the **parental-controls surface**, which was the top item
on the previous pass's "not covered" list: all five of alexapy's Amazon Kids
calls were unwrapped, so the harness could neither see nor change whether an
Echo was in kids mode.

New module `core/kids.py` + a `kids` command group (4 commands) wrap the lot:

| Command | alexapy call(s) | Notes |
| --- | --- | --- |
| `kids profiles` | `get_child_profiles` | household child profiles — name, age, `directedId` |
| `kids status [<device>]` | `get_child_mode` + `get_device_child` | no argument = every Echo; a name/serial = one |
| `kids enable <device> --child <name\|id>` | `enable_child_mode` | dry-run + `--yes`, **verified** |
| `kids disable <device>` | `disable_child_mode` | dry-run + `--yes`, **verified** |

Tests: **1108 → 1187** (+79). Coverage: **88% → 89%**, `core/kids.py` at
**100%**. CI `--cov-fail-under` raised 85 → **86**. `alexapy` `AlexaAPI` methods
actually invoked: 39 → **44** of 58 public ones (+`get_child_profiles`,
`get_child_mode`, `get_device_child`, `enable_child_mode`, `disable_child_mode`).

## 1. The finding that shaped the module: a kids write reports NOTHING

`enable_child_mode` and `disable_child_mode` are declared `-> None` **and**
wrapped in alexapy's `_catch_all_exceptions`, which converts a failed request
into a quiet `None` return. Success and rejection are therefore identical at the
call site — "it didn't raise" carries no information.

It is worse than a plain missing return value, because the assign is the **one
mutating call in this harness that does not use `session.csrf_header`**:

* it rides the *localized parent-dashboard host* (`parents.amazon.co.uk`,
  `eltern.amazon.de` — alexapy's `_parent_dashboard_subdomain`), not the web host;
* it authenticates with `ft-panda-csrf-token` echoed into `x-amzn-csrf`, seeded
  by GETting the onboarding page first;
* and if that cookie is absent alexapy logs at **debug** and posts anyway.

So the most likely failure mode — a missing dashboard csrf — is completely
silent. Both writes therefore call `read_state` afterwards and report `ok` from
the state Amazon **actually holds**, not from the absence of an exception. This
is the same "verify, don't assume" rule `devices delete --verify` follows for
native re-sync, and it is now written into CLAUDE.md as the pattern for any
future write whose API returns no result.

## 2. "Unknown" is not "off"

`get_child_mode` returns `None` when the state could not be read (unsupported
device, changed payload) — a different answer from `False`. `status_row` keeps
`None` as `None`, so `kids status` leaves the column blank rather than reporting
an unreadable speaker as "kids mode is off". `disable` likewise sets
`ok = (enabled is False)`, so an unreadable verify never reads as success. Three
tests pin this distinction specifically.

## 3. Safety decisions

* **`enable`/`disable` require an explicit device.** Every other device-bound
  command in the harness (`media`, `echos connect`, `push`) defaults to the first
  online Echo. Kids mode changes what a speaker *will do* — kid-safe content,
  restricted purchasing and calling — so silently applying it to whichever
  speaker happened to answer first is the wrong default. The target is a required
  argument; a CLI test asserts both commands refuse without one.
* **An unknown child is refused locally**, listing the known profiles. Amazon
  rejects an unknown `childDirectedId` without a message reaching the caller, so
  the alternative is a silent no-op — the same trap `echos connect` has with
  unpaired addresses.
* **Ambiguity aborts**, following the harness rule: siblings really can share a
  first name, so >1 match lists the `directedId`s to pick from. The exact-name
  tier resolves `Sam` vs `sam` first, so only a genuine collision aborts.
* **Dry-run by default + `--yes`**, with the target and pending action echoed
  verbatim in the preview.

## 4. Tests

| File | Tests | Covers |
| --- | --- | --- |
| `tests/test_kids.py` | 53 | the pure layer (profile flattening incl. dropping adults from a raw household payload and keeping role-less pre-filtered rows, name normalisation, 3-tier child resolution + precedence, the not-found message, status rows from both a raw record and a `DeviceRef`) and every live wrapper against a fake `AlexaAPI` — that `enable` posts the **resolved `directedId`**, that both writes **re-read to verify**, that a failed/unreadable verify reports `ok: false`, and that every refusal path never writes |
| `tests/test_cli_kids_paths.py` | 26 | the `kids` CLI paths: the dry-run contract on both mutations (preview, no network without `--yes`, `--yes` reaches the core function with the right arguments), that the target Echo is **named, never implicit**, that `enable` refuses a missing device/child, `status` routing (no arg → `status_all`, arg → `device_status`), null-not-off in the JSON, and that the group is reachable with help on every subcommand |

Every assertion is on observable behaviour — exit code, JSON on stdout, which
core coroutine was called with what — never on source text. Two of the tests I
first wrote were themselves wrong (they treated `Sam`/`sam` as ambiguous when the
exact-name tier correctly disambiguates); the fixtures were corrected rather than
the resolver loosened, and the precedence is now pinned by its own test.

Module coverage after: `core/kids.py` **100%** (statements *and* branches),
`alexa_cli.py` 73% → **74%**.

## 5. Docs

* `README.md` — 4 table rows plus an "Amazon Kids (child mode)" section covering
  the verify rule, the parent-dashboard host/token, and the unknown-≠-off trap.
* `cli_anything/alexa/README.md` — same rows plus a matching section.
* `CLAUDE.md` (SOP) — `kids.py` added to the Layout, a new gotcha entry for the
  reports-nothing/verify rule + the non-standard csrf path + the required
  explicit device, and a new entry in **Verified → assumptions worth checking
  live** (does the `ft-panda-csrf-token` bootstrap really succeed, and is
  `get_child_mode` immediately consistent after an assign, or does the verify
  need a retry).
* `skills/SKILL.md` — the agent-facing command list, flagging that `ok` comes
  from the re-read and that a blank `kids` means unreadable.

## Gates

| Gate | Result |
| --- | --- |
| `pytest tests` | **1187 passed**, 0 failed |
| `--cov-fail-under=86` | **89%** (raised from 85) |
| `ruff check cli_anything/` | clean |
| `ruff format --check cli_anything/` | clean |
| `bandit -r cli_anything/ -ll` | 0 findings |

No regressions: all 1108 pre-existing tests still pass, and no command was
changed or removed.

## Not covered (next refine pass)

* **Diagnostics reads**, the natural next cluster: `get_authentication` (a
  `auth whoami` — customer id/name/email + Prime-music entitlement), `ping` (a
  cheap probe, would make a good `auth status --ping`), `get_device_preferences`
  and the device-bound `get_wifi_details`.
* `get_devices_gql` — the GraphQL device list, potentially a richer `echos list`.
* `set_background` (Echo Show wallpaper), `set_notifications` (editing an
  existing alarm/timer/reminder rather than add/delete).
* `find_wake_word` / `force_logout` — duplicate or no-op, deliberately skipped.

Still true, and now the single biggest gap in confidence: **nothing in the
harness has had a mutation executed against a real account.** The kids surface
adds two more to that list — see CLAUDE.md's Verified section, where
`kids profiles` is flagged as the safe read to try first.
