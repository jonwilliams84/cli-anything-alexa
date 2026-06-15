"""Alexa smart-home groups (phoenix groups).

alexapy does NOT wrap the phoenix *group* endpoints, so this is a raw
authed-session reader. Listing is implemented; create/delete are stubbed
with a TODO because the create payload (endpoint + applianceIds + group
type) is undocumented and brittle — left for a future, verified pass.
"""

from __future__ import annotations

from typing import Any

from cli_anything.alexa.core.session import base_url


def group_rows(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten raw group records to display rows (pure)."""
    out: list[dict[str, Any]] = []
    for g in groups or []:
        members = g.get("applianceIds") or g.get("childIds") or []
        out.append(
            {
                "id": g.get("groupId") or g.get("entityId"),
                "name": g.get("name") or g.get("groupName"),
                "type": g.get("groupType") or g.get("entityType"),
                "members": len(members),
            }
        )
    return out


async def list_groups(login) -> list[dict[str, Any]]:
    """List smart-home groups via /api/phoenix/group."""
    url = f"{base_url(login.url)}/api/phoenix/group"
    async with login.session.get(url) as resp:
        if resp.status != 200:
            return []
        data = await resp.json(content_type=None)
    groups = []
    if isinstance(data, dict):
        groups = data.get("applianceGroups") or data.get("groups") or []
    elif isinstance(data, list):
        groups = data
    return group_rows(groups)


# TODO: create_group / delete_group.
# The phoenix group create/delete payloads (POST/DELETE /api/phoenix/group)
# require an undocumented body shape (group type + applianceId list +
# entity references). Implement + verify against the live account before
# shipping a mutating path here.
