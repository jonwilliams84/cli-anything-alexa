"""Pure logic for Alexa smart-home appliances.

These helpers are intentionally free of any network / `alexapy` dependency
so they can be unit-tested in isolation. They parse the appliance records
returned by `AlexaAPI.get_network_details`, map Home-Assistant-sourced
appliances back to their HA entity ids, and decide which orphans to prune
against a whitelist.

Appliance-id shape (HA Emulated Smart Home bridge / `alexa: smart_home:`):
    <prefix>..._<domain>#<object_id>
e.g.  "amzn1.alexa.appliance.AAA_light#kitchen_lamp"  ->  "light.kitchen_lamp"
The HA bridge encodes the entity id as the TAIL after the last underscore,
with the domain/object separated by `#` (HA replaces the entity-id `.`
with `#` so it survives Alexa's id grammar).
"""

from __future__ import annotations

from typing import Any, Optional

HA_MANUFACTURER = "Home Assistant"


def parse_entity_id(appliance_id: str) -> Optional[str]:
    """Decode the HA entity id from an Alexa applianceId, or None.

    The HA-sourced applianceId always ends in `_<domain>#<object_id>`.
    Returns ``"<domain>.<object_id>"`` or ``None`` when the tail does not
    look like an HA entity reference (e.g. a native Hue/Wemo appliance).
    """
    if not appliance_id or "#" not in appliance_id:
        return None
    # Split into "<...prefix>_<domain>" and "<object_id>" at the `#`.
    # The domain is the chunk after the LAST underscore that precedes the `#`;
    # the object_id (which may itself contain underscores) follows the `#`.
    head, _, object_id = appliance_id.partition("#")
    domain = head.rsplit("_", 1)[-1]
    if not domain or not object_id:
        return None
    # Domains are lowercase identifiers; reject anything that clearly isn't.
    if not domain.replace("_", "").isalnum():
        return None
    return f"{domain}.{object_id}"


def is_ha_sourced(appliance: dict[str, Any]) -> bool:
    """True when the appliance was created by the Home Assistant bridge."""
    return (appliance or {}).get("manufacturerName") == HA_MANUFACTURER


def appliance_row(appliance: dict[str, Any]) -> dict[str, Any]:
    """Flatten one raw appliance record into a stable display/JSON row."""
    appliance = appliance or {}
    appliance_id = appliance.get("applianceId") or appliance.get("entityId") or ""
    entity_id = parse_entity_id(appliance_id) if is_ha_sourced(appliance) else None
    return {
        "applianceId": appliance_id,
        "friendlyName": appliance.get("friendlyName"),
        "manufacturer": appliance.get("manufacturerName"),
        "model": appliance.get("modelName"),
        "ha_sourced": is_ha_sourced(appliance),
        "entity_id": entity_id,
    }


def list_appliance_rows(appliances: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map a list of raw appliance records to display rows."""
    return [appliance_row(a) for a in (appliances or [])]


def load_whitelist(text: str) -> set[str]:
    """Parse a whitelist file body into a set of entity ids.

    One entity id per line. Blank lines and `#`-comments are ignored.
    Surrounding whitespace and a trailing inline `# comment` are stripped.
    """
    out: set[str] = set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # allow a trailing inline comment: "light.foo  # some note"
        line = line.split("#", 1)[0].strip()
        if line:
            out.add(line)
    return out


def plan_prune(
    appliances: list[dict[str, Any]],
    whitelist: set[str],
) -> dict[str, list[dict[str, Any]]]:
    """Decide which HA-sourced appliances to delete vs keep.

    Only Home-Assistant-sourced appliances are candidates for pruning.
    A candidate is DELETED when its mapped entity id is NOT in the
    whitelist (or its id couldn't be parsed to an entity). Non-HA
    appliances (Hue/Wemo/Tuya/etc.) are always kept and never touched.

    Returns ``{"delete": [...rows...], "keep": [...rows...], "skipped": [...]}``
    where ``skipped`` is the non-HA appliances left untouched.
    """
    delete: list[dict[str, Any]] = []
    keep: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for raw in appliances or []:
        row = appliance_row(raw)
        if not row["ha_sourced"]:
            skipped.append(row)
            continue
        entity_id = row["entity_id"]
        if entity_id and entity_id in whitelist:
            keep.append(row)
        else:
            delete.append(row)
    return {"delete": delete, "keep": keep, "skipped": skipped}
