"""Routines (Alexa "behaviors" automations).

List via `AlexaAPI.get_automations`; trigger via the device-bound
`run_routine(utterance)` (alexapy posts the routine's sequence to
`/api/behaviors/preview`). Matching name->routine is pure logic.
"""

from __future__ import annotations

from typing import Any, Optional


def routine_rows(automations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten raw automation records to display rows (pure)."""
    out: list[dict[str, Any]] = []
    for a in automations or []:
        triggers = a.get("triggers") or []
        utterance = None
        for t in triggers:
            payload = (t or {}).get("payload") or {}
            utterance = payload.get("utterance")
            if utterance:
                break
        out.append(
            {
                "id": a.get("automationId"),
                "name": a.get("name"),
                "status": a.get("status"),
                "utterance": utterance,
            }
        )
    return out


def find_routine(automations: list[dict[str, Any]], name_or_id: str) -> Optional[dict[str, Any]]:
    """Match a routine by automationId or by name/utterance (ci, pure)."""
    if not name_or_id:
        return None
    target = name_or_id.strip().lower()
    for a in automations or []:
        if (a.get("automationId") or "").lower() == target:
            return a
    for a in automations or []:
        if (a.get("name") or "").strip().lower() == target:
            return a
    # last resort: match the utterance
    for row in routine_rows(automations):
        if (row.get("utterance") or "").strip().lower() == target:
            for a in automations:
                if a.get("automationId") == row["id"]:
                    return a
    return None


async def list_routines(login) -> list[dict[str, Any]]:
    from alexapy import AlexaAPI

    data = await AlexaAPI.get_automations(login)
    return routine_rows(list(data or []))


async def run_routine(login, name_or_id: str) -> dict[str, Any]:
    """Trigger a routine by name/id.

    alexapy's `run_routine` takes the routine *utterance* and a device to
    run it against; we pick the first available Echo as the runner.
    """
    from alexapy import AlexaAPI

    automations = list(await AlexaAPI.get_automations(login) or [])
    routine = find_routine(automations, name_or_id)
    if not routine:
        raise ValueError(f"no routine matching {name_or_id!r}")

    devices = list(await AlexaAPI.get_devices(login) or [])
    runner = next((d for d in devices if d.get("online")), None) or (
        devices[0] if devices else None
    )
    if not runner:
        raise ValueError("no Alexa device available to run the routine on")

    rows = routine_rows([routine])
    utterance = rows[0].get("utterance") or rows[0].get("name")
    api = AlexaAPI(runner, login)
    await api.run_routine(utterance)
    return {
        "triggered": routine.get("name"),
        "automationId": routine.get("automationId"),
        "via_device": runner.get("accountName"),
        "utterance": utterance,
    }
