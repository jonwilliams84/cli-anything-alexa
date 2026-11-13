"""Echo **equalizer** — per-Echo bass / mid / treble.

The one speaker-sound surface alexapy wraps that the harness exposed
nothing for.  Amazon keeps three signed bands per Echo, each an integer in
-6..+6, read and written per device over ``/api/equalizer/<serial>/<type>``.
This is the "Sound settings" page of the Alexa app.

Alexapy wraps both halves as *static* methods (login + serial + deviceType,
like the notification-volume surface — not instance methods, so NOT a
``DeviceRef`` path):

* ``AlexaAPI.get_equalizer(login, serial, device_type)`` — the read.
* ``AlexaAPI.set_equalizer(login, serial, device_type, bass, mid, treble)`` —
  the write.  Amazon zeroes any band left out, so all three are sent
  together; alexapy validates the range too and refuses a fractional value.

The write answers ``True`` whenever the HTTP response merely *exists* —
nothing about what Amazon stored — so the executed write re-reads the same
equalizer document and reports ``ok`` from what Amazon holds: ``True`` when
all three observed bands equal the request, ``False`` when any differs, and
``None`` (could not check) when the verify read answered nothing — the same
three-valued verify the notification-volume / locks / Kids writes use.
"""

from __future__ import annotations

from typing import Any

from cli_anything.alexa.core import devices_meta

BANDS = ("bass", "mid", "treble")
BAND_MIN = -6
BAND_MAX = 6


def normalize_band(value: Any, band: str) -> int:
    """One equalizer band as Amazon stores it: an **integer** in -6..+6 (pure).

    A fractional value (``2.5``) is refused, not rounded away: Amazon stores
    *something*, and the verify re-read could then never equal what the user
    asked for.  ``bool`` is refused (``True`` is an ``int`` in Python).
    Raises ``ValueError`` with a caller-facing message; the CLI surfaces it
    verbatim, before login, per the harness-wide rule.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{band} must be an integer from -6 to 6, got {value!r}")
    if not BAND_MIN <= value <= BAND_MAX:
        raise ValueError(f"{band} must be from -6 to 6, got {value}")
    return int(value)


def equalizer_row(payload: Any, record: dict[str, Any]) -> dict[str, Any]:
    """Flatten one Echo's equalizer read into a display row (pure).

    ``payload`` is alexapy's ``get_equalizer`` answer (``{bass, mid, treble}``
    or ``None`` when the device has no equalizer — alexapy hands back ``None``
    for anything that did not answer with a dict).  Every band stays ``None``
    there so a non-Echo device is *visible* in the roster, not silently
    dropped, and a band Amazon omits shows its own ``None``.
    """
    bands = payload if isinstance(payload, dict) else {}
    row: dict[str, Any] = {
        "device": record.get("accountName") or record.get("serialNumber"),
        "serial": record.get("serialNumber"),
        "deviceType": record.get("deviceType"),
    }
    for band in BANDS:
        value = bands.get(band)
        row[band] = (
            int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
        )
    return row


def resolve_equalizer_target(records: list[dict[str, Any]] | None, target: str) -> dict[str, Any]:
    """The device record an equalizer write targets (pure).

    Same name-or-serial match as the notification-volume surface's lookup —
    these calls are *static* too, so what the write needs is the raw record's
    ``serialNumber``/``deviceType`` (both ride the URL), not a
    :class:`DeviceRef`.  An unknown name is refused locally with the account's
    device names — the same abort-and-list-candidates rule everywhere else
    uses, so the failure never depends on the network answering.
    """
    record = devices_meta.find_device(records or [], target)
    if record is None:
        known = sorted({d.get("accountName") for d in records or [] if d.get("accountName")})
        hint = f"; devices: {', '.join(str(k) for k in known)}" if known else ""
        raise ValueError(f"no device matching {target!r}{hint}")
    serial = record.get("serialNumber")
    device_type = record.get("deviceType")
    missing = [
        label
        for label, value in (("serialNumber", serial), ("deviceType", device_type))
        if not value
    ]
    if missing:
        raise ValueError(
            "cannot target "
            f"{record.get('accountName') or serial!r}: the device record has no "
            f"{', '.join(missing)} — re-run `echos list` and pick a device Amazon knows"
        )
    return record


def observed_bands(payload: Any) -> dict[str, int | None] | None:
    """One Echo's bass/mid/treble as Amazon holds them NOW (pure).

    ``None`` when the fresh read answered nothing — the verify's "could not
    check" answer, never a quiet pass.  A dict is accepted band-by-band: a
    band Amazon omits stays ``None`` and cannot silently verify as equal.
    """
    if not isinstance(payload, dict):
        return None
    out: dict[str, int | None] = {}
    for band in BANDS:
        value = payload.get(band)
        try:
            out[band] = int(value) if value is not None else None
        except (TypeError, ValueError):
            out[band] = None
    return out


def verify_equalizer(
    requested: dict[str, int], observed: dict[str, int | None] | None
) -> bool | None:
    """Three-valued compare of what was sent against what Amazon holds (pure).

    ``True`` when every observed band equals its request, ``False`` when any
    observed band differs, ``None`` when the read gave nothing or any band is
    unverifiable — the same three-valued semantics the other writes use.
    """
    if observed is None:
        return None
    if any(observed.get(band) is None for band in BANDS):
        return None
    return all(observed[band] == requested[band] for band in BANDS)


async def fetch_equalizer(
    login, device: str | None = None, devices: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Equalizer bass/mid/treble per Echo.

    The whole roster by default (one short GET per device; a device with no
    equalizer shows ``None`` bands rather than disappearing); name a device
    (or serial) for just its row, refused locally if unknown.
    """
    from alexapy import AlexaAPI

    record_list = devices if devices is not None else await devices_meta.fetch_devices(login)
    if device:
        record = resolve_equalizer_target(record_list, device)
        record_list = [record]
    out: list[dict[str, Any]] = []
    for record in record_list:
        serial = record.get("serialNumber")
        device_type = record.get("deviceType")
        payload = None
        if serial and device_type:
            payload = await AlexaAPI.get_equalizer(login, serial, device_type)
        out.append(equalizer_row(payload, record))
    return out


async def set_equalizer_bands(
    login, target: str, bass: int, mid: int, treble: int
) -> dict[str, Any]:
    """Set ONE Echo's bass / mid / treble, then verify by re-read.

    All three bands are sent — Amazon zeroes any band left out, so a partial
    write would silently flatten the others to 0; the CLI requires all three
    up front.  The verify re-reads the same equalizer document and reports
    ``ok`` per :func:`verify_equalizer`.
    """
    from alexapy import AlexaAPI

    requested = {
        "bass": normalize_band(bass, "bass"),
        "mid": normalize_band(mid, "mid"),
        "treble": normalize_band(treble, "treble"),
    }
    devices = await devices_meta.fetch_devices(login)
    record = resolve_equalizer_target(devices, target)
    serial = record["serialNumber"]
    device_type = record["deviceType"]
    sent = await AlexaAPI.set_equalizer(
        login, serial, device_type, requested["bass"], requested["mid"], requested["treble"]
    )
    observed = observed_bands(await AlexaAPI.get_equalizer(login, serial, device_type))
    return {
        "device": record.get("accountName"),
        "serial": serial,
        **requested,
        "sent": bool(sent),
        "observed": observed,
        "ok": verify_equalizer(requested, observed),
    }
