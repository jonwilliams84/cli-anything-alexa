"""Voice **activity history**: what was said to Alexa, and what she said back.

The harness could already make devices do things but had no way to see what
they had *done* — the read that closes every other loop.  Three Amazon
surfaces, all static (no device binding):

* ``/alexa-privacy/apd/rvh/customer-history-records``
  (``AlexaAPI.get_customer_history_records``) — the modern privacy view.  It is
  the only one that returns the **transcript** of both halves of a turn
  ("turn off the kitchen lights" → "OK"), so it backs ``activity history``.
* ``/api/activities`` (``AlexaAPI.get_activities``) — the legacy feed.  Kept as
  ``activity records`` because it still carries ids and per-activity status
  that the privacy view drops, and because it is the id source for deletion.
* ``DELETE /api/activities/<id>`` (``AlexaAPI.clear_history``) — bulk delete of
  recent recordings, the one destructive call here.

Four things are worth knowing before extending this module:

* **The two feeds have different shapes on purpose.**  The privacy records are
  already flattened by alexapy into ``{description:{summary}, alexaResponse,
  deviceSerialNumber, creationTimestamp, utteranceType}``; the legacy feed
  nests a **JSON-encoded string** in ``description`` and the serial under
  ``sourceDeviceIds[].serialNumber``.  :func:`history_rows` and
  :func:`activity_rows` normalise both to the same row so the CLI renders one
  table either way, and both tolerate junk (a row with no transcript survives
  as ``None``, never a traceback).
* **Timestamps are epoch milliseconds** and are rendered as timezone-aware UTC
  ISO strings (:func:`format_timestamp`); a naive ``fromtimestamp`` would
  silently re-interpret them in whatever zone the machine happens to be in.
* **The window is a query parameter, not a filter.**  The privacy endpoint
  takes ``startTime``/``endTime``; :func:`history_window` computes them from a
  simple ``--hours`` so the value is pure and testable with an injected ``now``.
* **``clear_history`` deletes real recordings.**  It is irreversible and
  Amazon refuses some entries with a 404 (nothing to delete) — alexapy returns
  ``False`` when that happened, which :func:`clear_summary` reports rather than
  swallowing, so a partial clear is never announced as a clean one.

Everything above the "live operations" divider is pure and unit-tested; only
the thin ``async def`` wrappers touch the network.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

#: Default number of hours of history to ask for.
DEFAULT_HISTORY_HOURS = 24

#: Default number of records to request/render.
DEFAULT_HISTORY_LIMIT = 20

#: Utterance types that are device housekeeping rather than a user turn — the
#: same one alexapy's ``get_last_device_serial`` skips.
NOISE_UTTERANCE_TYPES = frozenset({"DEVICE_ARBITRATION"})


# ── pure helpers ─────────────────────────────────────────────────────────


def format_timestamp(value: Any) -> str | None:
    """Epoch **milliseconds** → timezone-aware UTC ISO-8601 string (pure)."""
    try:
        millis = float(value)
    except (TypeError, ValueError):
        return None
    if millis != millis or millis in (float("inf"), float("-inf")):  # NaN / inf
        return None
    try:
        return datetime.fromtimestamp(millis / 1000.0, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):  # absurd epoch values
        return None


def history_window(
    hours: float | int | str | None = DEFAULT_HISTORY_HOURS,
    now: datetime | None = None,
) -> tuple[int, int]:
    """``(startTime, endTime)`` epoch-ms for the privacy query (pure).

    ``endTime`` is *now*, not now+24h as alexapy defaults to: asking for the
    future returns nothing extra and makes the window meaningless in output.
    """
    try:
        span = DEFAULT_HISTORY_HOURS if hours is None or hours == "" else float(hours)
    except (TypeError, ValueError):
        raise ValueError(f"hours must be a number, got {hours!r}") from None
    if span != span or span in (float("inf"), float("-inf")):  # NaN / inf
        raise ValueError(f"hours must be a number, got {hours!r}")
    if span <= 0:
        raise ValueError(f"hours must be greater than 0, got {hours!r}")
    end = now or datetime.now(tz=timezone.utc)
    if end.tzinfo is None:  # a caller-supplied naive datetime is assumed UTC
        end = end.replace(tzinfo=timezone.utc)
    start = end - timedelta(hours=span)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def normalize_limit(value: Any, default: int = DEFAULT_HISTORY_LIMIT) -> int:
    """Validate a record count (pure). Must be a positive whole number."""
    if value is None or value == "":
        return default
    try:
        count = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"limit must be a whole number, got {value!r}") from None
    if count < 1:
        raise ValueError(f"limit must be at least 1, got {value!r}")
    return count


def _serial_to_name(devices: list[dict[str, Any]] | None) -> dict[str, str]:
    """serialNumber → accountName lookup (pure)."""
    return {
        d.get("serialNumber"): d.get("accountName")
        for d in devices or []
        if isinstance(d, dict) and d.get("serialNumber")
    }


def _clean(text: Any) -> str | None:
    """Trim a transcript field; empty/absent becomes ``None`` (pure)."""
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    return stripped or None


def history_rows(
    records: Any,
    devices: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Flatten privacy history records into display rows (pure).

    alexapy hands back a list of already-simplified dicts (or ``None`` when the
    request failed).  Anything that is not a dict is skipped rather than
    raising, because one malformed record must not lose the other 19.
    """
    names = _serial_to_name(devices)
    rows: list[dict[str, Any]] = []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        serial = record.get("deviceSerialNumber")
        description = record.get("description")
        summary = None
        if isinstance(description, dict):
            summary = _clean(description.get("summary"))
        elif isinstance(description, str):
            summary = _clean(description)
        rows.append(
            {
                "time": format_timestamp(record.get("creationTimestamp")),
                "device": names.get(serial) or serial,
                "utterance": summary,
                "response": _clean(record.get("alexaResponse")),
                "type": record.get("utteranceType"),
            }
        )
    return rows


def activity_rows(
    payload: Any,
    devices: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Flatten the legacy ``/api/activities`` feed into display rows (pure).

    ``description`` is a JSON *string* here; an undecodable one degrades to the
    raw text instead of dropping the row.
    """
    names = _serial_to_name(devices)
    items = payload if isinstance(payload, list) else (payload or {}).get("activities") or []
    rows: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        summary = None
        description = item.get("description")
        if isinstance(description, str):
            try:
                decoded = json.loads(description)
            except (ValueError, TypeError):
                summary = _clean(description)
            else:
                summary = (
                    _clean(decoded.get("summary"))
                    if isinstance(decoded, dict)
                    else _clean(description)
                )
        elif isinstance(description, dict):
            summary = _clean(description.get("summary"))
        sources = item.get("sourceDeviceIds") or []
        serial = None
        if isinstance(sources, list):
            for source in sources:
                if isinstance(source, dict) and source.get("serialNumber"):
                    serial = source["serialNumber"]
                    break
        rows.append(
            {
                "time": format_timestamp(item.get("creationTimestamp")),
                "device": names.get(serial) or serial,
                "utterance": summary,
                "status": item.get("activityStatus"),
                "id": item.get("id"),
            }
        )
    return rows


def filter_rows(
    rows: list[dict[str, Any]],
    device: str | None = None,
    contains: str | None = None,
    include_noise: bool = False,
) -> list[dict[str, Any]]:
    """Client-side row filter (pure) — neither endpoint can filter server-side.

    ``include_noise=False`` drops the wake-word arbitration rows Amazon records
    when several Echos hear the same "Alexa"; they are never what a user means
    by "what did I ask".
    """
    device_key = (device or "").strip().lower()
    needle = (contains or "").strip().lower()
    out: list[dict[str, Any]] = []
    for row in rows or []:
        if not include_noise and row.get("type") in NOISE_UTTERANCE_TYPES:
            continue
        if device_key and device_key not in str(row.get("device") or "").lower():
            continue
        if needle:
            haystack = f"{row.get('utterance') or ''} {row.get('response') or ''}".lower()
            if needle not in haystack:
                continue
        out.append(row)
    return out


def last_command_row(payload: Any, devices: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Flatten ``get_last_device_serial`` into a single row (pure).

    ``None`` (no qualifying turn in the searched window) is reported as an
    empty row rather than an error: "nothing was said recently" is a valid
    answer, not a failure.
    """
    names = _serial_to_name(devices)
    record = payload if isinstance(payload, dict) else {}
    serial = record.get("serialNumber")
    return {
        "time": format_timestamp(record.get("timestamp")),
        "device": names.get(serial) or serial,
        "serial": serial,
        "utterance": _clean(record.get("summary")),
    }


def clear_summary(result: Any, requested: int) -> dict[str, Any]:
    """Report a ``clear_history`` outcome (pure).

    alexapy returns ``False`` when Amazon refused at least one entry (a 404 —
    "there is no voice recording to delete"), so a partial clear is reported as
    partial, with the app-side remedy.
    """
    complete = bool(result)
    row: dict[str, Any] = {"requested": requested, "cleared": complete}
    if not complete:
        row["hint"] = (
            "Amazon refused at least one entry (no recording to delete); "
            "remove those manually in the Alexa app"
        )
    return row


# ── live operations ──────────────────────────────────────────────────────


async def fetch_history(
    login,
    limit: int = DEFAULT_HISTORY_LIMIT,
    hours: float | int | str | None = DEFAULT_HISTORY_HOURS,
    now: datetime | None = None,
) -> Any:
    """Raw privacy history records for a window (network)."""
    from alexapy import AlexaAPI

    start, end = history_window(hours, now=now)
    return await AlexaAPI.get_customer_history_records(
        login, start_time=start, end_time=end, max_record_size=limit
    )


async def voice_history(
    login,
    limit: Any = DEFAULT_HISTORY_LIMIT,
    hours: Any = DEFAULT_HISTORY_HOURS,
    device: str | None = None,
    contains: str | None = None,
    include_noise: bool = False,
) -> list[dict[str, Any]]:
    """What was said to Alexa (and her replies) in the last ``hours``."""
    from cli_anything.alexa.core.devices_meta import fetch_devices

    count = normalize_limit(limit)
    history_window(hours)  # validate before spending a request
    records = await fetch_history(login, limit=count, hours=hours)
    devices = await fetch_devices(login)
    rows = history_rows(records, devices)
    return filter_rows(rows, device=device, contains=contains, include_noise=include_noise)


async def activity_records(login, limit: Any = DEFAULT_HISTORY_LIMIT) -> list[dict[str, Any]]:
    """The legacy ``/api/activities`` feed, with ids (network)."""
    from alexapy import AlexaAPI

    from cli_anything.alexa.core.devices_meta import fetch_devices

    count = normalize_limit(limit)
    payload = await AlexaAPI.get_activities(login, items=count)
    devices = await fetch_devices(login)
    return activity_rows(payload, devices)


async def last_command(login, limit: Any = DEFAULT_HISTORY_LIMIT) -> dict[str, Any]:
    """The most recent Echo that answered, and what it was asked."""
    from alexapy import AlexaAPI

    from cli_anything.alexa.core.devices_meta import fetch_devices

    count = normalize_limit(limit)
    payload = await AlexaAPI.get_last_device_serial(login, items=count)
    devices = await fetch_devices(login)
    return last_command_row(payload, devices)


async def clear_history(login, items: Any = 50) -> dict[str, Any]:
    """Delete recent voice recordings (irreversible)."""
    from alexapy import AlexaAPI

    count = normalize_limit(items, default=50)
    result = await AlexaAPI.clear_history(login, items=count)
    return clear_summary(result, count)
