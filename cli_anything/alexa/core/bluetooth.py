"""Bluetooth **connect / disconnect** for physical Echo devices.

The harness could already *read* what is paired to each Echo (``echos
bluetooth`` → :func:`devices_meta.fetch_bluetooth`) but not act on it, which is
the half a user wants: "connect my phone to the kitchen Echo, then play".  Two
device-bound alexapy calls close that:

* :func:`connect` — ``AlexaAPI.set_bluetooth(mac)``
  (``POST /api/bluetooth/pair-sink/<type>/<serial>``) connects an **already
  paired** sink.  It does *not* perform the initial pairing: putting a phone in
  pairing mode and confirming the code is an Alexa-app/voice-only flow, so a
  target that is not in the Echo's ``pairedDeviceList`` is refused locally with
  the list of what *is* paired, rather than posting an address Amazon answers
  with a bare 200 and ignores.
* :func:`disconnect` — ``AlexaAPI.disconnect_bluetooth()``
  (``POST /api/bluetooth/disconnect-sink/<type>/<serial>``).  **Amazon has no
  per-sink disconnect**: the endpoint drops *every* connected sink on that Echo,
  so the CLI reports the target as "all" rather than implying otherwise.

Three things are worth knowing before extending this module:

* **Both calls are device-bound**, so the raw ``get_devices()`` record is
  wrapped in a :class:`~cli_anything.alexa.core.device_ref.DeviceRef` — alexapy
  reads ``_device_type`` / ``device_serial_number`` as *attributes* off
  ``self._device`` and raises ``AttributeError`` on a plain dict.
* **The address is sent verbatim from the pairing record.**  ``/api/bluetooth``
  reports each sink's ``address``, and that exact string is what ``pair-sink``
  wants (Home Assistant's ``alexa_media`` does the same).  A user-typed MAC is
  only used to *find* the pairing (:func:`normalize_mac` makes ``aa-bb-…`` and
  ``AA:BB:…`` compare equal); the value posted is still Amazon's own.
* **Ambiguity is the caller's decision, as everywhere else.**
  :func:`resolve_pairing` returns *all* matches for the winning precedence tier
  (a paired phone and a paired car can share a friendly name), so 0 / 1 / >1 are
  handled by the CLI with the same abort-and-list-candidates rule
  ``devices rename`` uses.

Everything above the "live operations" divider is pure and unit-tested; only the
thin ``async def`` wrappers touch the network.
"""

from __future__ import annotations

import re
from typing import Any

from cli_anything.alexa.core.device_ref import DeviceRef
from cli_anything.alexa.core.media import resolve_device

#: A MAC address as a human might type it: six hex pairs, optionally separated
#: by ``:`` or ``-``.  Amazon's own ``address`` values are colon-separated
#: uppercase, but its Bluetooth ids are not *always* plain MACs (some sinks
#: report a longer opaque id), which is why an unmatched target is looked up by
#: name too rather than rejected on shape alone.
_MAC_RE = re.compile(r"^(?:[0-9a-f]{2}([:-]?))(?:[0-9a-f]{2}\1){4}[0-9a-f]{2}$", re.IGNORECASE)


# ── pure helpers ─────────────────────────────────────────────────────────


def is_mac(value: Any) -> bool:
    """True when ``value`` looks like a MAC address (pure)."""
    return bool(isinstance(value, str) and _MAC_RE.match(value.strip()))


def normalize_mac(value: Any) -> str:
    """Canonicalise a MAC to ``AA:BB:CC:DD:EE:FF`` for *comparison* (pure).

    Never used as the value posted to Amazon — see the module docstring — only
    to make the three ways of writing the same address compare equal.  A
    non-MAC string is returned trimmed and upper-cased so opaque sink ids still
    compare sanely.
    """
    raw = (value or "").strip() if isinstance(value, str) else ""
    if not raw:
        return ""
    if not is_mac(raw):
        return raw.upper()
    hex_only = re.sub(r"[:-]", "", raw).upper()
    return ":".join(hex_only[i : i + 2] for i in range(0, 12, 2))


def normalize_name(value: Any) -> str:
    """Case/whitespace-insensitive form of a sink's friendly name (pure)."""
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip().lower()


def pairings_for(payload: Any, serial: str) -> list[dict[str, Any]]:
    """Every sink paired to ONE Echo, as flat rows (pure).

    ``/api/bluetooth`` answers with one ``bluetoothStates`` entry per Echo, each
    carrying a ``pairedDeviceList``.  An Echo with nothing paired (or absent from
    the payload entirely) yields an empty list rather than raising, so "nothing
    is paired" stays a normal answer.
    """
    target = (serial or "").strip()
    if not target:
        return []
    states = payload if isinstance(payload, list) else None
    if states is None:
        states = (payload or {}).get("bluetoothStates") if isinstance(payload, dict) else None
    rows: list[dict[str, Any]] = []
    for state in states or []:
        if not isinstance(state, dict) or state.get("deviceSerialNumber") != target:
            continue
        for sink in state.get("pairedDeviceList") or []:
            if not isinstance(sink, dict):
                continue
            rows.append(
                {
                    "name": sink.get("friendlyName"),
                    "address": sink.get("address"),
                    "connected": sink.get("connected"),
                    "profiles": list(sink.get("profiles") or []),
                }
            )
    return rows


def resolve_pairing(pairings: list[dict[str, Any]], target: str) -> list[dict[str, Any]]:
    """Resolve a ``<target>`` to paired-sink rows (pure).

    Precedence (first tier that yields any match wins):
      1. exact ``address``
      2. normalized address (``aa-bb-…`` / lower case / no separators)
      3. exact friendly name
      4. normalized friendly name

    Returns the matches for the winning tier; the caller decides what 0 (not
    paired), 1 (resolved) and >1 (ambiguous) mean.
    """
    rows = [p for p in (pairings or []) if isinstance(p, dict)]
    raw = (target or "").strip()
    if not raw:
        return []
    hits = [p for p in rows if p.get("address") == raw]
    if hits:
        return hits
    # `raw` is non-empty here, so both normalisers are non-empty too — no guard.
    wanted = normalize_mac(raw)
    hits = [p for p in rows if normalize_mac(p.get("address")) == wanted]
    if hits:
        return hits
    hits = [p for p in rows if p.get("name") == raw]
    if hits:
        return hits
    name = normalize_name(raw)
    hits = [p for p in rows if normalize_name(p.get("name")) == name]
    if hits:
        return hits
    return []


def pairing_names(pairings: list[dict[str, Any]]) -> list[str]:
    """The paired sinks' display labels, for an error message (pure)."""
    labels = []
    for p in pairings or []:
        if not isinstance(p, dict):
            continue
        label = p.get("name") or p.get("address")
        if label:
            labels.append(str(label))
    return labels


def not_paired_error(device: str | None, target: str, pairings: list[dict[str, Any]]) -> str:
    """The caller-facing message for a target that is not paired (pure).

    Naming the alternatives matters here: Amazon answers ``pair-sink`` for an
    unknown address with a ``200`` and does nothing, so without this the failure
    would be invisible.
    """
    known = pairing_names(pairings)
    where = f" to {device!r}" if device else ""
    if not known:
        return (
            f"nothing is paired{where}; pair the device in the Alexa app first "
            "(initial pairing is not available over the API)"
        )
    return f"no paired device matching {target!r}{where}; paired: {', '.join(known)}"


def connect_row(ref: DeviceRef, sink: dict[str, Any]) -> dict[str, Any]:
    """Result row for a successful :func:`connect` (pure)."""
    return {
        "device": ref.account_name,
        "connected": sink.get("name") or sink.get("address"),
        "address": sink.get("address"),
        "ok": True,
    }


# ── live operations ──────────────────────────────────────────────────────


async def _fetch_payload(login) -> Any:
    from alexapy import AlexaAPI

    return await AlexaAPI.get_bluetooth(login)


async def list_pairings(login, device: str | None = None) -> dict[str, Any]:
    """Every sink paired to one Echo (defaults to the first online one)."""
    ref = await resolve_device(login, device)
    payload = await _fetch_payload(login)
    return {
        "device": ref.account_name,
        "serial": ref.device_serial_number,
        "pairings": pairings_for(payload, ref.device_serial_number),
    }


async def connect(login, device: str | None, target: str) -> dict[str, Any]:
    """Connect an already-paired Bluetooth sink to one Echo."""
    wanted = (target or "").strip()
    if not wanted:
        raise ValueError("a paired device name or MAC address is required")
    from alexapy import AlexaAPI

    ref = await resolve_device(login, device)
    pairings = pairings_for(await _fetch_payload(login), ref.device_serial_number)
    matches = resolve_pairing(pairings, wanted)
    if not matches:
        raise ValueError(not_paired_error(ref.account_name, wanted, pairings))
    if len(matches) > 1:
        addresses = ", ".join(str(m.get("address")) for m in matches)
        raise ValueError(
            f"{wanted!r} matches {len(matches)} paired devices on {ref.account_name!r}; "
            f"pick one by address: {addresses}"
        )
    sink = matches[0]
    address = sink.get("address")
    if not address:
        raise ValueError(f"paired device {wanted!r} has no address; cannot connect it")
    api = AlexaAPI(ref, login)
    await api.set_bluetooth(address)
    return connect_row(ref, sink)


async def disconnect(login, device: str | None = None) -> dict[str, Any]:
    """Disconnect **every** connected Bluetooth sink from one Echo.

    Amazon's endpoint is all-or-nothing (see the module docstring), so the row
    says ``all`` instead of pretending a single sink was targeted.
    """
    from alexapy import AlexaAPI

    ref = await resolve_device(login, device)
    api = AlexaAPI(ref, login)
    await api.disconnect_bluetooth()
    return {"device": ref.account_name, "disconnected": "all", "ok": True}
