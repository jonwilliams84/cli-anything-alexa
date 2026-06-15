"""Endpoint-level operations over the modern Alexa **GraphQL** API.

The ``endpoints`` query is the one source of truth that ties together the three
ids a device carries:

    * ``id``                          = the GraphQL **endpoint id**
                                        ``amzn1.alexa.endpoint.<uuid>`` — used by
                                        groups + ``setEndpointFriendlyName`` (rename).
    * ``legacyAppliance.applianceId`` = the **applianceId** used by the phoenix
                                        DELETE. For HA-sourced devices its tail
                                        ``_<domain>#<object_id>`` decodes back to
                                        the HA entity (see ``appliances.parse_entity_id``).
    * ``friendlyNameObject.value.text`` = the current **display name**.

``manufacturerName == "Home Assistant"`` marks an HA-sourced device; anything
else (e.g. ``"Belkin International Inc."`` for Tasmota-Wemo plugs) is native and
has no HA entity — such devices can only be targeted by display name.

All network calls go through ``AlexaAPI._static_request`` so auth/headers/host
are correct (the nexus host, NOT the web host — see ``groups.py``). The pure
resolution / duplicate-detection logic below has no ``alexapy`` dependency and
is unit-tested.

Reachability note: the ``Endpoint`` GraphQL type exposes ``enablement`` (a clean
enum, e.g. ``ENABLED``) plus ``connections`` / ``endpointReports``. Only
``enablement`` introspected as a clean, consistently-present scalar, so
``device_rows`` surfaces it as ``enabled``; a true online/reachability column was
deliberately skipped because the nested ``connections`` / ``endpointReports``
shapes were not consistently available on the live account.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from cli_anything.alexa.core.appliances import HA_MANUFACTURER, parse_entity_id
from cli_anything.alexa.core.groups import normalize_name

# ── GraphQL documents ──────────────────────────────────────────────────────

# The canonical device query — reused for rename / delete-by-name|entity /
# duplicates / groups --device. Pulls every id a device carries.
ENDPOINTS_QUERY = (
    "query{ endpoints(endpointsQueryParams:{paginationParams:"
    "{disablePagination:true}}){ items{ id legacyAppliance{ applianceId "
    "manufacturerName friendlyName } friendlyNameObject{ value{ text } } "
    "enablement } } }"
)

_RENAME_MUTATION = (
    "mutation($in:SetEndpointFriendlyNameInput!){ "
    "setEndpointFriendlyName(input:$in){ __typename } }"
)


# ── pure helpers: flatten + classify ───────────────────────────────────────

def endpoint_record(item: dict[str, Any]) -> dict[str, Any]:
    """Flatten one raw ``endpoints`` item into a stable record (pure).

    The display name prefers ``friendlyNameObject.value.text`` (what the app
    shows) and falls back to ``legacyAppliance.friendlyName``.
    """
    item = item or {}
    legacy = item.get("legacyAppliance") or {}
    appliance_id = legacy.get("applianceId") or ""
    manufacturer = legacy.get("manufacturerName")
    display = (((item.get("friendlyNameObject") or {}).get("value") or {})
               .get("text")) or legacy.get("friendlyName")
    ha_sourced = manufacturer == HA_MANUFACTURER
    entity_id = parse_entity_id(appliance_id) if ha_sourced else None
    return {
        "endpointId": item.get("id"),
        "applianceId": appliance_id,
        "name": display,
        "manufacturer": manufacturer,
        "ha_sourced": ha_sourced,
        "entity_id": entity_id,
        "enabled": item.get("enablement"),
    }


def endpoint_records(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten a list of raw ``endpoints`` items to records (pure)."""
    return [endpoint_record(it) for it in (items or [])]


def device_rows(
    records: list[dict[str, Any]],
    *,
    native_only: bool = False,
    manufacturer: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Display rows for ``devices list`` (pure).

    Adds a ``source`` marker (``HA`` vs ``native``) and supports the
    ``--native-only`` / ``--manufacturer <substr>`` filters. ``manufacturer`` is
    a case-insensitive substring match.
    """
    out: list[dict[str, Any]] = []
    man_sub = (manufacturer or "").strip().lower()
    for r in records or []:
        if native_only and r.get("ha_sourced"):
            continue
        if man_sub and man_sub not in (r.get("manufacturer") or "").lower():
            continue
        out.append(
            {
                "name": r.get("name"),
                "manufacturer": r.get("manufacturer"),
                "source": "HA" if r.get("ha_sourced") else "native",
                "enabled": r.get("enabled"),
                "entity_id": r.get("entity_id"),
                "applianceId": r.get("applianceId"),
                "endpointId": r.get("endpointId"),
            }
        )
    return out


# ── pure helpers: target resolution ────────────────────────────────────────

def resolve_target(records: list[dict[str, Any]], target: str) -> list[dict[str, Any]]:
    """Resolve a ``<target>`` to matching endpoint records (pure).

    Precedence (first tier that yields any match wins):
      1. exact ``applianceId``
      2. exact endpoint id (``amzn1.alexa.endpoint.*``)
      3. exact display name (case-sensitive)
      4. normalized / case-insensitive display name

    Returns the list of matches for the winning tier. The caller decides what
    to do with 0 (no such device), 1 (resolved), or >1 (ambiguous — abort and
    disambiguate). A native + HA twin can legitimately share a display name, so
    name-based tiers may return more than one.
    """
    records = records or []
    t = (target or "").strip()
    if not t:
        return []
    # 1. exact applianceId
    hits = [r for r in records if r.get("applianceId") == t]
    if hits:
        return hits
    # 2. exact endpoint id
    hits = [r for r in records if r.get("endpointId") == t]
    if hits:
        return hits
    # 3. exact display name
    hits = [r for r in records if r.get("name") == t]
    if hits:
        return hits
    # 4. normalized display name
    norm = normalize_name(t)
    if norm:
        hits = [r for r in records if normalize_name(r.get("name") or "") == norm]
        if hits:
            return hits
    return []


def resolve_by_entity(records: list[dict[str, Any]], entity_id: str) -> list[dict[str, Any]]:
    """Resolve an HA ``<domain>.<object_id>`` entity id to records (pure)."""
    ent = (entity_id or "").strip()
    if not ent:
        return []
    return [r for r in (records or []) if r.get("entity_id") == ent]


def resolve_by_name(records: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    """Resolve a device by normalized display name -> records (pure).

    This is how native / non-HA devices (e.g. Tasmota-Wemo plugs) are targeted —
    they have no HA entity, only a friendly name.
    """
    norm = normalize_name(name or "")
    if not norm:
        return []
    return [r for r in (records or []) if normalize_name(r.get("name") or "") == norm]


def ambiguous_matches(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compact descriptor rows for an ambiguity abort message (pure)."""
    return [
        {
            "name": r.get("name"),
            "source": "HA" if r.get("ha_sourced") else "native",
            "manufacturer": r.get("manufacturer"),
            "entity_id": r.get("entity_id"),
            "applianceId": r.get("applianceId"),
            "endpointId": r.get("endpointId"),
        }
        for r in (records or [])
    ]


# ── pure helpers: duplicate detection ──────────────────────────────────────

def find_duplicates(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Detect the same physical device exposed twice (pure).

    Groups records by normalized display name and reports any name shared by
    more than one endpoint. Each reported pair/cluster flags whether it is the
    classic native+HA twin (a native appliance and an HA-sourced appliance under
    the same name — usually the thing to de-dupe). The output lets a human
    decide which copy to drop; nothing is deleted here.
    """
    by_name: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for r in records or []:
        norm = normalize_name(r.get("name") or "")
        if not norm:
            continue
        if norm not in by_name:
            by_name[norm] = []
            order.append(norm)
        by_name[norm].append(r)

    out: list[dict[str, Any]] = []
    for norm in order:
        group = by_name[norm]
        if len(group) < 2:
            continue
        has_ha = any(g.get("ha_sourced") for g in group)
        has_native = any(not g.get("ha_sourced") for g in group)
        out.append(
            {
                "name": group[0].get("name"),
                "count": len(group),
                "native_plus_ha": bool(has_ha and has_native),
                "endpoints": [
                    {
                        "source": "HA" if g.get("ha_sourced") else "native",
                        "manufacturer": g.get("manufacturer"),
                        "entity_id": g.get("entity_id"),
                        "applianceId": g.get("applianceId"),
                        "endpointId": g.get("endpointId"),
                    }
                    for g in group
                ],
            }
        )
    return out


# ── pure helpers: GraphQL variables builders ───────────────────────────────

def build_rename_variables(endpoint_id: str, friendly_name: str) -> dict[str, Any]:
    """Variables for ``setEndpointFriendlyName`` (pure)."""
    return {"in": {"endpointId": endpoint_id, "friendlyName": friendly_name}}


# ── network (alexapy GraphQL / phoenix via _static_request) ────────────────

async def _graphql(login, query: str, variables: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """POST a GraphQL doc to /nexus/v1/graphql via alexapy's static request.

    Reuses ``AlexaAPI._static_request`` so auth/headers/host are correct.
    Raises ``RuntimeError`` if the response carries GraphQL ``errors``.
    """
    from alexapy import AlexaAPI

    data: dict[str, Any] = {"query": query}
    if variables is not None:
        data["variables"] = variables
    resp = await AlexaAPI._static_request(
        "post", login, "/nexus/v1/graphql", data=data
    )
    body = json.loads(await resp.text())
    errors = body.get("errors")
    if errors:
        raise RuntimeError(f"GraphQL error: {json.dumps(errors)[:300]}")
    return body


async def fetch_endpoints(login) -> list[dict[str, Any]]:
    """Raw items from the canonical ``endpoints`` query."""
    body = await _graphql(login, ENDPOINTS_QUERY)
    items = ((body.get("data") or {}).get("endpoints") or {}).get("items") or []
    return list(items)


async def fetch_endpoint_records(login) -> list[dict[str, Any]]:
    """Flattened endpoint records (id/applianceId/name/manufacturer/entity)."""
    return endpoint_records(await fetch_endpoints(login))


async def rename_endpoint(login, endpoint_id: str, friendly_name: str) -> dict[str, Any]:
    """setEndpointFriendlyName — rename a device by its endpoint id."""
    variables = build_rename_variables(endpoint_id, friendly_name)
    body = await _graphql(login, _RENAME_MUTATION, variables)
    return {
        "endpointId": endpoint_id,
        "friendlyName": friendly_name,
        "result": (body.get("data") or {}).get("setEndpointFriendlyName"),
    }
