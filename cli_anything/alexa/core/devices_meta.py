"""Echo/Alexa *device* (not appliance) helpers.

These are the physical Echo speakers used as targets for announce / dnd /
routine-run, distinct from the smart-home `appliances` graph.
"""

from __future__ import annotations

from typing import Any


async def fetch_devices(login) -> list[dict[str, Any]]:
    """Raw Alexa device records (Echos etc.)."""
    from alexapy import AlexaAPI

    data = await AlexaAPI.get_devices(login)
    return list(data or [])


def device_rows(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten device records into display rows."""
    out: list[dict[str, Any]] = []
    for d in devices or []:
        out.append(
            {
                "accountName": d.get("accountName"),
                "serialNumber": d.get("serialNumber"),
                "deviceType": d.get("deviceType"),
                "deviceFamily": d.get("deviceFamily"),
                "online": d.get("online"),
            }
        )
    return out


def find_device(devices: list[dict[str, Any]], name_or_serial: str) -> dict[str, Any] | None:
    """Match a device by accountName (case-insensitive) or serialNumber."""
    if not name_or_serial:
        return None
    target = name_or_serial.strip().lower()
    for d in devices or []:
        if (d.get("serialNumber") or "").lower() == target:
            return d
        if (d.get("accountName") or "").strip().lower() == target:
            return d
    return None


def _unwrap(payload: Any, key: str) -> list[dict[str, Any]]:
    """Pull a list out of an Alexa response (pure).

    alexapy is inconsistent about this: ``get_wake_words`` already unwraps
    ``payload["wakeWords"]`` and hands back the list, while ``get_bluetooth``
    and ``get_dnd_state`` return the whole envelope.  Accepting both shapes (and
    ``None``) keeps the row builders honest either way.
    """
    if payload is None:
        return []
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    if isinstance(payload, dict):
        items = payload.get(key)
        if isinstance(items, list):
            return [p for p in items if isinstance(p, dict)]
    return []


def _serial_to_name(devices: list[dict[str, Any]] | None) -> dict[str, str]:
    """serialNumber → accountName lookup (pure)."""
    return {
        d.get("serialNumber"): d.get("accountName") for d in devices or [] if d.get("serialNumber")
    }


def bluetooth_rows(
    payload: Any, devices: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Flatten ``/api/bluetooth`` into one row per *paired* device (pure).

    The response is one entry per Echo, each carrying a ``pairedDeviceList``;
    an Echo with nothing paired still gets a row (with empty fields) so the
    output shows it was checked rather than silently omitting it.
    """
    names = _serial_to_name(devices)
    out: list[dict[str, Any]] = []
    for state in _unwrap(payload, "bluetoothStates"):
        serial = state.get("deviceSerialNumber")
        paired = [p for p in (state.get("pairedDeviceList") or []) if isinstance(p, dict)]
        if not paired:
            out.append(
                {
                    "device": names.get(serial, serial),
                    "serial": serial,
                    "paired": None,
                    "address": None,
                    "connected": None,
                }
            )
            continue
        for p in paired:
            out.append(
                {
                    "device": names.get(serial, serial),
                    "serial": serial,
                    "paired": p.get("friendlyName"),
                    "address": p.get("address"),
                    "connected": p.get("connected"),
                }
            )
    return out


def wake_word_rows(
    payload: Any, devices: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Flatten wake-word records into display rows (pure)."""
    names = _serial_to_name(devices)
    out: list[dict[str, Any]] = []
    for w in _unwrap(payload, "wakeWords"):
        serial = w.get("deviceSerialNumber")
        out.append(
            {
                "device": names.get(serial, serial),
                "serial": serial,
                "wakeWord": w.get("wakeWord"),
                "active": w.get("active"),
            }
        )
    return out


def dnd_rows(payload: Any, devices: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Flatten ``/api/dnd/device-status-list`` into display rows (pure).

    ``enabled`` is rendered as the same on/off vocabulary the ``dnd`` *write*
    command takes, so a read and a write of the same device speak alike.
    """
    names = _serial_to_name(devices)
    out: list[dict[str, Any]] = []
    for d in _unwrap(payload, "doNotDisturbDeviceStatusList"):
        serial = d.get("deviceSerialNumber")
        enabled = d.get("enabled")
        out.append(
            {
                "device": names.get(serial, serial),
                "serial": serial,
                "dnd": None if enabled is None else ("on" if enabled else "off"),
            }
        )
    return out


async def fetch_bluetooth(login) -> list[dict[str, Any]]:
    """Paired-bluetooth rows for every Echo on the account."""
    from alexapy import AlexaAPI

    payload = await AlexaAPI.get_bluetooth(login)
    return bluetooth_rows(payload, await fetch_devices(login))


async def fetch_wake_words(login) -> list[dict[str, Any]]:
    """Configured wake word per Echo."""
    from alexapy import AlexaAPI

    payload = await AlexaAPI.get_wake_words(login)
    return wake_word_rows(payload, await fetch_devices(login))


async def fetch_dnd_states(login) -> list[dict[str, Any]]:
    """Current Do-Not-Disturb state per Echo."""
    from alexapy import AlexaAPI

    payload = await AlexaAPI.get_dnd_state(login)
    return dnd_rows(payload, await fetch_devices(login))
