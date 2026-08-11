"""Adapter: raw ``get_devices()`` record → the object alexapy actually wants.

**This exists because of a real shape mismatch, not for tidiness.**

``AlexaAPI.get_devices(login)`` returns plain JSON dicts (``serialNumber``,
``deviceType``, ``deviceFamily``, ``accountName``, ``online`` …).  But every
*device-bound* ``AlexaAPI`` **instance** method reads its target off
``self._device`` as **attributes**::

    self._device.device_serial_number   # set_media / set_dnd_state / stop / …
    self._device._device_type           # …
    self._device._device_family         # process_targets (WHA whole-home audio)
    self._device._cluster_members       # process_targets
    self._device._locale                # send_announcement / send_tts

(alexapy is written against Home Assistant's ``AlexaClient`` entity object,
which has those attributes.)  Handing the raw dict straight to
``AlexaAPI(record, login)`` therefore raises ``AttributeError`` on the first
attribute access — and alexapy's ``_catch_all_exceptions`` decorator only
converts ``ClientConnectionError``/``KeyError``/``JSONDecodeError``/… and
**re-raises everything else**, so it surfaces as a raw traceback rather than a
friendly error.

``DeviceRef`` is a pure, dependency-free translation of one record into that
attribute surface.  Construct it once per device and pass it wherever a
device-bound ``AlexaAPI`` is built.  Keeping it pure means the whole mapping is
unit-testable without alexapy or a live account.
"""

from __future__ import annotations

from typing import Any

#: alexapy substitutes this when a device record carries no locale
#: (it does ``self._device._locale if self._device._locale else "en-US"``).
DEFAULT_LOCALE = "en-US"

#: ``deviceFamily`` value marking a whole-home-audio group.  alexapy's
#: ``process_targets`` fans a WHA target out across ``_cluster_members``.
WHA_FAMILY = "WHA"


class DeviceRef:
    """Attribute view over one Alexa device record.

    Attribute names deliberately mirror alexapy's private ``AlexaClient``
    contract (including the leading underscores) — they are the interface, not
    an implementation detail we chose.
    """

    __slots__ = (
        "_cluster_members",
        "_device_family",
        "_device_type",
        "_locale",
        "account_name",
        "device_serial_number",
        "online",
        "raw",
    )

    def __init__(self, record: dict[str, Any]):
        rec = dict(record or {})
        serial = (rec.get("serialNumber") or "").strip()
        if not serial:
            name = rec.get("accountName") or "<unnamed>"
            raise ValueError(f"device record for {name!r} has no serialNumber; cannot target it")
        self.raw = rec
        self.device_serial_number = serial
        self._device_type = rec.get("deviceType") or ""
        self._device_family = rec.get("deviceFamily") or ""
        # clusterMembers is the WHA group's member serials; absent on normal Echos.
        self._cluster_members = list(rec.get("clusterMembers") or [])
        # Left falsy (not defaulted here) so alexapy applies its own en-US
        # fallback — see DEFAULT_LOCALE, exposed via `locale` for callers.
        self._locale = rec.get("locale") or None
        self.account_name = rec.get("accountName")
        self.online = bool(rec.get("online"))

    @property
    def locale(self) -> str:
        """Effective locale, applying alexapy's own ``en-US`` fallback."""
        return self._locale or DEFAULT_LOCALE

    @property
    def is_wha(self) -> bool:
        """True when this is a whole-home-audio group rather than one speaker."""
        return self._device_family == WHA_FAMILY

    def summary(self) -> dict[str, Any]:
        """Display/JSON-safe identity of the device (pure)."""
        return {
            "device": self.account_name,
            "serial": self.device_serial_number,
            "deviceType": self._device_type,
            "deviceFamily": self._device_family,
            "online": self.online,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"DeviceRef({self.account_name!r}, {self.device_serial_number!r})"


def to_device_ref(record: dict[str, Any]) -> DeviceRef:
    """Build a :class:`DeviceRef` from a raw ``get_devices()`` record."""
    return DeviceRef(record)
