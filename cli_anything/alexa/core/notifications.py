"""Alarms / timers / reminders (the `/api/notifications` surface).

`get_notifications` is wrapped by alexapy; alexapy only exposes the PUT
(edit) for writes, so create (POST) and delete (DELETE) are issued as raw
authed-session calls with the `csrf` header — matching the proven endpoint
notes. The payload builders here are kept pure where practical.
"""

from __future__ import annotations

import time
from typing import Any, Optional
from urllib.parse import quote

from cli_anything.alexa.core.session import (
    AlexaSessionError,
    base_url,
    csrf_header,
)


def notification_rows(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten raw notification records to display rows (pure)."""
    out: list[dict[str, Any]] = []
    for n in items or []:
        out.append(
            {
                "id": n.get("notificationIndex") or n.get("id"),
                "type": n.get("type"),
                "status": n.get("status"),
                "label": n.get("reminderLabel") or n.get("originalLabel") or n.get("timerLabel"),
                "deviceSerial": n.get("deviceSerialNumber"),
                "alarmTime": n.get("alarmTime") or n.get("originalTime"),
                "remaining": n.get("remainingTime"),
            }
        )
    return out


def _epoch_ms(seconds_from_now: Optional[float] = None,
              at_epoch_ms: Optional[int] = None) -> int:
    if at_epoch_ms is not None:
        return int(at_epoch_ms)
    return int((time.time() + (seconds_from_now or 0)) * 1000)


def build_reminder(label: str, device_serial: str, device_type: str,
                   at_epoch_ms: int) -> dict[str, Any]:
    """Build a Reminder creation payload (pure)."""
    return {
        "type": "Reminder",
        "status": "ON",
        "alarmTime": int(at_epoch_ms),
        "originalTime": None,
        "reminderLabel": label,
        "deviceSerialNumber": device_serial,
        "deviceType": device_type,
    }


def build_alarm(device_serial: str, device_type: str,
                at_epoch_ms: int, label: str = "") -> dict[str, Any]:
    """Build an Alarm creation payload (pure)."""
    return {
        "type": "Alarm",
        "status": "ON",
        "alarmTime": int(at_epoch_ms),
        "originalTime": None,
        "originalLabel": label or None,
        "deviceSerialNumber": device_serial,
        "deviceType": device_type,
    }


def build_timer(device_serial: str, device_type: str,
                duration_ms: int, label: str = "") -> dict[str, Any]:
    """Build a Timer creation payload (pure)."""
    return {
        "type": "Timer",
        "status": "ON",
        "remainingTime": int(duration_ms),
        "originalDurationInMillis": int(duration_ms),
        "timerLabel": label or None,
        "deviceSerialNumber": device_serial,
        "deviceType": device_type,
    }


async def list_notifications(login) -> list[dict[str, Any]]:
    from alexapy import AlexaAPI

    data = await AlexaAPI.get_notifications(login)
    return notification_rows(list(data or []))


async def create_notification(login, payload: dict[str, Any]) -> dict[str, Any]:
    """POST a new alarm/timer/reminder to /api/notifications."""
    headers = csrf_header(login)
    if not headers:
        raise AlexaSessionError("no csrf cookie — cannot create a notification")
    url = f"{base_url(login.url)}/api/notifications"
    async with login.session.put(url, json=payload, headers=headers) as resp:
        text = await resp.text()
        return {"status": resp.status, "ok": resp.status in (200, 201), "body": text[:300]}


async def delete_notification(login, notification_id: str) -> dict[str, Any]:
    """DELETE /api/notifications/<id>."""
    headers = csrf_header(login)
    if not headers:
        raise AlexaSessionError("no csrf cookie — cannot delete a notification")
    url = f"{base_url(login.url)}/api/notifications/{quote(notification_id, safe='')}"
    async with login.session.delete(url, headers=headers) as resp:
        text = await resp.text()
        return {
            "id": notification_id,
            "status": resp.status,
            "deleted": resp.status in (200, 204),
            "body": text[:200],
        }
