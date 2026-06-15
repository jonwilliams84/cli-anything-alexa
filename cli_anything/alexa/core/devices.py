"""Smart-home appliance (device) operations against the live account.

`list_appliances` reads the phoenix smart-home graph via alexapy;
`delete_appliance` issues the raw ``DELETE /api/phoenix/appliance/<id>``
that alexapy doesn't wrap. Pure parsing/whitelist logic lives in
``appliances.py`` and is reused here.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote

from cli_anything.alexa.core import appliances as appliances_pure
from cli_anything.alexa.core.session import (
    AlexaSessionError,
    base_url,
    csrf_header,
)


async def fetch_appliances(login) -> list[dict[str, Any]]:
    """Raw appliance records from the phoenix smart-home graph."""
    from alexapy import AlexaAPI

    data = await AlexaAPI.get_network_details(login)
    if not data:
        return []
    # get_network_details may return the raw graph dict or a list of
    # appliances depending on alexapy version; normalise to a list.
    if isinstance(data, dict):
        net = data.get("networkDetail", {}).get("locationDetails", {})
        out: list[dict[str, Any]] = []
        _collect_appliances(net, out)
        if out:
            return out
        # fallback: some versions nest under "applianceDetails"
        appliance_map = data.get("applianceDetails", {}).get("applianceDetails", {})
        if appliance_map:
            return list(appliance_map.values())
        return []
    return list(data)


def _collect_appliances(node: Any, out: list[dict[str, Any]]) -> None:
    """Recursively gather appliance dicts from the nested phoenix graph."""
    if isinstance(node, dict):
        appliances = node.get("amazonBridgeDetails")
        if isinstance(appliances, dict):
            for bridge in appliances.get("amazonBridgeDetails", {}).values():
                details = bridge.get("applianceDetails", {})
                inner = details.get("applianceDetails", {})
                if isinstance(inner, dict):
                    out.extend(inner.values())
        for value in node.values():
            _collect_appliances(value, out)
    elif isinstance(node, list):
        for item in node:
            _collect_appliances(item, out)


async def list_appliances(login) -> list[dict[str, Any]]:
    """Display rows for every appliance (HA + native)."""
    raw = await fetch_appliances(login)
    return appliances_pure.list_appliance_rows(raw)


async def delete_appliance(login, appliance_id: str) -> dict[str, Any]:
    """Delete a single appliance via the raw phoenix endpoint.

    200 with an empty body == deleted. Adds the required `csrf` header.
    """
    url = f"{base_url(login.url)}/api/phoenix/appliance/{quote(appliance_id, safe='')}"
    headers = csrf_header(login)
    if not headers:
        raise AlexaSessionError(
            "no csrf cookie on the session — cannot perform a mutating call"
        )
    async with login.session.delete(url, headers=headers) as resp:
        body = await resp.text()
        ok = resp.status == 200
        return {
            "applianceId": appliance_id,
            "status": resp.status,
            "deleted": ok,
            "body": body[:200] if body else "",
        }


async def trigger_discovery(login) -> dict[str, Any]:
    """Trigger Alexa smart-home device discovery.

    Not GraphQL — a raw ``POST /api/phoenix/discovery`` on the web host with the
    csrf header. Returns ``200 {}`` on success. Adds the required `csrf` header.
    """
    url = f"{base_url(login.url)}/api/phoenix/discovery"
    headers = csrf_header(login)
    if not headers:
        raise AlexaSessionError(
            "no csrf cookie on the session — cannot perform a mutating call"
        )
    async with login.session.post(url, headers=headers) as resp:
        body = await resp.text()
        return {
            "discovery": "triggered" if resp.status == 200 else "failed",
            "status": resp.status,
            "body": body[:200] if body else "",
        }


async def verify_deletes(
    login, deleted: list[dict[str, Any]], wait_seconds: float = 12.0
) -> dict[str, Any]:
    """After deletes, re-discover + re-query endpoints to find re-synced devices.

    Native devices re-sync from their cloud skill/bridge, so a delete may not
    stick. This triggers a discovery, waits, re-queries the canonical
    ``endpoints`` graph, and reports which just-deleted devices re-appeared (so
    the user knows which need source-side removal). Pure diff logic lives in
    ``endpoints.reappeared_after_delete``.
    """
    # imported here to avoid a module-load cycle (endpoints imports groups, etc.)
    from cli_anything.alexa.core import endpoints as endpoints_core

    disc = await trigger_discovery(login)
    await asyncio.sleep(wait_seconds)
    records_after = await endpoints_core.fetch_endpoint_records(login)
    reappeared = endpoints_core.reappeared_after_delete(deleted, records_after)
    return {
        "discovery": disc.get("discovery"),
        "waited_seconds": wait_seconds,
        "checked": len(deleted or []),
        "reappeared": reappeared,
    }
