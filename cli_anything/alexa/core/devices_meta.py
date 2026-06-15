"""Echo/Alexa *device* (not appliance) helpers.

These are the physical Echo speakers used as targets for announce / dnd /
routine-run, distinct from the smart-home `appliances` graph.
"""

from __future__ import annotations

from typing import Any, Optional


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


def find_device(devices: list[dict[str, Any]], name_or_serial: str) -> Optional[dict[str, Any]]:
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
