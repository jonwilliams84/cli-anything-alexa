"""Routines (Alexa "behaviors" automations).

List via `AlexaAPI.get_automations`; trigger via the device-bound
`run_routine(utterance)` (alexapy posts the routine's sequence to
`/api/behaviors/preview`). Matching name->routine + trigger/action parsing is
pure logic.

**Editing an existing ROUTINE is not supported via the API.** Amazon hard-
refuses every write path: ``updateAutomation`` returns *"not supported for
automation type: ROUTINE"*; ``batchUpdateAutomations`` requires an opaque
scripted-source blob that the read API (``/api/behaviors/v2/automations``)
never returns; and a REST ``PUT`` to the automation 404s. So routine edits are
**Alexa-app-only**. This module lists routines (with their trigger utterance and
a best-effort action-target summary) and can *trigger* one — it does not mutate
them.
"""

from __future__ import annotations

from typing import Any, Optional


def _node_summary(node: dict[str, Any]) -> Optional[str]:
    """Summarize one action node from a routine sequence (pure, best-effort)."""
    node = node or {}
    ntype = node.get("type") or (node.get("@type") or "").rsplit(".", 1)[-1]
    payload = node.get("operationPayload") or {}
    target = payload.get("target") or payload.get("targets")
    ops = payload.get("operations") or []
    op_types = [o.get("type") for o in ops if isinstance(o, dict) and o.get("type")]
    parts: list[str] = []
    if ntype:
        parts.append(str(ntype))
    if op_types:
        parts.append("/".join(op_types))
    if target:
        parts.append(f"-> {target if isinstance(target, str) else str(target)}")
    return " ".join(parts) if parts else None


def action_targets(automation: dict[str, Any]) -> list[str]:
    """Best-effort list of action-node summaries for a routine (pure).

    Walks ``sequence.startNode.nodesToExecute`` (the serial action list),
    summarising each node by its type, operations, and SmartHome target id.
    Nodes that don't parse are skipped — this is a convenience surfacing, not a
    complete decode of Alexa's opaque action payloads.
    """
    seq = (automation or {}).get("sequence") or {}
    start = seq.get("startNode") or {}
    nodes = start.get("nodesToExecute")
    if nodes is None and start:
        nodes = [start]
    out: list[str] = []
    for n in nodes or []:
        s = _node_summary(n)
        if s:
            out.append(s)
    return out


def routine_rows(automations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten raw automation records to display rows (pure).

    Surfaces the trigger ``utterance`` and a best-effort ``actions`` summary
    (action-node types / operations / SmartHome target id).
    """
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
                "actions": action_targets(a),
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
