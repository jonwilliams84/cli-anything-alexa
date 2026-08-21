"""Alarms / timers / reminders (the `/api/notifications` surface).

`get_notifications` is wrapped by alexapy; alexapy only exposes the PUT
(edit) for writes, so create (POST) and delete (DELETE) are issued as raw
authed-session calls with the `csrf` header — matching the proven endpoint
notes. The payload builders here are kept pure where practical.

Three things are worth knowing before extending the **edit** half of this
module (``pause``/``resume``/``reschedule``/``snooze``):

* **An edit is a whole-record PUT, not a patch.**  ``/api/notifications``
  replaces the notification with the body it is given, so every builder here
  starts from a *copy of the record Amazon returned* and changes one or two
  fields.  Hand-rolling a minimal body drops fields (recurrence, the owning
  device) and Amazon accepts it silently — the alarm just loses them.
* **A reminder fires off its LOCAL wall-clock fields, not only ``alarmTime``.**
  A reminder/alarm record carries ``alarmTime`` (epoch ms) *and*
  ``originalDate``/``originalTime`` — the date and time-of-day, in the owning
  Echo's timezone, that the app displays and that the schedule is rebuilt
  from.  Moving ``alarmTime`` alone leaves those stale, so
  :func:`build_reschedule` recomputes them **whenever the record has them**,
  using the device's own ``timeZoneId`` from ``/api/device-preferences``
  (:func:`~cli_anything.alexa.core.devices_meta.fetch_device_preferences`).
  With no timezone available it falls back to UTC and says so in ``tz`` rather
  than silently writing the wrong local time.
* **The PUT reports almost nothing usable, so the writes VERIFY.**
  ``AlexaAPI.set_notifications`` is wrapped in alexapy's
  ``_catch_all_exceptions``, which turns a failed request into a quiet
  ``None`` — indistinguishable from an accepted edit that returned an empty
  body.  :func:`apply_update` therefore re-reads the notification list and
  sets ``ok`` from what Amazon actually holds, the same rule ``kids
  enable``/``devices delete --verify`` follow.  ``ok`` is ``None`` (not
  ``False``) when the verify read itself could not see the record — Amazon
  throttles ``/api/notifications`` and answers ``None`` — because "could not
  check" is not "did not work".

Timers are deliberately excluded from reschedule/snooze: a timer counts down
via ``remainingTime`` from the moment it was set and has no ``alarmTime`` to
move, so the honest answer is delete-and-recreate.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any

from cli_anything.alexa.core.session import (
    AlexaSessionError,
    base_url,
    csrf_header,
)

#: Amazon's notification ``status`` vocabulary.  ``OFF`` is a *paused*
#: notification: it stays in the list (and keeps its schedule) but will not
#: fire, which is what ``notifications pause`` means.
STATUS_ON = "ON"
STATUS_OFF = "OFF"

#: Notification types that own an ``alarmTime`` and can therefore be moved in
#: time.  ``Timer`` is absent on purpose — see the module docstring.
SCHEDULABLE_TYPES: tuple[str, ...] = ("Alarm", "Reminder", "MusicAlarm")

#: Amazon's default alarm snooze, in minutes — the same nine minutes the
#: "snooze" voice command applies, so the CLI default matches the speaker.
DEFAULT_SNOOZE_MINUTES = 9


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


def _epoch_ms(seconds_from_now: float | None = None, at_epoch_ms: int | None = None) -> int:
    if at_epoch_ms is not None:
        return int(at_epoch_ms)
    return int((time.time() + (seconds_from_now or 0)) * 1000)


def build_reminder(
    label: str, device_serial: str, device_type: str, at_epoch_ms: int
) -> dict[str, Any]:
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


def build_alarm(
    device_serial: str, device_type: str, at_epoch_ms: int, label: str = ""
) -> dict[str, Any]:
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


def build_timer(
    device_serial: str, device_type: str, duration_ms: int, label: str = ""
) -> dict[str, Any]:
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


# ── edit helpers (pure) ──────────────────────────────────────────────────


def notification_id(record: Any) -> str | None:
    """The id Amazon addresses a notification by (pure).

    ``notificationIndex`` is the field the delete endpoint takes; ``id`` is what
    older payloads carry.  Both are accepted so a record from either shape can
    be matched and re-PUT.
    """
    if not isinstance(record, dict):
        return None
    value = record.get("notificationIndex") or record.get("id")
    return str(value) if value else None


def notification_label(record: Any) -> str | None:
    """A notification's human label, whatever type it is (pure)."""
    if not isinstance(record, dict):
        return None
    return record.get("reminderLabel") or record.get("originalLabel") or record.get("timerLabel")


def normalize_status(value: Any) -> str:
    """Normalise a status word to Amazon's ``ON``/``OFF`` (pure).

    Accepts the CLI's on/off vocabulary plus the synonyms a user reaches for
    (``pause``/``resume``/``enabled``/``disabled``) so a paused alarm can be
    described either way.
    """
    word = str(value or "").strip().lower()
    if word in ("on", "resume", "resumed", "enable", "enabled", "active"):
        return STATUS_ON
    if word in ("off", "pause", "paused", "disable", "disabled", "inactive"):
        return STATUS_OFF
    raise ValueError(f"status must be on or off, got {value!r}")


def _normalize_label(value: Any) -> str:
    """Case/whitespace-insensitive form of a label (pure)."""
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip().lower()


def find_notifications(items: Any, target: str) -> list[dict[str, Any]]:
    """Resolve ``target`` to notification records (pure).

    Precedence (first tier that matches wins): exact id → exact label →
    normalized label.  Returns every match in the winning tier: two alarms can
    honestly share the label "Wake up", and the caller must then disambiguate
    by id rather than the harness guessing.
    """
    records = [n for n in (items or []) if isinstance(n, dict)]
    wanted = (target or "").strip()
    if not wanted:
        return []
    hits = [n for n in records if notification_id(n) == wanted]
    if hits:
        return hits
    hits = [n for n in records if notification_label(n) == wanted]
    if hits:
        return hits
    normalized = _normalize_label(wanted)
    return [n for n in records if _normalize_label(notification_label(n)) == normalized]


def notification_choices(items: Any) -> list[str]:
    """``id (label)`` descriptions for an error message (pure)."""
    out: list[str] = []
    for n in items or []:
        if not isinstance(n, dict):
            continue
        nid = notification_id(n)
        if not nid:
            continue
        label = notification_label(n)
        out.append(f"{nid} ({label})" if label else nid)
    return out


def resolve_notification(items: Any, target: str) -> dict[str, Any]:
    """Resolve ``target`` to exactly ONE notification record (pure).

    Raises ``ValueError`` — surfaced verbatim by the CLI — when nothing or more
    than one thing matches, listing the alternatives either way.  Editing the
    wrong alarm is silent and only noticed at 3am, so an ambiguous label is
    always refused rather than resolved by "first match".
    """
    matches = find_notifications(items, target)
    if not matches:
        known = notification_choices(items)
        if not known:
            raise ValueError("no alarms, timers or reminders exist on this account")
        raise ValueError(f"no notification matching {target!r}; known: {', '.join(known)}")
    if len(matches) > 1:
        ids = ", ".join(str(notification_id(m)) for m in matches)
        raise ValueError(f"{target!r} matches {len(matches)} notifications; pick one by id: {ids}")
    return matches[0]


def is_schedulable(record: Any) -> bool:
    """True when a record has an ``alarmTime`` that can be moved (pure)."""
    return isinstance(record, dict) and record.get("type") in SCHEDULABLE_TYPES


def local_fields(at_epoch_ms: int, tz_id: str | None = None) -> dict[str, Any]:
    """Amazon's local wall-clock fields for an instant (pure).

    Returns ``originalDate`` (``YYYY-MM-DD``), ``originalTime``
    (``HH:MM:SS.000``) and the ``tz`` actually used — ``UTC`` when ``tz_id`` is
    missing or unknown to the host, so the caller can say which clock the
    answer is in instead of pretending it knew.
    """
    tzinfo = timezone.utc
    used = "UTC"
    if tz_id:
        try:
            from zoneinfo import ZoneInfo

            tzinfo = ZoneInfo(str(tz_id))
            used = str(tz_id)
        except Exception:  # noqa: BLE001 - an unknown/invalid tz must not break an edit
            tzinfo = timezone.utc
            used = "UTC"
    moment = datetime.fromtimestamp(int(at_epoch_ms) / 1000, tz=tzinfo)
    return {
        "originalDate": moment.strftime("%Y-%m-%d"),
        "originalTime": moment.strftime("%H:%M:%S.000"),
        "tz": used,
    }


def build_status_update(record: dict[str, Any], status: Any) -> dict[str, Any]:
    """Whole-record PUT body that pauses/resumes a notification (pure)."""
    if not isinstance(record, dict) or not record:
        raise ValueError("cannot update an empty notification record")
    payload = dict(record)
    payload["status"] = normalize_status(status)
    return payload


def build_reschedule(
    record: dict[str, Any], at_epoch_ms: int, tz_id: str | None = None
) -> dict[str, Any]:
    """Whole-record PUT body that moves a notification to ``at_epoch_ms`` (pure).

    ``originalDate``/``originalTime`` are rewritten only when the record
    already carries them (see the module docstring): adding them to a record
    that had none would invent a schedule shape Amazon did not send.
    """
    if not isinstance(record, dict) or not record:
        raise ValueError("cannot reschedule an empty notification record")
    if not is_schedulable(record):
        kind = record.get("type") or "unknown"
        raise ValueError(
            f"a {kind} has no alarmTime to move; delete it and create a new one instead"
        )
    when = int(at_epoch_ms)
    if when <= 0:
        raise ValueError(f"the new time must be a positive epoch-ms value, got {at_epoch_ms!r}")
    payload = dict(record)
    payload["alarmTime"] = when
    local = local_fields(when, tz_id)
    for field in ("originalDate", "originalTime"):
        if field in payload:
            payload[field] = local[field]
    return payload


def snooze_epoch_ms(record: dict[str, Any], minutes: float, now_ms: int | None = None) -> int:
    """Where a snooze moves a notification to (pure).

    Snoozing measures from the alarm's own time when that is still in the
    future (nudging tomorrow's 7am alarm to 7:09am) and from *now* when it has
    already fired — which is the case that matters, because that is when a
    human is reaching for snooze.
    """
    try:
        span = float(minutes)
    except (TypeError, ValueError):
        raise ValueError(f"snooze minutes must be a number, got {minutes!r}") from None
    if span != span or span in (float("inf"), float("-inf")):  # NaN / inf
        raise ValueError(f"snooze minutes must be a number, got {minutes!r}")
    if span <= 0:
        raise ValueError(f"snooze minutes must be greater than 0, got {minutes!r}")
    now = int(now_ms) if now_ms is not None else int(time.time() * 1000)
    try:
        base = int((record or {}).get("alarmTime") or 0)
    except (TypeError, ValueError):
        base = 0
    start = base if base > now else now
    return start + round(span * 60_000)


def build_snooze(
    record: dict[str, Any],
    minutes: float = DEFAULT_SNOOZE_MINUTES,
    tz_id: str | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Whole-record PUT body that snoozes a notification (pure)."""
    if not is_schedulable(record):
        kind = (record or {}).get("type") or "unknown"
        raise ValueError(f"a {kind} cannot be snoozed; cancel it and set a new one instead")
    return build_reschedule(record, snooze_epoch_ms(record, minutes, now_ms), tz_id)


def change_summary(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """The fields an edit actually changes, as ``{field: {from, to}}`` (pure).

    This is what the dry-run prints: the *diff*, not the whole 30-key record,
    so the safety review before ``--yes`` is readable.
    """
    out: dict[str, Any] = {}
    for key in sorted(set(before or {}) | set(after or {})):
        old = (before or {}).get(key)
        new = (after or {}).get(key)
        if old != new:
            out[key] = {"from": old, "to": new}
    return out


def render_epoch_ms(value: Any) -> str | None:
    """Epoch ms → an ISO-8601 UTC string for display (pure).

    Timestamps are rendered tz-aware (never a naive ``fromtimestamp``, which
    would silently re-read them in the host's zone).
    """
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def verify_status(record: Any, expected: dict[str, Any]) -> bool | None:
    """Did the re-read record land on ``expected``? (pure)

    ``None`` means the record could not be re-read at all — Amazon throttles
    ``/api/notifications`` and answers ``None`` — which is deliberately not
    reported as failure.
    """
    if not isinstance(record, dict):
        return None
    for key, want in (expected or {}).items():
        if record.get(key) != want:
            return False
    return True


async def list_notifications(login) -> list[dict[str, Any]]:
    from alexapy import AlexaAPI

    data = await AlexaAPI.get_notifications(login)
    return notification_rows(list(data or []))


async def fetch_notifications(login) -> list[dict[str, Any]]:
    """The RAW notification records (not the display rows).

    An edit is a whole-record PUT, so the untouched record Amazon returned is
    the only safe starting point — hence this exists alongside
    :func:`list_notifications`.
    """
    from alexapy import AlexaAPI

    data = await AlexaAPI.get_notifications(login)
    return [n for n in (data or []) if isinstance(n, dict)]


async def _timezone_for(login, record: dict[str, Any]) -> str | None:
    """The owning Echo's ``timeZoneId``, or ``None`` if it cannot be read.

    A preferences read failing must never block an edit — the reschedule then
    falls back to UTC and reports ``tz: UTC``.
    """
    serial = record.get("deviceSerialNumber")
    if not serial:
        return None
    from cli_anything.alexa.core.devices_meta import device_timezone, fetch_device_preferences

    try:
        rows = await fetch_device_preferences(login)
    except Exception:  # noqa: BLE001 - preferences are an enrichment, not a gate
        return None
    return device_timezone(rows, serial)


async def show_notification(login, target: str) -> dict[str, Any]:
    """One notification's row plus its raw record (the edit's starting point)."""
    records = await fetch_notifications(login)
    record = resolve_notification(records, target)
    row = notification_rows([record])[0]
    row["alarmTimeUtc"] = render_epoch_ms(record.get("alarmTime"))
    row["raw"] = record
    return row


async def plan_update(
    login,
    target: str,
    *,
    status: Any = None,
    at_epoch_ms: int | None = None,
    snooze_minutes: float | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Resolve ``target`` and build the PUT body for one edit (no write).

    Exactly one of ``status`` / ``at_epoch_ms`` / ``snooze_minutes`` is applied;
    the result carries both the payload and a readable ``change`` diff so the
    CLI can print the same plan for the dry-run and then execute it verbatim.
    Resolution and validation happen HERE, before any write, so a bad target or
    a timer-reschedule fails identically with and without ``--yes``.
    """
    records = await fetch_notifications(login)
    record = resolve_notification(records, target)
    tz_id: str | None = None
    if status is not None:
        payload = build_status_update(record, status)
    elif at_epoch_ms is not None:
        tz_id = await _timezone_for(login, record)
        payload = build_reschedule(record, at_epoch_ms, tz_id)
    elif snooze_minutes is not None:
        tz_id = await _timezone_for(login, record)
        payload = build_snooze(record, snooze_minutes, tz_id, now_ms)
    else:
        raise ValueError("nothing to change: pass a status, a new time, or a snooze")
    return {
        "id": notification_id(record),
        "type": record.get("type"),
        "label": notification_label(record),
        "deviceSerial": record.get("deviceSerialNumber"),
        "tz": tz_id or "UTC",
        "alarmTimeUtc": render_epoch_ms(payload.get("alarmTime")),
        "change": change_summary(record, payload),
        "payload": payload,
        "before": record,
    }


async def apply_update(login, plan: dict[str, Any]) -> dict[str, Any]:
    """PUT a planned edit, then RE-READ to report what Amazon actually holds.

    ``ok`` is ``True``/``False`` from the verify read and ``None`` when the
    record could not be re-read at all — see the module docstring.
    """
    from alexapy import AlexaAPI

    payload = (plan or {}).get("payload")
    if not isinstance(payload, dict) or not payload:
        raise ValueError("no edit payload to apply")
    nid = plan.get("id") or notification_id(payload)
    response = await AlexaAPI.set_notifications(login, payload)
    expected = {k: v["to"] for k, v in (plan.get("change") or {}).items()}
    verified = None
    try:
        after = await fetch_notifications(login)
    except Exception:  # noqa: BLE001 - a throttled verify is "unknown", not failure
        after = []
    current = next((n for n in after if notification_id(n) == nid), None)
    verified = verify_status(current, expected)
    return {
        "id": nid,
        "type": plan.get("type"),
        "label": plan.get("label"),
        "change": plan.get("change"),
        "alarmTimeUtc": render_epoch_ms((current or payload).get("alarmTime")),
        "status": (current or payload).get("status"),
        "ok": verified,
        "note": None if verified is not None else "could not re-read the notification to verify",
        "response": response,
    }


async def set_status(login, target: str, status: Any) -> dict[str, Any]:
    """Pause/resume a notification in one call (plan + apply)."""
    return await apply_update(login, await plan_update(login, target, status=status))


async def reschedule(login, target: str, at_epoch_ms: int) -> dict[str, Any]:
    """Move a notification to an absolute epoch-ms time (plan + apply)."""
    return await apply_update(login, await plan_update(login, target, at_epoch_ms=at_epoch_ms))


async def snooze(login, target: str, minutes: float = DEFAULT_SNOOZE_MINUTES) -> dict[str, Any]:
    """Push a notification further out by ``minutes`` (plan + apply)."""
    return await apply_update(login, await plan_update(login, target, snooze_minutes=minutes))


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
    url = f"{base_url(login.url)}/api/notifications/{notification_id}"
    async with login.session.delete(url, headers=headers) as resp:
        text = await resp.text()
        return {
            "id": notification_id,
            "status": resp.status,
            "deleted": resp.status in (200, 204),
            "body": text[:200],
        }
