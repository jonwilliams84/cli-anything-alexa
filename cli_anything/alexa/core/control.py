"""Announce + Do-Not-Disturb operations against physical Echo devices."""

from __future__ import annotations

from typing import Any, Optional

from cli_anything.alexa.core.devices_meta import fetch_devices, find_device


async def announce(login, text: str, device: Optional[str] = None) -> dict[str, Any]:
    """Send a TTS announcement to all devices (or one named target)."""
    from alexapy import AlexaAPI

    devices = await fetch_devices(login)
    if not devices:
        raise ValueError("no Alexa devices found on the account")

    target = None
    if device:
        target = find_device(devices, device)
        if not target:
            raise ValueError(f"no device matching {device!r}")

    runner = target or next((d for d in devices if d.get("online")), devices[0])
    api = AlexaAPI(runner, login)
    targets = [target["serialNumber"]] if target else None
    await api.send_announcement(text, targets=targets)
    return {
        "announced": text,
        "target": (target or {}).get("accountName", "all"),
        "via_device": runner.get("accountName"),
    }


async def set_dnd(login, device: str, state: bool) -> dict[str, Any]:
    """Turn Do-Not-Disturb on/off for one device."""
    from alexapy import AlexaAPI

    devices = await fetch_devices(login)
    target = find_device(devices, device)
    if not target:
        raise ValueError(f"no device matching {device!r}")
    api = AlexaAPI(target, login)
    await api.set_dnd_state(state)
    return {"device": target.get("accountName"), "dnd": "on" if state else "off"}
