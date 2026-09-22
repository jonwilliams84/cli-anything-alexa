"""Smart-home **state reads** and **actuation** over the phoenix state API.

Until now the harness could *inventory* the smart-home graph (list / rename /
group / delete endpoints) but could not read a device's current state or turn
anything on — the two things a user actually wants after finding a device.  This
module closes that gap over ``/api/phoenix/state``:

* **read** — ``AlexaAPI.get_entity_state`` (``POST``, ``stateRequests``) returns
  the live capability values (power, brightness, colour, temperature, …).
* **control** — ``AlexaAPI.set_light_state`` (``PUT``, ``controlRequests``)
  issues ``turnOn``/``turnOff``/``setBrightness``/``setColor``/
  ``setColorTemperature``.  Despite the name it is the *generic* phoenix control
  call: a plug/switch is just a light with no brightness or colour.
* **guard** — the same ``controlRequests`` shape with
  ``action=controlSecurityPanel`` (``AlexaAPI.static_set_guard_state``), read
  back through the appliance-typed state read.

Three things are worth knowing before extending this module:

* **``entityId`` is NOT ``applianceId`` and NOT the endpoint id.**  The phoenix
  state API addresses a device by ``legacyAppliance.entityId`` (an opaque
  ``ENTITY``-typed id).  It is a *fourth* id alongside the three
  ``endpoints.py`` documents, so the canonical ``endpoints`` query now selects
  it and :func:`entity_ref` refuses — with a caller-facing message — a record
  that has none, rather than letting Alexa answer a confident ``200`` for an id
  it silently ignored.
* **``capabilityStates`` are JSON-encoded *strings***, not objects: the payload
  nests ``deviceStates[].capabilityStates`` as a list of ``str`` each holding
  one ``{"namespace":…,"name":…,"value":…}`` document.  :func:`state_rows`
  accepts both that and an already-decoded dict (and skips undecodable junk)
  so a shape change upstream degrades to a missing row, never a traceback.
* **Colour names are a closed vocabulary in snake_case.**  Alexa rejects
  anything else, and the rejection arrives as a generic error, so
  :func:`normalize_color` / :func:`normalize_color_temperature` validate
  locally against the documented lists and name the alternatives.

Everything above the "live operations" divider is pure and unit-tested; only the
thin ``async def`` wrappers touch the network.
"""

from __future__ import annotations

import json
from typing import Any

#: Colour names accepted by ``setColor`` (Alexa's documented palette, snake_case).
COLOR_NAMES: tuple[str, ...] = (
    "white",
    "red",
    "crimson",
    "salmon",
    "orange",
    "gold",
    "yellow",
    "green",
    "turquoise",
    "cyan",
    "sky_blue",
    "blue",
    "purple",
    "magenta",
    "pink",
    "lavender",
)

#: Colour *temperature* names accepted by ``setColorTemperature`` (snake_case).
COLOR_TEMPERATURE_NAMES: tuple[str, ...] = (
    "warm_white",
    "soft_white",
    "white",
    "daylight_white",
    "cool_white",
)

#: Human guard verb → the ``armState`` Amazon expects.  There is no third
#: state: "home" (``ARMED_STAY``) *is* how Guard is stood down — a literal
#: "DISARMED" is rejected, which is why this mapping is explicit.
GUARD_STATES: dict[str, str] = {
    "away": "ARMED_AWAY",
    "home": "ARMED_STAY",
}

#: ``applianceTypes`` marker identifying the Alexa Guard (RedRock) panel.
GUARD_APPLIANCE_TYPE = "SECURITY_PANEL"


# ── pure helpers: value normalisation ────────────────────────────────────


def normalize_brightness(value: float | int | str) -> int:
    """Validate a 0–100 brightness and return it as an int (pure).

    ``set_light_state`` *silently drops* a brightness outside 0–100 (it guards
    with ``if 0 <= brightness <= 100``), so an out-of-range value would look
    like a successful no-op.  Failing loudly here — including on NaN/inf, which
    slip past a naive range check just as they do for volume — keeps the CLI
    honest.  Raises ``ValueError`` with a caller-facing message.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"brightness must be a number between 0 and 100, got {value!r}") from None
    if number != number or number in (float("inf"), float("-inf")):  # NaN / inf
        raise ValueError(f"brightness must be a number between 0 and 100, got {value!r}")
    if not 0 <= number <= 100:
        raise ValueError(f"brightness must be between 0 and 100, got {value!r}")
    # ``round`` of a float already returns an int (RUF046) — no cast needed.
    return round(number)


def _snake(name: str) -> str:
    """ "Sky Blue" / "sky-blue" → "sky_blue" (pure)."""
    cleaned = (name or "").strip().lower().replace("-", "_").replace(" ", "_")
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned


def normalize_color(name: str) -> str:
    """Normalise + validate a ``setColor`` colour name (pure).

    Accepts human spellings ("Sky Blue", "sky-blue") and returns the snake_case
    form.  Unknown colours raise ``ValueError`` listing the palette instead of
    letting Amazon reject the whole control request opaquely.
    """
    value = _snake(name)
    if not value:
        raise ValueError("a colour name is required")
    if value not in COLOR_NAMES:
        raise ValueError(f"unknown colour {name!r}; expected one of: {', '.join(COLOR_NAMES)}")
    return value


def normalize_color_temperature(name: str) -> str:
    """Normalise + validate a ``setColorTemperature`` name (pure)."""
    value = _snake(name)
    if not value:
        raise ValueError("a colour temperature name is required")
    if value not in COLOR_TEMPERATURE_NAMES:
        raise ValueError(
            f"unknown colour temperature {name!r}; "
            f"expected one of: {', '.join(COLOR_TEMPERATURE_NAMES)}"
        )
    return value


def normalize_guard_state(state: str) -> str:
    """Map ``away``/``home`` (or a raw ``ARMED_*``) to Amazon's ``armState`` (pure)."""
    value = _snake(state)
    if value in GUARD_STATES:
        return GUARD_STATES[value]
    upper = value.upper()
    if upper in GUARD_STATES.values():
        return upper
    raise ValueError(f"unknown guard state {state!r}; expected one of: away, home")


def plan_light_change(
    *,
    power: bool | None = None,
    brightness: float | int | str | None = None,
    color: str | None = None,
    color_temperature: str | None = None,
) -> dict[str, Any]:
    """Validate + describe a pending light change (pure).

    Returns ``{"power": bool|None, "brightness": int|None, "color": str|None,
    "color_temperature": str|None, "actions": [str, ...]}`` — ``actions`` is the
    human preview the dry-run prints, in the order Alexa will apply them.

    Raises ``ValueError`` when nothing was asked for (an empty control request
    returns ``200`` having done nothing) or when ``color`` and
    ``color_temperature`` are combined: ``set_light_state`` would send both and
    the device keeps whichever it applied last, so the outcome is undefined.
    """
    if color and color_temperature:
        raise ValueError(
            "--color and --color-temp are mutually exclusive "
            "(Alexa applies both and the result is whichever landed last)"
        )
    level = None if brightness is None else normalize_brightness(brightness)
    color_value = normalize_color(color) if color else None
    temp_value = normalize_color_temperature(color_temperature) if color_temperature else None
    if power is None and level is None and color_value is None and temp_value is None:
        raise ValueError(
            "nothing to change — pass --on/--off, --brightness, --color or --color-temp"
        )
    actions: list[str] = []
    if power is not None:
        actions.append("turnOn" if power else "turnOff")
    if level is not None:
        actions.append(f"setBrightness={level}")
    if color_value is not None:
        actions.append(f"setColor={color_value}")
    if temp_value is not None:
        actions.append(f"setColorTemperature={temp_value}")
    return {
        "power": power,
        "brightness": level,
        "color": color_value,
        "color_temperature": temp_value,
        "actions": actions,
    }


# ── pure helpers: entity addressing ──────────────────────────────────────


def entity_ref(record: dict[str, Any]) -> str:
    """The phoenix ``entityId`` for an endpoint record (pure).

    Raises ``ValueError`` when the record carries no ``entityId``: the state API
    would accept the request and quietly do nothing, so refusing is the only
    honest answer.  The message names the device so the user can go look.
    """
    entity_id = (record or {}).get("entityId")
    if not entity_id:
        name = (record or {}).get("name") or (record or {}).get("applianceId") or "device"
        raise ValueError(
            f"{name!r} has no phoenix entityId — it cannot be read or controlled "
            "through the state API (try `discover` to re-sync it)"
        )
    return str(entity_id)


def entity_refs(records: list[dict[str, Any]]) -> list[str]:
    """Every usable ``entityId`` in a record list, skipping those without one (pure)."""
    return [str(r.get("entityId")) for r in records or [] if r.get("entityId")]


def name_by_entity(records: list[dict[str, Any]] | None) -> dict[str, str]:
    """``entityId`` → display name lookup, for labelling state rows (pure)."""
    return {
        str(r.get("entityId")): r.get("name")
        for r in records or []
        if r.get("entityId") and r.get("name")
    }


# ── pure helpers: state payload flattening ───────────────────────────────


def _decode_capability(raw: Any) -> dict[str, Any] | None:
    """One ``capabilityStates`` entry → dict, or None if undecodable (pure)."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return decoded if isinstance(decoded, dict) else None
    return None


def _short_namespace(namespace: Any) -> str | None:
    """``Alexa.PowerController`` → ``PowerController`` (pure)."""
    if not namespace:
        return None
    text = str(namespace)
    return text.split(".", 1)[1] if text.startswith("Alexa.") else text


def state_rows(
    payload: Any,
    records: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Flatten a ``/api/phoenix/state`` response to one row per capability (pure).

    ``records`` (endpoint records) is optional and only used to label each row
    with the device's display name.  Rows whose capability blob cannot be
    decoded are skipped rather than emitted half-built.
    """
    if not isinstance(payload, dict):
        return []
    names = name_by_entity(records)
    out: list[dict[str, Any]] = []
    for device_state in payload.get("deviceStates") or []:
        if not isinstance(device_state, dict):
            continue
        entity = device_state.get("entity") or {}
        entity_id = entity.get("entityId") if isinstance(entity, dict) else None
        for raw in device_state.get("capabilityStates") or []:
            cap = _decode_capability(raw)
            if cap is None:
                continue
            out.append(
                {
                    "name": names.get(str(entity_id)),
                    "capability": _short_namespace(cap.get("namespace")),
                    "property": cap.get("name"),
                    "value": cap.get("value"),
                    "entityId": entity_id,
                }
            )
    return out


def state_errors(payload: Any) -> list[dict[str, Any]]:
    """Flatten the ``errors`` half of a state response (pure).

    An unreachable or unsupported device does not fail the request — it comes
    back in ``errors`` while ``deviceStates`` stays silent about it.  Surfacing
    these is the difference between "off" and "we never heard back".
    """
    if not isinstance(payload, dict):
        return []
    out: list[dict[str, Any]] = []
    for err in payload.get("errors") or []:
        if not isinstance(err, dict):
            continue
        entity = err.get("entity") or {}
        out.append(
            {
                "entityId": entity.get("entityId") if isinstance(entity, dict) else None,
                "code": err.get("code") or err.get("errorCode"),
                "message": err.get("message") or err.get("description"),
            }
        )
    return out


def power_state(payload: Any, entity_id: str | None = None) -> str | None:
    """The ``powerState`` value (``ON``/``OFF``) from a state payload (pure).

    With ``entity_id`` the lookup is scoped to that device; without it, the
    first power row wins (the single-target case).  ``None`` when the device
    reported no power capability.
    """
    for row in state_rows(payload):
        if row.get("property") != "powerState":
            continue
        if entity_id and str(row.get("entityId")) != str(entity_id):
            continue
        value = row.get("value")
        return value if isinstance(value, str) else None
    return None


def find_guard(records: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """The Alexa Guard panel record, or None (pure).

    Guard is an appliance like any other: identified by ``SECURITY_PANEL`` in
    ``applianceTypes``, with a friendly-name fallback for the (older) accounts
    that report no types.
    """
    for r in records or []:
        types = r.get("applianceTypes") or []
        if isinstance(types, str):
            types = [types]
        if GUARD_APPLIANCE_TYPE in [str(t).upper() for t in types]:
            return r
    for r in records or []:
        if "guard" in (r.get("name") or "").strip().lower():
            return r
    return None


def guard_row(payload: Any, name: str | None = None) -> dict[str, Any]:
    """Flatten a Guard state read into a single row (pure).

    ``armState`` is reported through the generic capability list, so this reuses
    :func:`state_rows` and maps the raw ``ARMED_*`` back to the human verb the
    CLI accepts.
    """
    arm_state = None
    for row in state_rows(payload):
        if row.get("property") in ("armState", "securityPanelState"):
            value = row.get("value")
            arm_state = value if isinstance(value, str) else None
            break
    human = next((k for k, v in GUARD_STATES.items() if v == arm_state), None)
    return {"name": name, "armState": arm_state, "mode": human}


# ── locks (Alexa.LockController) ──────────────────────────────────────────

#: Human verb → the phoenix ``action`` Amazon expects for a lock. A lock write
#: is ONE controlRequest with a bare ``lock``/``unlock`` — the same request
#: family as ``turnOn``/``controlSecurityPanel`` (both already shipped in this
#: harness via alexapy's own builders).
LOCK_ACTIONS: dict[bool, str] = {True: "lock", False: "unlock"}

#: The values Amazon reports for ``Alexa.LockController.lockState``. JAMMED is a
#: real, reportable state — never map it to UNLOCKED.
LOCK_STATES = ("LOCKED", "UNLOCKED", "JAMMED")


def lock_action(lock: bool) -> str:
    """``True`` → ``lock``, ``False`` → ``unlock`` (pure)."""
    return LOCK_ACTIONS[bool(lock)]


def lock_state(payload: Any, entity_id: str | None = None) -> str | None:
    """The ``lockState`` value from a phoenix state read, or ``None`` (pure).

    ``None`` means "Amazon reported no lockState for this entity (yet)" — an
    unreachable device or a throttled read. Collapsing that into ``UNLOCKED``
    would report a silent write as a success, so the caller sees the ``None``.
    """
    for row in state_rows(payload):
        if row.get("property") != "lockState":
            continue
        if entity_id and row.get("entityId") not in (None, entity_id):
            continue
        value = row.get("value")
        return value if isinstance(value, str) else None
    return None


def lock_verify(payload: Any, entity_id: str, lock: bool) -> bool | None:
    """Three-valued verify of a lock write against a fresh state read (pure).

    ``True``/``False`` — Amazon holds (not) the state we asked for; JAMMED is a
    False. ``None`` — Amazon reported no lockState at all: the verify read was
    throttled or the device unreachable, which is "could not check", never a
    quiet failure. Mirrors the kids/notification write-verify semantics.
    """
    value = lock_state(payload, entity_id)
    if value is None:
        return None
    return value == ("LOCKED" if lock else "UNLOCKED")


# ── thermostat ───────────────────────────────────────────────────────────
#
# Thermostat control rides the same ``controlRequests`` PUT the light verbs
# and locks use — alexapy's ``set_light_state`` builder simply has no
# thermostat actions, so (like the 0.8.0 lock surface) the request is built
# locally and sent through ``AlexaAPI._static_request``.  The parameter shapes
# are the app's own:
#
#   {"action": "setTargetSetpoint",
#    "targetSetpoint": {"value": "21.0", "scale": "CELSIUS"}}
#   {"action": "adjustTargetTemperature",
#    "targetSetpointDelta": {"value": "-1.0", "scale": "CELSIUS"}}
#   {"action": "setMode", "mode": "HEAT"}

#: Human thermostat mode → the value ``Alexa.ThermostatController.setMode``
#: expects. CUSTOM exists in Alexa's vocabulary; ECO covers both the app's
#: "Ecô" toggle and HAVEN-style eco modes.
THERMOSTAT_MODES: dict[str, str] = {
    "off": "OFF",
    "heat": "HEAT",
    "cool": "COOL",
    "auto": "AUTO",
    "eco": "ECO",
    "custom": "CUSTOM",
}

#: Human scale spellings → what the ``targetSetpoint``/``targetSetpointDelta``
#: parameter expects. Alexa wants the uppercase word, not a symbol.
TEMPERATURE_SCALES: dict[str, str] = {
    "c": "CELSIUS",
    "celsius": "CELSIUS",
    "f": "FAHRENHEIT",
    "fahrenheit": "FAHRENHEIT",
}


def normalize_scale(scale: str | None) -> str:
    """Normalise + validate a temperature scale (pure)."""
    value = (scale or "").strip().lower()
    if not value:
        raise ValueError("a temperature scale is required (celsius or fahrenheit)")
    try:
        return TEMPERATURE_SCALES[value]
    except KeyError:
        raise ValueError(
            f"unknown temperature scale {scale!r}; "
            f"expected one of {sorted(set(TEMPERATURE_SCALES.values()))}"
        ) from None


def normalize_temperature(value: float | int | str | None) -> float:
    """Validate a temperature value (pure).

    Degrees, not a percentage — so the only hard limits are physical: NaN and
    infinity are refused, anything else is passed through as a float (Alexa
    itself rejects out-of-range setpoints for a given thermostat).
    """
    if value is None:
        raise ValueError("a temperature is required")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"invalid temperature {value!r}") from None
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(f"invalid temperature {value!r}")
    return number


def normalize_thermostat_mode(mode: str | None) -> str:
    """Normalise + validate a thermostat mode (pure)."""
    value = (mode or "").strip().lower()
    if not value:
        raise ValueError("a thermostat mode is required")
    try:
        return THERMOSTAT_MODES[value]
    except KeyError:
        raise ValueError(
            f"unknown thermostat mode {mode!r}; expected one of {sorted(THERMOSTAT_MODES)}"
        ) from None


def plan_thermostat_change(
    *,
    setpoint: float | int | str | None = None,
    adjust: float | int | str | None = None,
    mode: str | None = None,
    scale: str | None = "celsius",
) -> dict[str, Any]:
    """Validate a thermostat change into the request parameters (pure).

    ``--setpoint`` (absolute) and ``--adjust`` (relative) are mutually
    exclusive — Alexa would apply both and the result would be a surprise.
    At least one of setpoint / adjust / mode is required: a thermostat change
    that changes nothing is a bug, not a no-op. ``mode`` may ride along with
    either temperature verb (the app's "set to 21 and switch to heat").
    """
    scale_name = normalize_scale(scale)
    target = normalize_temperature(setpoint) if setpoint is not None else None
    delta = normalize_temperature(adjust) if adjust is not None else None
    if target is not None and delta is not None:
        raise ValueError("--setpoint and --adjust are mutually exclusive")
    if target is None and delta is None and mode is None:
        raise ValueError("nothing to change — give --setpoint, --adjust or --mode")
    actions: list[str] = []
    if target is not None:
        actions.append(f"setTargetSetpoint={target:g} {scale_name}")
    if delta is not None:
        actions.append(f"adjustTargetTemperature={delta:+g} {scale_name}")
    mode_value = normalize_thermostat_mode(mode) if mode is not None else None
    if mode_value is not None:
        actions.append(f"setMode={mode_value}")
    return {
        "targetSetpoint": (
            {"value": f"{target:.1f}", "scale": scale_name} if target is not None else None
        ),
        "targetSetpointDelta": (
            {"value": f"{delta:.1f}", "scale": scale_name} if delta is not None else None
        ),
        "mode": mode_value,
        "actions": actions,
    }


def thermostat_state(payload: Any, entity_id: str | None = None) -> dict[str, Any] | None:
    """The ``targetSetpoint`` read back from a phoenix state read (pure).

    Returns ``{"value": <float>, "scale": "CELSIUS"|"FAHRENHEIT"}`` — or
    ``None`` when Amazon reported no targetSetpoint for the entity (throttled
    read, unreachable device, or the device has no thermostat capability).
    ``None`` is "could not check", never a failed write.
    """
    for row in state_rows(payload):
        if row.get("property") != "targetSetpoint":
            continue
        if entity_id and row.get("entityId") not in (None, entity_id):
            continue
        value = row.get("value")
        if not isinstance(value, dict):
            continue
        try:
            number = float(value.get("value"))
        except (TypeError, ValueError):
            return None
        scale = value.get("scale")
        return {"value": number, "scale": scale if isinstance(scale, str) else None}
    return None


def thermostat_mode_state(payload: Any, entity_id: str | None = None) -> str | None:
    """The ``thermostatMode`` read back from a phoenix state read (pure)."""
    for row in state_rows(payload):
        if row.get("property") != "thermostatMode":
            continue
        if entity_id and row.get("entityId") not in (None, entity_id):
            continue
        value = row.get("value")
        return value if isinstance(value, str) else None
    return None


def _held_matches(held: dict[str, Any] | None, want: float, scale: str) -> bool | None:
    """Compare a held setpoint to the requested one, converting scale (pure).

    A thermostat that reports in the *other* scale is still a valid answer —
    69.8°F is 21°C — so the comparison converts rather than failing. Same-scale
    compares to the penny (±0.05°); cross-scale gets a half-degree of slack
    because a device holding "21.0°C" reports "69.8°F", not "70.0°F".
    ``None`` only when the read gave no usable value.
    """
    if held is None or held.get("value") is None:
        return None
    value = held["value"]
    held_scale = held.get("scale")
    tolerance = 0.05
    if held_scale == "CELSIUS" and scale == "FAHRENHEIT":
        value = value * 9 / 5 + 32
        tolerance = 0.5
    elif held_scale == "FAHRENHEIT" and scale == "CELSIUS":
        value = (value - 32) * 5 / 9
        tolerance = 0.5
    return abs(value - want) <= tolerance


def thermostat_verify(
    payload: Any,
    entity_id: str,
    *,
    setpoint: float | None = None,
    adjust: float | None = None,
    mode: str | None = None,
    scale: str = "CELSIUS",
    before: float | None = None,
) -> dict[str, Any]:
    """Three-valued verify of a thermostat write against a state read (pure).

    Same semantics as :func:`lock_verify`: ``True``/``False`` from what Amazon
    holds, ``None`` when the verify read answered nothing ("could not check",
    never a quiet pass). For ``--adjust`` the check needs the *pre-write*
    setpoint — pass it as ``before``; without it the relative write cannot be
    verified and ``ok`` is honestly ``None``.
    """
    held = thermostat_state(payload, entity_id)
    held_mode = thermostat_mode_state(payload, entity_id)
    checks: list[bool | None] = []
    if setpoint is not None:
        checks.append(_held_matches(held, setpoint, scale))
    if adjust is not None:
        checks.append(None if before is None else _held_matches(held, before + adjust, scale))
    if mode is not None:
        checks.append(None if held_mode is None else held_mode.upper() == mode.upper())
    ok: bool | None = None if (not checks or None in checks) else all(checks)
    return {
        "targetSetpoint": held,
        "mode": held_mode if mode is not None else None,
        "ok": ok,
    }


# ── live operations ──────────────────────────────────────────────────────


async def fetch_states(
    login,
    entity_ids: list[str] | None = None,
    appliance_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Raw ``POST /api/phoenix/state`` response for the given ids."""
    from alexapy import AlexaAPI

    if not entity_ids and not appliance_ids:
        raise ValueError("no addressable device — nothing to read state for")
    data = await AlexaAPI.get_entity_state(
        login,
        entity_ids=list(entity_ids) if entity_ids else None,
        appliance_ids=list(appliance_ids) if appliance_ids else None,
    )
    return data or {}


async def read_states(login, records: list[dict[str, Any]]) -> dict[str, Any]:
    """Capability rows (+ errors + skipped devices) for endpoint records."""
    ids = entity_refs(records)
    skipped = [r.get("name") for r in records or [] if not r.get("entityId")]
    if not ids:
        raise ValueError(
            "none of the selected devices has a phoenix entityId — no state can be read for them"
        )
    payload = await fetch_states(login, entity_ids=ids)
    return {
        "states": state_rows(payload, records),
        "errors": state_errors(payload),
        "skipped": [s for s in skipped if s],
    }


async def set_light_state(
    login,
    entity_id: str,
    *,
    power: bool | None = None,
    brightness: float | int | str | None = None,
    color: str | None = None,
    color_temperature: str | None = None,
) -> dict[str, Any]:
    """Apply a validated light/plug change to one entity.

    ``power`` defaults to ``True`` at the API boundary because
    ``set_light_state`` always sends a ``turnOn``/``turnOff``: a
    brightness-only change would otherwise silently turn the light *on*.  We
    make that explicit — a plan with no power verb sends ``turnOn``, which is
    what "set the brightness" means for an off lamp — and report what was sent.
    """
    plan = plan_light_change(
        power=power,
        brightness=brightness,
        color=color,
        color_temperature=color_temperature,
    )
    from alexapy import AlexaAPI

    response = await AlexaAPI.set_light_state(
        login,
        entity_id,
        power_on=True if plan["power"] is None else bool(plan["power"]),
        brightness=plan["brightness"],
        color_name=plan["color"],
        color_temperature_name=plan["color_temperature"],
    )
    return {
        "entityId": entity_id,
        "actions": plan["actions"],
        "response": response or {},
    }


async def set_power(login, entity_id: str, on: bool) -> dict[str, Any]:
    """Turn one device on or off (the plug/switch case of a light change)."""
    return await set_light_state(login, entity_id, power=bool(on))


async def fetch_guard_state(login, appliance_id: str, name: str | None = None) -> dict[str, Any]:
    """Read the Guard panel's arm state (addressed by **applianceId**)."""
    from alexapy import AlexaAPI

    payload = await AlexaAPI.get_guard_state(login, appliance_id)
    return guard_row(payload or {}, name=name)


async def set_guard_state(
    login, entity_id: str, state: str, name: str | None = None
) -> dict[str, Any]:
    """Arm Guard away / stand it down to home (addressed by **entityId**)."""
    arm_state = normalize_guard_state(state)
    from alexapy import AlexaAPI

    response = await AlexaAPI.static_set_guard_state(login, entity_id, arm_state)
    return {
        "name": name,
        "entityId": entity_id,
        "armState": arm_state,
        "response": response or {},
    }


async def set_lock_state(login, entity_id: str, lock: bool) -> dict[str, Any]:
    """Send one lock/unlock controlRequest (``PUT /api/phoenix/state``).

    alexapy wraps all 58 of its public ``AlexaAPI`` methods and none of them is
    a lock write, so this rides ``AlexaAPI._static_request`` directly — the same
    reuse-the-helper pattern as groups (nexus) and lists (www) — with the same
    ``controlRequests`` shape the Guard arm (``controlSecurityPanel``) and the
    light verbs (``turnOn``/``setBrightness``) already use, addressed by
    **entityId** with ``entityType: ENTITY``.

    Like every write whose response answers nothing useful (a bare control
    response, no state), the caller must re-read and verify — use
    :func:`verify_lock_write`.
    """
    from alexapy import AlexaAPI

    action = lock_action(lock)
    data = {
        "controlRequests": [
            {
                "entityId": entity_id,
                "entityType": "ENTITY",
                "parameters": {"action": action},
            }
        ]
    }
    resp = await AlexaAPI._static_request("put", login, "/api/phoenix/state", data=data)
    if resp is None:
        raise RuntimeError("the lock control request returned no response")
    text = await resp.text()
    try:
        body = json.loads(text)
    except (TypeError, ValueError):
        body = {}
    if not isinstance(body, dict):
        body = {}
    return {"entityId": entity_id, "action": action, "response": body}


async def verify_lock_write(
    login, entity_id: str, lock: bool, name: str | None = None
) -> dict[str, Any]:
    """Re-read one lock's state after a write and report ``ok`` (pure-ish).

    The write's own response carries no state, so ``ok`` comes from what Amazon
    **holds** on a fresh ``/api/phoenix/state`` read: ``True``/``False``, or
    ``None`` when the read answered nothing for the entity (unreachable or
    throttled — "could not check", never silently failure).
    """
    payload = await fetch_states(login, entity_ids=[entity_id])
    return {
        "name": name,
        "entityId": entity_id,
        "lockState": lock_state(payload, entity_id),
        "ok": lock_verify(payload, entity_id, lock),
    }


async def set_thermostat_state(
    login,
    entity_id: str,
    *,
    setpoint: float | int | str | None = None,
    adjust: float | int | str | None = None,
    mode: str | None = None,
    scale: str | None = "celsius",
) -> dict[str, Any]:
    """Send one thermostat controlRequest set (``PUT /api/phoenix/state``).

    alexapy's ``set_light_state`` builder has no thermostat actions, so — like
    the lock surface — this builds the app's own ``controlRequests`` shape and
    rides ``AlexaAPI._static_request``.  One request entry per action, in the
    order Alexa applies them (setpoint/adjust, then mode), addressed by
    **entityId** with ``entityType: ENTITY``.

    Like every write whose response answers nothing useful, the caller must
    re-read and verify — use :func:`verify_thermostat_write`.
    """
    from alexapy import AlexaAPI

    plan = plan_thermostat_change(setpoint=setpoint, adjust=adjust, mode=mode, scale=scale)
    requests: list[dict[str, Any]] = []
    if plan["targetSetpoint"] is not None:
        requests.append(
            {
                "entityId": entity_id,
                "entityType": "ENTITY",
                "parameters": {
                    "action": "setTargetSetpoint",
                    "targetSetpoint": plan["targetSetpoint"],
                },
            }
        )
    if plan["targetSetpointDelta"] is not None:
        requests.append(
            {
                "entityId": entity_id,
                "entityType": "ENTITY",
                "parameters": {
                    "action": "adjustTargetTemperature",
                    "targetSetpointDelta": plan["targetSetpointDelta"],
                },
            }
        )
    if plan["mode"] is not None:
        requests.append(
            {
                "entityId": entity_id,
                "entityType": "ENTITY",
                "parameters": {"action": "setMode", "mode": plan["mode"]},
            }
        )
    resp = await AlexaAPI._static_request(
        "put", login, "/api/phoenix/state", data={"controlRequests": requests}
    )
    if resp is None:
        raise RuntimeError("the thermostat control request returned no response")
    text = await resp.text()
    try:
        body = json.loads(text)
    except (TypeError, ValueError):
        body = {}
    if not isinstance(body, dict):
        body = {}
    return {"entityId": entity_id, "actions": plan["actions"], "response": body}


async def verify_thermostat_write(
    login,
    entity_id: str,
    *,
    setpoint: float | None = None,
    adjust: float | None = None,
    mode: str | None = None,
    scale: str = "CELSIUS",
    before: float | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """Re-read one thermostat after a write and report ``ok`` (pure-ish).

    ``ok`` is three-valued exactly like the lock verify: ``True``/``False``
    from what Amazon holds, ``None`` when the read answered nothing (device
    unreachable or throttled — "could not check", never a silent pass).
    """
    payload = await fetch_states(login, entity_ids=[entity_id])
    return {
        "name": name,
        "entityId": entity_id,
        **thermostat_verify(
            payload,
            entity_id,
            setpoint=setpoint,
            adjust=adjust,
            mode=mode,
            scale=scale,
            before=before,
        ),
    }
