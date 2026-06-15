"""Smart-home appliance (device) operations against the live account.

`list_appliances` reads the phoenix smart-home graph via alexapy;
`delete_appliance` issues the raw ``DELETE /api/phoenix/appliance/<id>``
that alexapy doesn't wrap. Pure parsing/whitelist logic lives in
``appliances.py`` and is reused here.
"""

from __future__ import annotations

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
