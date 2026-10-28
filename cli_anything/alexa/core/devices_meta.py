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


def preference_rows(
    payload: Any, devices: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Flatten ``/api/device-preferences`` into display rows (pure).

    The interesting field is ``timeZoneId``: it is the clock a reminder's
    ``originalDate``/``originalTime`` are expressed in, so an alarm edit
    (:mod:`cli_anything.alexa.core.notifications`) reads it from here rather
    than assuming the host's timezone.  ``locale`` matters for the same reason
    announce/speak care about it, and ``temperatureUnit``/``distanceUnits``
    explain what a device answers "what's the temperature" with.
    """
    names = _serial_to_name(devices)
    out: list[dict[str, Any]] = []
    for p in _unwrap(payload, "devicePreferences"):
        serial = p.get("deviceSerialNumber")
        out.append(
            {
                "device": names.get(serial, serial),
                "serial": serial,
                "timeZoneId": p.get("timeZoneId"),
                "locale": p.get("locale"),
                "temperatureUnit": p.get("temperatureUnit"),
                "distanceUnits": p.get("distanceUnits"),
                "wakeWordConfirmation": p.get("goldfishEnabled")
                if "goldfishEnabled" in p
                else p.get("wakeWordConfirmation"),
                "postalCode": p.get("postalCode"),
            }
        )
    return out


def device_timezone(rows: list[dict[str, Any]] | None, serial: str | None) -> str | None:
    """One device's ``timeZoneId`` from :func:`preference_rows` output (pure).

    ``None`` when the device has no preferences entry — the caller must then
    say which clock it fell back to rather than pretending it knew.
    """
    if not serial:
        return None
    for row in rows or []:
        if isinstance(row, dict) and row.get("serial") == serial:
            return row.get("timeZoneId") or None
    return None


def wifi_row(payload: Any, device: str | None = None, serial: str | None = None) -> dict[str, Any]:
    """Flatten ``/api/device-wifi-details`` into one display row (pure).

    Every field is optional: a device that has never been on wifi (or an
    ethernet-attached one) answers with an envelope missing most keys, which
    comes back as ``None`` rather than raising.
    """
    data = payload if isinstance(payload, dict) else {}
    # The payload is sometimes wrapped in a single-key envelope.
    for key in ("deviceWifiDetails", "wifiDetails", "details"):
        inner = data.get(key)
        if isinstance(inner, dict):
            data = inner
            break
    return {
        "device": device,
        "serial": serial or data.get("deviceSerialNumber"),
        "ssid": data.get("ssid") or data.get("essid"),
        "signalStrength": data.get("signalStrength") or data.get("rssi"),
        "securityMethod": data.get("securityMethod"),
        "macAddress": data.get("macAddress"),
        "ipAddress": data.get("ipAddress"),
        "frequency": data.get("frequency") or data.get("frequencyBand"),
    }


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


def wake_word_row(word: str | None, *, device: str, serial: str) -> dict[str, Any]:
    """One Echo's wake word as a display row (pure)."""
    return {"device": device, "serial": serial, "wakeWord": word}


async def fetch_wake_word(login, device: str | None = None) -> dict[str, Any]:
    """Wake word for ONE Echo via ``AlexaAPI.find_wake_word`` (device-bound).

    ``find_wake_word`` answers from alexapy's own ``get_wake_words`` read
    (lower-casing the word) and returns ``None`` when the serial has no entry —
    the row keeps that ``None`` rather than guessing a default, because an
    unreadable value is not the same as "alexa".
    """
    from alexapy import AlexaAPI

    from cli_anything.alexa.core.media import resolve_device

    ref = await resolve_device(login, device)
    word = await AlexaAPI.find_wake_word(login, ref.device_serial_number)
    return wake_word_row(word, device=ref.account_name, serial=ref.device_serial_number)


def background_url_problem(url: Any) -> str | None:
    """Why ``url`` is not a usable Echo Show background (pure), or ``None``.

    alexapy only *warns* about a plain-``http://`` URL and posts it anyway;
    Amazon answers it with nothing on screen, so the CLI refuses the URL
    locally instead of posting into the void.  Same contract as
    ``media.normalize_volume``: called before login, so bad input fails
    identically with and without ``--yes``.
    """
    if not isinstance(url, str) or not url.strip():
        return "a background image URL is required"
    if not url.strip().lower().startswith("https://"):
        return "the background image URL must be https (an Echo Show ignores plain http)"
    return None


async def set_background(login, device: str | None, url: str) -> dict[str, Any]:
    """Set an Echo Show's background to an https image URL (device-bound).

    Unlike the Amazon Kids writes, ``set_background`` returns a real success
    bool (HTTP 200 or not), so ``ok`` comes straight from the call — no verify
    re-read is needed.
    """
    from alexapy import AlexaAPI

    from cli_anything.alexa.core.media import resolve_device

    ref = await resolve_device(login, device)
    ok = await AlexaAPI(ref, login).set_background(url)
    return {
        "device": ref.account_name,
        "serial": ref.device_serial_number,
        "url": url,
        "ok": bool(ok),
    }


async def fetch_dnd_states(login) -> list[dict[str, Any]]:
    """Current Do-Not-Disturb state per Echo."""
    from alexapy import AlexaAPI

    payload = await AlexaAPI.get_dnd_state(login)
    return dnd_rows(payload, await fetch_devices(login))


async def fetch_device_preferences(
    login, devices: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Per-Echo preferences (timezone / locale / units) for the account.

    ``devices`` may be passed in by a caller that already fetched the device
    list (an alarm edit does) so the same records are not fetched twice.
    """
    from alexapy import AlexaAPI

    payload = await AlexaAPI.get_device_preferences(login)
    return preference_rows(payload, devices if devices is not None else await fetch_devices(login))


async def fetch_wifi_details(login, device: str | None = None) -> dict[str, Any]:
    """Wifi details for ONE Echo (default: the first online one).

    Device-bound, so it goes through :class:`DeviceRef` like every other
    instance-method call.  ``resolve_device`` is imported inside the function
    on purpose: :mod:`cli_anything.alexa.core.media` imports *this* module at
    module level, so a top-level import here would be a cycle.
    """
    from alexapy import AlexaAPI

    from cli_anything.alexa.core.media import resolve_device

    ref = await resolve_device(login, device)
    payload = await AlexaAPI(ref, login).get_wifi_details()
    return wifi_row(payload, device=ref.account_name, serial=ref.device_serial_number)


# ── notification volume ──────────────────────────────────────────────────
#
# The "Alarms, Timers, and Notifications" volume each Echo plays alerts at —
# a *device setting*, distinct from the media volume `media volume` writes.
# alexapy wraps both halves (``get_notification_states`` — the account-wide
# read — and ``set_notification_volume`` — the per-device write) but the
# harness exposed neither until 0.14.0.


def notification_volume_rows(
    payload: Any, devices: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Flatten ``/api/device-notification-state`` into display rows (pure).

    alexapy's ``get_notification_states`` already unwraps
    ``deviceNotificationStates`` to the list; the envelope form is accepted
    too (same defensive split as :func:`bluetooth_rows`).  ``softwareVersion``
    is kept because the *write* URL embeds it — a row missing it is an Echo a
    later ``set-notification-volume`` cannot target.
    """
    names = _serial_to_name(devices)
    out: list[dict[str, Any]] = []
    for s in _unwrap(payload, "deviceNotificationStates"):
        serial = s.get("deviceSerialNumber")
        out.append(
            {
                "device": names.get(serial, serial),
                "serial": serial,
                "volume": s.get("volumeLevel"),
                "softwareVersion": s.get("softwareVersion"),
            }
        )
    return out


def normalize_notification_volume(level: float | int | str) -> int:
    """The 0–100 **whole-number** volume the notification-state PUT wants (pure).

    Unlike the media volume (a fraction in alexapy's API), this endpoint takes
    an integer percentage — and a fractional one is not silently rounded away:
    ``30.5`` is refused, because Amazon would store *something* and the verify
    re-read could never equal what the user asked for.  Raises ``ValueError``
    with a caller-facing message on anything else; the CLI surfaces it
    verbatim, before login, per the harness-wide rule.
    """
    if isinstance(level, bool):
        raise ValueError(
            f"notification volume must be a whole number between 0 and 100, got {level!r}"
        )
    try:
        value = float(level)
    except (TypeError, ValueError):
        raise ValueError(
            f"notification volume must be a whole number between 0 and 100, got {level!r}"
        ) from None
    if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
        raise ValueError(
            f"notification volume must be a whole number between 0 and 100, got {level!r}"
        )
    if value != int(value):
        raise ValueError(f"notification volume must be a whole number, got {level!r}")
    if not 0 <= value <= 100:
        raise ValueError(f"notification volume must be between 0 and 100, got {level!r}")
    return int(value)


def resolve_notification_target(
    records: list[dict[str, Any]] | None, target: str
) -> dict[str, Any]:
    """The device record a notification-volume write targets (pure).

    Same name-or-serial match as :func:`resolve_device`'s lookup, but this is
    a *static* alexapy call, so what the write needs is the raw record's
    ``serialNumber``/``deviceType``/``softwareVersion``, not a
    :class:`DeviceRef`.  An unknown name is refused locally with the account's
    device names — the same abort-and-list-candidates rule everywhere else
    uses, so the failure never depends on the network answering.
    """
    record = find_device(records or [], target)
    if record is None:
        known = sorted({d.get("accountName") for d in records or [] if d.get("accountName")})
        hint = f"; devices: {', '.join(str(k) for k in known)}" if known else ""
        raise ValueError(f"no device matching {target!r}{hint}")
    return record


def notification_request(record: dict[str, Any], level: int | float | str) -> dict[str, Any]:
    """The PUT body alexapy's ``set_notification_volume`` sends (pure).

    Every one of ``deviceSerialNumber``/``deviceType``/``softwareVersion``
    rides in the body **and** in the URL path, so a record missing any of them
    cannot be addressed and is refused here rather than posting a half-built
    request.  ``level`` is normalised (whole 0–100) inside, so the function's
    output is exactly what alexapy serialises.
    """
    serial = record.get("serialNumber") or record.get("deviceSerialNumber")
    device_type = record.get("deviceType")
    software = record.get("softwareVersion")
    missing = [
        label
        for label, value in (
            ("serialNumber", serial),
            ("deviceType", device_type),
            ("softwareVersion", software),
        )
        if not value
    ]
    if missing:
        raise ValueError(
            "cannot target "
            f"{record.get('accountName') or serial!r}: the device record has no "
            f"{', '.join(missing)} — re-run `echos list` and pick a device Amazon knows"
        )
    return {
        "deviceSerialNumber": serial,
        "deviceType": device_type,
        "softwareVersion": software,
        "volumeLevel": normalize_notification_volume(level),
    }


def observed_notification_volume(payload: Any, serial: str | None) -> int | None:
    """One Echo's notification volume as Amazon holds it NOW (pure).

    ``None`` when the serial has no entry in the fresh read — the verify's
    "could not check" answer, never a quiet pass (the same three-valued
    semantics the locks/kids verifies use).
    """
    if not serial:
        return None
    for state in _unwrap(payload, "deviceNotificationStates"):
        if state.get("deviceSerialNumber") == serial:
            level = state.get("volumeLevel")
            try:
                return int(level)
            except (TypeError, ValueError):
                return None
    return None


async def fetch_notification_states(
    login, devices: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Notification volume ("Alarms, Timers, and Notifications") per Echo."""
    from alexapy import AlexaAPI

    payload = await AlexaAPI.get_notification_states(login)
    return notification_volume_rows(
        payload, devices if devices is not None else await fetch_devices(login)
    )


async def set_notification_volume(login, target: str, level: float | int | str) -> dict[str, Any]:
    """Set ONE Echo's notification volume, then verify by re-read.

    alexapy's ``set_notification_volume`` answers ``True`` whenever the HTTP
    response merely *exists* — nothing about what Amazon stored — so the
    executed write re-reads the account-wide notification state and reports
    ``ok`` from what Amazon holds: ``True`` when the observed volume equals
    the request, ``False`` when it differs, and ``None`` (could not check)
    when the verify read answered nothing.  Same three-valued verify as the
    locks and Amazon Kids writes.
    """
    from alexapy import AlexaAPI

    volume = normalize_notification_volume(level)
    devices = await fetch_devices(login)
    record = resolve_notification_target(devices, target)
    data = notification_request(record, volume)
    serial = data["deviceSerialNumber"]
    sent = await AlexaAPI.set_notification_volume(
        login,
        serial,
        data["deviceType"],
        data["softwareVersion"],
        data["volumeLevel"],
    )
    observed = observed_notification_volume(await AlexaAPI.get_notification_states(login), serial)
    return {
        "device": record.get("accountName"),
        "serial": serial,
        "volume": data["volumeLevel"],
        "sent": bool(sent),
        "observed": observed,
        "ok": None if observed is None else observed == data["volumeLevel"],
    }
