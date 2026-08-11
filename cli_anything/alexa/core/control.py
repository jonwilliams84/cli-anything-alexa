"""Announce / speak / push / Do-Not-Disturb operations against physical Echo devices.

Every call here is a *device-bound* alexapy instance method, so the raw
``get_devices()`` record must be wrapped in a
:class:`~cli_anything.alexa.core.device_ref.DeviceRef` first — alexapy reads
``device_serial_number`` / ``_device_type`` / ``_device_family`` / ``_locale``
as **attributes** off ``self._device`` and raises ``AttributeError`` on a dict.

The four verbs differ in *where the message lands*, which is the whole reason
all four exist:

============  ==========================  =====================================
verb          alexapy call                lands on
============  ==========================  =====================================
``announce``  ``send_announcement``       every speaker (or one), with the chime
``speak``     ``send_tts``                one speaker, no chime
``push``      ``send_mobilepush``         the **Alexa app** on your phone
``push``      ``send_dropin_notification``  the Alexa app, offering a drop-in
============  ==========================  =====================================

``push`` is the only one that does not make a noise in the house, which makes it
the safe channel for a script running at 3am.  Both push variants are still
*device-bound* (they ride the behaviours API through ``send_sequence``), so they
resolve an Echo exactly like ``speak`` does even though nothing plays on it.
"""

from __future__ import annotations

from typing import Any

from cli_anything.alexa.core.device_ref import to_device_ref
from cli_anything.alexa.core.devices_meta import fetch_devices, find_device

#: Title shown above a pushed message in the Alexa app.  alexapy's own default
#: is the developer-facing ``"AlexaAPI Message"``; this names the sender the way
#: a user would recognise it.
DEFAULT_PUSH_TITLE = "cli-anything-alexa"


async def announce(login, text: str, device: str | None = None) -> dict[str, Any]:
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
    api = AlexaAPI(to_device_ref(runner), login)
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
    api = AlexaAPI(to_device_ref(target), login)
    await api.set_dnd_state(state)
    return {"device": target.get("accountName"), "dnd": "on" if state else "off"}


async def speak(login, text: str, device: str | None = None) -> dict[str, Any]:
    """Speak text on one Echo via TTS (``send_tts``) — no announcement chime.

    ``announce`` uses ``send_announcement``, which plays Alexa's announcement
    tone first and can fan out to every device.  ``send_tts`` is the plain
    "Simon Says" path: the speaker just talks.  alexapy documents ``targets``
    as **non-functional** for TTS (Amazon ignores it), so the message always
    plays on the device the API instance is bound to — we therefore bind to the
    requested device rather than passing targets, and default to the first
    online Echo when none is named.
    """
    from alexapy import AlexaAPI

    devices = await fetch_devices(login)
    if not devices:
        raise ValueError("no Alexa devices found on the account")

    if device:
        target = find_device(devices, device)
        if not target:
            raise ValueError(f"no device matching {device!r}")
    else:
        target = next((d for d in devices if d.get("online")), devices[0])

    ref = to_device_ref(target)
    api = AlexaAPI(ref, login)
    await api.send_tts(text)
    return {"spoke": text, "device": ref.account_name, "serial": ref.device_serial_number}


def normalize_push(message: str | None, title: str | None = None) -> tuple[str, str]:
    """Validate a push ``(message, title)`` pair (pure).

    Only emptiness is refused — Amazon renders the strings as given — and an
    empty title falls back to :data:`DEFAULT_PUSH_TITLE` rather than alexapy's
    developer-facing ``"AlexaAPI Message"``.
    """
    text = (message or "").strip()
    if not text:
        raise ValueError("a message is required, e.g. 'the washing machine finished'")
    heading = (title or "").strip() or DEFAULT_PUSH_TITLE
    return text, heading


async def push(
    login,
    message: str,
    title: str | None = None,
    device: str | None = None,
    dropin: bool = False,
) -> dict[str, Any]:
    """Push a notification to the Alexa app (silent on the speakers).

    ``dropin=True`` sends ``send_dropin_notification`` instead, which offers to
    drop in on the resolved Echo from the notification; the plain variant is
    ``send_mobilepush``.  Both are device-bound (they ride the behaviours API),
    so an Echo is resolved the same way ``speak`` does — the first *online* one
    when none is named — even though nothing is played on it.
    """
    from alexapy import AlexaAPI

    text, heading = normalize_push(message, title)

    devices = await fetch_devices(login)
    if not devices:
        raise ValueError("no Alexa devices found on the account")
    if device:
        target = find_device(devices, device)
        if not target:
            raise ValueError(f"no device matching {device!r}")
    else:
        target = next((d for d in devices if d.get("online")), devices[0])

    ref = to_device_ref(target)
    api = AlexaAPI(ref, login)
    if dropin:
        await api.send_dropin_notification(text, title=heading)
    else:
        await api.send_mobilepush(text, title=heading)
    return {
        "pushed": text,
        "title": heading,
        "kind": "dropin" if dropin else "mobilepush",
        "via_device": ref.account_name,
    }
