"""Smart-home **scenes** — the Alexa app's scene buttons, over the phoenix
state API.

Scenes are exposed to the smart-home graph as appliances whose
``applianceTypes`` carry the ``SCENE`` marker (Amazon's own scene vocabulary;
``SMARTPLAY`` is the sibling "smart play" marker the app treats the same way).
The harness could already *inventory* them through the canonical endpoints
query, but it could never *tap* one — until now, scenes were the last
``SCENE``-typed capability with a CLI read but no write.

The write rides the exact ``controlRequests`` PUT that already drives the
lights (``turnOn``/``setBrightness``), Guard (``controlSecurityPanel``), locks
(``lock``/``unlock``) and thermostats (``setTargetSetpoint``/…):

    PUT /api/phoenix/state
    {"controlRequests": [{"entityId": "…", "entityType": "ENTITY",
                          "parameters": {"action": "Activate"}}]}

``Activate`` is the one ``Alexa.SceneController`` verb, and alexapy's request
builders ship none of the ``Alexa.SceneController`` surface, so — like the
lock (0.8.0) and thermostat (0.9.0) surfaces — the request is built locally
and sent through ``AlexaAPI._static_request`` (the same reuse-the-helper
pattern as ``groups``/``lists``), addressed by the **entityId** with
``entityType: ENTITY``.

Two honest-reporting notes, mirroring the lock/thermostat conventions:

* A scene write's own response is a bare control response — it answers
  nothing.  Unlike a lock there is also **no persistent state to re-read**
  (a scene is instantaneous, not a held state), so there is no verify re-read;
  instead the harness reads the *response* itself: ``ok`` is ``True``/``False``
  from the ``controlResponses[].code`` Amazon answers with, and ``None`` when
  the response carries no ``controlResponses`` at all ("nothing to check",
  never a silent pass).
* Scenes with no phoenix ``entityId`` are unusable — :func:`scene_rows` keeps
  listing them (so the inventory stays honest) and :func:`entity_ref` refuses
  them at activation time. Everything above the "network" divider is pure and
  unit-tested; only the two thin ``async def`` wrappers touch the network.
"""

from __future__ import annotations

from typing import Any

# ── scene detection (pure) ───────────────────────────────────────────────

#: ``applianceTypes`` markers the Alexa app treats as tappable scenes.
#: ``SCENE`` is the Amazon scene vocabulary; ``SMARTPLAY`` is its sibling
#: (Harmony-style "smart play" one-tap actions) and gets the same control.
SCENE_APPLIANCE_TYPES: tuple[str, ...] = ("SCENE", "SMARTPLAY")


def is_scene(record: dict[str, Any]) -> bool:
    """Does this endpoint record look like a scene appliance? (pure).

    True when the record's ``applianceTypes`` contains one of the scene
    markers. Type-safe: a non-dict record, a missing list, or a string
    ``applianceTypes`` is "not a scene" rather than a traceback.
    """
    types = record.get("applianceTypes") if isinstance(record, dict) else None
    if isinstance(types, str):
        types = [types]
    if not isinstance(types, (list, tuple)):
        return False
    return any(str(t).strip().upper() in set(SCENE_APPLIANCE_TYPES) for t in types)


def scene_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten + filter endpoint records to scene rows for `scenes list` (pure).

    Only scene appliances, sorted by display name (case-insensitive) so the
    listing is stable. The row keeps the names every consumer needs: the
    display ``name``, the phoenix ``entityId`` the activation PUT addresses,
    the ``applianceId``/``endpointId`` for disambiguation, and a ``source``
    marker (``HA`` vs ``native``) matching `devices list`. Records with no
    entityId are listed too — with ``entityId: null`` — because a scene the
    CLI cannot activate is still a scene the user should see.
    """
    out: list[dict[str, Any]] = []
    for rec in records or []:
        if not isinstance(rec, dict) or not is_scene(rec):
            continue
        out.append(
            {
                "name": rec.get("name"),
                "entityId": rec.get("entityId") or None,
                "applianceId": rec.get("applianceId") or None,
                "endpointId": rec.get("endpointId") or None,
                "manufacturer": rec.get("manufacturer"),
                "source": "HA" if rec.get("ha_sourced") else "native",
            }
        )
    out.sort(key=lambda row: (row.get("name") or "").lower())
    return out


def scene_control_request(entity_id: str) -> dict[str, Any]:
    """The ``Activate`` controlRequest PUT body for one scene (pure).

    The exact shape the Alexa app sends when a scene button is tapped — the
    same ``controlRequests`` family the locks/thermostat surfaces build. Kept
    as its own pure builder so tests pin the wire format without network.
    """
    return {
        "controlRequests": [
            {
                "entityId": entity_id,
                "entityType": "ENTITY",
                "parameters": {"action": "Activate"},
            }
        ]
    }


def activate_verify(response: Any) -> bool | None:
    """Three-valued ``ok`` read off the scene write's own response (pure).

    A scene activation has no held state to re-read, so the honest check is
    the code Amazon answers the control request with:

    * ``True``  — every ``controlResponses[].code`` is ``"SUCCESS"``;
    * ``False`` — at least one is not (``"NOENT"``, ``"INTERNAL_ERROR"`` …);
    * ``None``  — the response carries no ``controlResponses`` (an empty or
      unrecognised body): "nothing to check", never a silent pass.
    """
    responses = (response or {}).get("controlResponses") if isinstance(response, dict) else None
    if not responses:
        return None
    codes = [str(entry.get("code")).upper() for entry in responses if isinstance(entry, dict)]
    if not codes:
        return None
    return all(code == "SUCCESS" for code in codes)


# ── network (thin async wrappers over _static_request) ───────────────────


async def fetch_scenes(login) -> list[dict[str, Any]]:
    """Scene endpoint records from the canonical ``endpoints`` query."""
    from cli_anything.alexa.core import endpoints as endpoints_core

    records = await endpoints_core.fetch_endpoint_records(login)
    return [rec for rec in records if is_scene(rec)]


async def activate_scene(login, entity_id: str, name: str | None = None) -> dict[str, Any]:
    """Send one scene ``Activate`` controlRequest (``PUT /api/phoenix/state``).

    Rides ``AlexaAPI._static_request`` — alexapy has no ``Alexa.SceneController``
    builder — so auth/headers/host match every other raw call in the harness.
    The response is parsed (never raises on non-JSON bodies, like the lock
    writer) and :func:`activate_verify` derives the three-valued ``ok``:
    reports back ``{"name", "entityId", "action", "ok", "response"}``.
    """
    import json as _json

    from alexapy import AlexaAPI

    resp = await AlexaAPI._static_request(
        "put", login, "/api/phoenix/state", data=scene_control_request(entity_id)
    )
    if resp is None:
        raise RuntimeError("the scene control request returned no response")
    text = await resp.text()
    try:
        body = _json.loads(text)
    except (TypeError, ValueError):
        body = {}
    if not isinstance(body, dict):
        body = {}
    return {
        "name": name,
        "entityId": entity_id,
        "action": "Activate",
        "ok": activate_verify(body),
        "response": body,
    }
