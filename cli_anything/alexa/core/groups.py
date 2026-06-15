"""Alexa smart-home device-groups (rooms) over the modern GraphQL API.

The legacy REST `/api/phoenix/group` endpoint is DEAD — it hard-401s with
``'at' and 'ubid' values required``. The current Alexa device-group / rooms
surface is **GraphQL** at ``/nexus/v1/graphql``. alexapy already talks to it,
so every call here goes through ``AlexaAPI._static_request`` — it sets the
auth/headers/host correctly. Do NOT hand-roll the host: the web host
``alexa.amazon.co.uk`` 401s for groups; only the nexus host works, and
``_static_request`` picks it.

IDs:
    group id    = ``amzn1.alexa.endpointGroup.<uuid>``
    endpoint id = ``amzn1.alexa.endpoint.<uuid>``  (a group's member device)

Member resolution maps HA ``<domain>.<object_id>`` entity ids to endpoint
ids via the ``endpoints`` query: each endpoint's
``legacyAppliance.applianceId`` ends in ``..._<domain>#<object_id>`` — the
same tail the appliances module already decodes (reused here).

GraphQL gotchas (reverse-engineered live; baked in below):
  * ``memberDeviceIds`` / ``associatedUnitIds`` are GraphQL ``[String!]``
    lists. They MUST be passed as real Python lists in ``variables`` so they
    serialize to JSON arrays. Passing a ``json.dumps``'d string makes GraphQL
    coerce the lone string into a 1-element list and the server SILENTLY
    no-ops (no error, nothing changes).
  * Do NOT send ``associatedUnitIds`` on create — it triggers
    ``BAD_REQUEST`` / non-201. Alexa auto-associates the unit from the member
    devices. Create takes ``friendlyName`` + ``memberDeviceIds`` only.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from cli_anything.alexa.core.appliances import parse_entity_id

# ── GraphQL documents ──────────────────────────────────────────────────────

_LIST_GROUPS_QUERY = (
    "query{ listDeviceGroups(listDeviceGroupsInput:{}){ deviceGroups{ "
    "id friendlyName{ value{ text } } memberDevices{ items{ id "
    "friendlyNameObject{ value{ text } } } } } } }"
)

_ENDPOINTS_QUERY = (
    "query{ endpoints(endpointsQueryParams:{paginationParams:"
    "{disablePagination:true}}){ items{ id legacyAppliance{ applianceId } } } }"
)

_CREATE_MUTATION = (
    "mutation($in:CreateDeviceGroupInput!){ "
    "createDeviceGroup(createDeviceGroupInput:$in){ __typename } }"
)

_UPDATE_MUTATION = (
    "mutation($in:UpdateDeviceGroupInput!){ "
    "updateDeviceGroup(updateDeviceGroupInput:$in){ __typename } }"
)

_DELETE_MUTATION = (
    "mutation($in:DeleteDeviceGroupInput!){ "
    "deleteDeviceGroup(deleteDeviceGroupInput:$in){ __typename } }"
)

# applianceId tail: ``..._<domain>#<object_id>`` -> HA <domain>.<object_id>.
# (parse_entity_id in the appliances module does the same split; reused.)
_APPLIANCE_TAIL_RE = re.compile(r"_([a-z_]+)#(.+)$")


# ── pure helpers ───────────────────────────────────────────────────────────

def normalize_name(name: str) -> str:
    """Normalize a group friendly name for case/space/punct-insensitive match."""
    if not name:
        return ""
    # keep only alphanumerics, lowercased — drops spaces, hyphens, punctuation.
    return re.sub(r"[^a-z0-9]+", "", name.strip().lower())


def group_rows(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten raw listDeviceGroups records to display rows (pure)."""
    out: list[dict[str, Any]] = []
    for g in groups or []:
        name = (((g.get("friendlyName") or {}).get("value") or {}).get("text"))
        members = ((g.get("memberDevices") or {}).get("items")) or []
        member_names = [
            (((m.get("friendlyNameObject") or {}).get("value") or {}).get("text"))
            for m in members
        ]
        out.append(
            {
                "id": g.get("id"),
                "name": name,
                "members": len(members),
                "memberNames": [n for n in member_names if n],
            }
        )
    return out


def find_group(groups: list[dict[str, Any]], name_or_id: str) -> Optional[dict[str, Any]]:
    """Match a raw group by id (exact) or friendly name (normalized), pure."""
    if not name_or_id:
        return None
    target = name_or_id.strip()
    # exact id first
    for g in groups or []:
        if g.get("id") == target:
            return g
    # normalized friendly-name match
    norm = normalize_name(target)
    for g in groups or []:
        name = (((g.get("friendlyName") or {}).get("value") or {}).get("text")) or ""
        if normalize_name(name) == norm:
            return g
    return None


def endpoint_map(endpoint_items: list[dict[str, Any]]) -> dict[str, str]:
    """Map HA ``<domain>.<object_id>`` entity id -> Alexa endpoint id (pure).

    Built from the ``endpoints`` query items. Each item's
    ``legacyAppliance.applianceId`` tail decodes to an HA entity id; only
    items that decode are included. Non-HA endpoints (no parseable tail) are
    skipped — they simply aren't addressable by entity id.
    """
    out: dict[str, str] = {}
    for item in endpoint_items or []:
        eid = item.get("id")
        appliance_id = ((item.get("legacyAppliance") or {}).get("applianceId")) or ""
        entity_id = parse_entity_id(appliance_id)
        if eid and entity_id:
            out[entity_id] = eid
    return out


def resolve_members(
    entities: list[str],
    endpoints: list[str],
    ent_map: dict[str, str],
) -> tuple[list[str], list[str]]:
    """Resolve ``--entity`` + ``--endpoint`` args to endpoint ids (pure).

    Returns ``(endpoint_ids, unresolved_entities)``. ``--endpoint`` values are
    passed through verbatim; ``--entity`` values are looked up in ``ent_map``.
    Order is preserved and duplicates de-duped while preserving first-seen
    order.
    """
    resolved: list[str] = []
    unresolved: list[str] = []
    for ent in entities or []:
        eid = ent_map.get(ent)
        if eid:
            resolved.append(eid)
        else:
            unresolved.append(ent)
    resolved.extend(endpoints or [])
    # de-dupe, preserve order
    seen: set[str] = set()
    deduped = [e for e in resolved if not (e in seen or seen.add(e))]
    return deduped, unresolved


# ── GraphQL variables builders (pure; the gotchas live HERE) ───────────────

def build_create_variables(name: str, member_ids: list[str]) -> dict[str, Any]:
    """Variables for createDeviceGroup.

    ``memberDeviceIds`` is a real Python list so it serializes to a JSON array
    (see module docstring — a string silently no-ops). ``associatedUnitIds``
    is deliberately OMITTED — sending it causes BAD_REQUEST; Alexa
    auto-associates the unit from the members.
    """
    return {"in": {"friendlyName": name, "memberDeviceIds": list(member_ids or [])}}


def build_update_variables(
    group_id: str,
    member_ids: list[str],
    operation: str,
) -> dict[str, Any]:
    """Variables for updateDeviceGroup.

    ``operation`` is ADD / REMOVE / REPLACE: REPLACE sets the full member set,
    ADD/REMOVE apply deltas. ``memberDeviceIds`` is a real list (not a string).
    """
    op = (operation or "").upper()
    if op not in ("ADD", "REMOVE", "REPLACE"):
        raise ValueError(f"operation must be ADD/REMOVE/REPLACE, got {operation!r}")
    return {
        "in": {
            "deviceGroupId": group_id,
            "memberDeviceIds": list(member_ids or []),
            "memberDeviceIdsUpdateOperation": op,
        }
    }


def build_delete_variables(group_id: str) -> dict[str, Any]:
    """Variables for deleteDeviceGroup."""
    return {"in": {"deviceGroupId": group_id}}


# ── network (alexapy GraphQL via _static_request) ──────────────────────────

async def _graphql(login, query: str, variables: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """POST a GraphQL doc to /nexus/v1/graphql via alexapy's static request.

    Reuses ``AlexaAPI._static_request`` so auth/headers/host are correct.
    Raises ``RuntimeError`` if the response carries GraphQL ``errors``.
    """
    from alexapy import AlexaAPI

    data: dict[str, Any] = {"query": query}
    if variables is not None:
        # Pass through verbatim: lists stay lists so they serialize as JSON
        # arrays (NOT json.dumps'd strings — that silently no-ops, see docstring).
        data["variables"] = variables
    resp = await AlexaAPI._static_request(
        "post", login, "/nexus/v1/graphql", data=data
    )
    body = json.loads(await resp.text())
    errors = body.get("errors")
    if errors:
        raise RuntimeError(f"GraphQL error: {json.dumps(errors)[:300]}")
    return body


async def fetch_groups(login) -> list[dict[str, Any]]:
    """Raw deviceGroups records from listDeviceGroups."""
    body = await _graphql(login, _LIST_GROUPS_QUERY)
    data = (body.get("data") or {}).get("listDeviceGroups") or {}
    return list(data.get("deviceGroups") or [])


async def list_groups(login) -> list[dict[str, Any]]:
    """Display rows for every smart-home group (name, id, member count/names)."""
    return group_rows(await fetch_groups(login))


async def fetch_endpoint_map(login) -> dict[str, str]:
    """Build the HA entity_id -> endpoint id map from the endpoints query."""
    body = await _graphql(login, _ENDPOINTS_QUERY)
    items = ((body.get("data") or {}).get("endpoints") or {}).get("items") or []
    return endpoint_map(list(items))


async def create_group(login, name: str, member_ids: list[str]) -> dict[str, Any]:
    """createDeviceGroup with friendlyName + memberDeviceIds (no unit ids)."""
    variables = build_create_variables(name, member_ids)
    body = await _graphql(login, _CREATE_MUTATION, variables)
    return {
        "created": name,
        "memberDeviceIds": variables["in"]["memberDeviceIds"],
        "result": (body.get("data") or {}).get("createDeviceGroup"),
    }


async def update_group(
    login, group_id: str, member_ids: list[str], operation: str
) -> dict[str, Any]:
    """updateDeviceGroup with an ADD/REMOVE/REPLACE member operation."""
    variables = build_update_variables(group_id, member_ids, operation)
    body = await _graphql(login, _UPDATE_MUTATION, variables)
    return {
        "deviceGroupId": group_id,
        "operation": variables["in"]["memberDeviceIdsUpdateOperation"],
        "memberDeviceIds": variables["in"]["memberDeviceIds"],
        "result": (body.get("data") or {}).get("updateDeviceGroup"),
    }


async def delete_group(login, group_id: str) -> dict[str, Any]:
    """deleteDeviceGroup by id."""
    variables = build_delete_variables(group_id)
    body = await _graphql(login, _DELETE_MUTATION, variables)
    return {
        "deviceGroupId": group_id,
        "result": (body.get("data") or {}).get("deleteDeviceGroup"),
    }
