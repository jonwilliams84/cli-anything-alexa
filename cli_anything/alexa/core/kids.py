"""Amazon Kids (child mode) for physical Echo devices.

The whole parental-controls surface of ``AlexaAPI`` was unwrapped: five calls
that read the household's child profiles, report whether an Echo is in Amazon
Kids mode, and assign/unassign a device to a child.

Four things are worth knowing before extending this module:

* **A write returns NOTHING, so this module VERIFIES.**  ``enable_child_mode``
  and ``disable_child_mode`` are declared ``-> None`` and are wrapped in
  alexapy's ``_catch_all_exceptions``, which converts a failed request into a
  quiet ``None`` return.  The two outcomes are therefore indistinguishable at
  the call site.  Every write here re-reads :func:`read_state` afterwards and
  reports ``verified`` (the state Amazon actually holds now) alongside ``ok``,
  rather than claiming success because nothing raised.  This is the same rule
  ``devices delete --verify`` follows for native re-sync.
* **Enable rides a DIFFERENT host and a DIFFERENT csrf token.**  The parent
  dashboard lives on a localized subdomain (``parents.amazon.co.uk``,
  ``eltern.amazon.de``) and authenticates writes with ``ft-panda-csrf-token``
  echoed into ``x-amzn-csrf`` — not the ``csrf`` cookie every other mutating
  call in this harness uses.  alexapy seeds it by GETting the onboarding page
  first, and if the cookie is missing it logs at **debug** and posts anyway, so
  a rejected assign is silent.  That is precisely why the verify step exists.
* **"Unknown" is not "off".**  ``get_child_mode`` returns ``None`` when the
  state could not be read (unsupported device, changed payload), which is not
  the same answer as ``False``.  The rows keep ``None`` as ``None`` so a device
  that could not be read is never rendered as "kids mode is off".
* **These are STATIC calls that still need a device.**  Unlike media/bluetooth,
  the kids calls take ``login`` plus a bare ``serial``/``device_type`` rather
  than a bound ``AlexaAPI``.  A :class:`~cli_anything.alexa.core.device_ref.DeviceRef`
  is still used to resolve them — it is the one tested place that turns a name
  or serial into that pair — but no ``AlexaAPI`` instance is constructed.

Everything above the "live operations" divider is pure and unit-tested; only
the thin ``async def`` wrappers touch the network.
"""

from __future__ import annotations

import re
from typing import Any

from cli_anything.alexa.core.device_ref import DeviceRef
from cli_anything.alexa.core.devices_meta import fetch_devices
from cli_anything.alexa.core.media import resolve_device

#: Amazon's role marker for a child household member.  ``get_child_profiles``
#: already filters on it; kept here so :func:`profile_rows` can also be handed a
#: raw ``/ajax/get-household-with-age`` ``members`` list (mixed ADULT/CHILD) and
#: still do the right thing.
CHILD_ROLE = "CHILD"


# ── pure helpers ─────────────────────────────────────────────────────────


def normalize_name(value: Any) -> str:
    """Case/whitespace-insensitive form of a child's name (pure)."""
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip().lower()


def profile_rows(members: Any) -> list[dict[str, Any]]:
    """Flatten household members into child-profile rows (pure).

    Accepts either the already-filtered list ``get_child_profiles`` returns or a
    raw ``members`` list containing adults, which are dropped: only a member
    with an explicit ``role`` of something other than ``CHILD`` is excluded, so
    a payload that omits ``role`` entirely (already filtered) still comes back.
    """
    rows: list[dict[str, Any]] = []
    for member in members or []:
        if not isinstance(member, dict):
            continue
        role = member.get("role")
        if role is not None and role != CHILD_ROLE:
            continue
        rows.append(
            {
                "name": member.get("firstName"),
                "age": member.get("age"),
                "directedId": member.get("directedId"),
            }
        )
    return rows


def resolve_child(profiles: list[dict[str, Any]], target: str) -> list[dict[str, Any]]:
    """Resolve a ``<child>`` to profile rows (pure).

    Precedence (first tier that yields any match wins):
      1. exact ``directedId``
      2. exact ``name``
      3. normalized ``name``

    Returns the matches for the winning tier.  Siblings really can share a first
    name, so >1 is a legitimate outcome the caller must disambiguate by
    ``directedId`` — the same abort-and-list rule ``devices rename`` uses.
    """
    rows = [p for p in (profiles or []) if isinstance(p, dict)]
    raw = (target or "").strip()
    if not raw:
        return []
    hits = [p for p in rows if p.get("directedId") == raw]
    if hits:
        return hits
    hits = [p for p in rows if p.get("name") == raw]
    if hits:
        return hits
    wanted = normalize_name(raw)
    return [p for p in rows if normalize_name(p.get("name")) == wanted]


def child_labels(profiles: list[dict[str, Any]]) -> list[str]:
    """The child profiles' display labels, for an error message (pure)."""
    labels = []
    for p in profiles or []:
        if not isinstance(p, dict):
            continue
        label = p.get("name") or p.get("directedId")
        if label:
            labels.append(str(label))
    return labels


def no_child_error(target: str, profiles: list[dict[str, Any]]) -> str:
    """Caller-facing message for a child that could not be resolved (pure).

    Naming the alternatives matters: ``assign-device-to-child`` with an unknown
    ``childDirectedId`` is rejected server-side without a message reaching the
    caller, so an unresolvable name is refused locally instead.
    """
    known = child_labels(profiles)
    if not known:
        return (
            "no Amazon Kids child profiles exist on this account; create one in "
            "the Alexa app (parent dashboard) first"
        )
    return f"no child profile matching {target!r}; known: {', '.join(known)}"


def child_name_for(profiles: list[dict[str, Any]], directed_id: Any) -> str | None:
    """``directedId`` → child display name (pure). ``None`` when unmatched."""
    if not directed_id:
        return None
    for p in profiles or []:
        if isinstance(p, dict) and p.get("directedId") == directed_id:
            return p.get("name")
    return None


def status_row(
    device: dict[str, Any] | DeviceRef,
    enabled: Any,
    directed_id: Any,
    profiles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One Echo's Amazon Kids state as a display row (pure).

    ``kids`` stays ``None`` when the state could not be read — see the module
    docstring — instead of collapsing "unknown" into "off".
    """
    if isinstance(device, DeviceRef):
        name, serial = device.account_name, device.device_serial_number
    else:
        rec = device or {}
        name, serial = rec.get("accountName"), rec.get("serialNumber")
    return {
        "device": name,
        "serial": serial,
        "kids": None if enabled is None else ("on" if enabled else "off"),
        "child": child_name_for(profiles or [], directed_id),
        "childDirectedId": directed_id or None,
    }


# ── live operations ──────────────────────────────────────────────────────


async def fetch_profiles(login) -> list[dict[str, Any]]:
    """Every CHILD profile in the Amazon household."""
    from alexapy import AlexaAPI

    return profile_rows(await AlexaAPI.get_child_profiles(login))


async def read_state(login, serial: str, device_type: str) -> tuple[Any, Any]:
    """``(is_child_directed, child_directed_id)`` for one Echo.

    Two calls because Amazon splits the answer: ``/api/device/op-mode`` knows
    *whether* kids mode is on, ``/ajax/get-oobe-device-data`` knows *which*
    child the device is assigned to.
    """
    from alexapy import AlexaAPI

    enabled = await AlexaAPI.get_child_mode(login, serial, device_type)
    directed_id = await AlexaAPI.get_device_child(login, serial, device_type)
    return enabled, directed_id


async def device_status(login, device: str | None = None) -> dict[str, Any]:
    """Amazon Kids state of ONE Echo (default: the first online one)."""
    ref = await resolve_device(login, device)
    profiles = await fetch_profiles(login)
    enabled, directed_id = await read_state(login, ref.device_serial_number, ref._device_type)
    return status_row(ref, enabled, directed_id, profiles)


async def status_all(login) -> list[dict[str, Any]]:
    """Amazon Kids state of EVERY Echo on the account.

    Costs two requests per device (see :func:`read_state`), which is why the
    per-device form exists; an Echo whose state cannot be read still gets a row
    with ``kids: None`` so it is visibly "checked, unknown" rather than absent.
    """
    devices = await fetch_devices(login)
    if not devices:
        raise ValueError("no Alexa devices found on the account")
    profiles = await fetch_profiles(login)
    rows: list[dict[str, Any]] = []
    for record in devices:
        serial = (record.get("serialNumber") or "").strip()
        if not serial:
            # Unaddressable (DeviceRef would refuse it too) - skip rather than
            # emit a row implying a device that cannot be queried at all.
            continue
        enabled, directed_id = await read_state(login, serial, record.get("deviceType") or "")
        rows.append(status_row(record, enabled, directed_id, profiles))
    return rows


async def enable(login, device: str, child: str) -> dict[str, Any]:
    """Turn Amazon Kids on for one Echo by assigning it to a child profile.

    Verifies afterwards (the write itself reports nothing — see the module
    docstring) and returns ``ok`` reflecting the state Amazon actually holds.
    """
    wanted = (child or "").strip()
    if not wanted:
        raise ValueError("a child profile name or directedId is required")
    from alexapy import AlexaAPI

    ref = await resolve_device(login, device)
    profiles = await fetch_profiles(login)
    matches = resolve_child(profiles, wanted)
    if not matches:
        raise ValueError(no_child_error(wanted, profiles))
    if len(matches) > 1:
        ids = ", ".join(str(m.get("directedId")) for m in matches)
        raise ValueError(
            f"{wanted!r} matches {len(matches)} child profiles; pick one by directedId: {ids}"
        )
    target = matches[0]
    directed_id = target.get("directedId")
    if not directed_id:
        raise ValueError(f"child profile {wanted!r} has no directedId; cannot assign a device")
    await AlexaAPI.enable_child_mode(login, ref.device_serial_number, ref._device_type, directed_id)
    enabled, now_id = await read_state(login, ref.device_serial_number, ref._device_type)
    row = status_row(ref, enabled, now_id, profiles)
    row["requested"] = target.get("name") or directed_id
    row["ok"] = bool(enabled)
    return row


async def disable(login, device: str) -> dict[str, Any]:
    """Turn Amazon Kids off for one Echo (unassign it from any child profile)."""
    from alexapy import AlexaAPI

    ref = await resolve_device(login, device)
    await AlexaAPI.disable_child_mode(login, ref.device_serial_number, ref._device_type)
    enabled, now_id = await read_state(login, ref.device_serial_number, ref._device_type)
    profiles = await fetch_profiles(login)
    row = status_row(ref, enabled, now_id, profiles)
    # `enabled is None` means the verify read failed, NOT that kids mode is off.
    row["ok"] = enabled is False
    return row
