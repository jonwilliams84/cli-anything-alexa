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
* **Selective delete, not just bulk.**  ``clear_history`` reaches only the N
  most recent records as a block; the underlying endpoint is a **per-id**
  ``DELETE /api/activities/<id>`` — the same id the legacy feed carries — so
  exactly the recordings meant can be named: only one Echo's, only one
  utterance's.  :func:`plan_clear` selects them from fetched rows (reusing
  :func:`filter_rows`), :func:`delete_activities` deletes **one id per
  request** (mirroring alexapy's own per-id loop) and
  :func:`selective_clear_summary` reports the result **three-valued** —
  ``True`` deleted, ``False`` (404: no recording behind that id), ``None``
  (Amazon did not answer) — never a quiet pass.
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

#: Records fetched to select from when clearing by filter.  The legacy feed
#: takes its size in one request and the selection is client-side, so this
#: fetch IS the deletion pool — kept visibly wide and widened with ``--limit``.
DEFAULT_CLEAR_LIMIT = 100


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
    """Validate a record count (pure). Must be a positive whole number.

    A non-integral *float* is refused rather than truncated: ``int(1.5)`` would
    silently ask Amazon for 1 record while the string ``"1.5"`` already raises,
    and a limit that quietly means something else than it says is worse than an
    error the caller can read.
    """
    if value is None or value == "":
        return default
    if isinstance(value, float) and value != int(value):
        raise ValueError(f"limit must be a whole number, got {value!r}")
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

    The payload itself is either the bare list or the ``{"activities": [...]}``
    envelope; anything else (alexapy hands back ``None`` on a failed request,
    and an error body can arrive as a plain string) yields no rows instead of
    raising.
    """
    names = _serial_to_name(devices)
    if isinstance(payload, list):
        items: Any = payload
    elif isinstance(payload, dict):
        items = payload.get("activities") or []
    else:
        items = []
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


# ── selective clear (per-id deletion) ───────────────────────────────────


def parse_ids(raw: str) -> list[str]:
    """Comma/whitespace separated activity ids → cleaned list (pure).

    Duplicates are dropped order-preserving.  An empty result raises rather
    than silently selecting nothing, mirroring :func:`normalize_limit`.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("no ids given — pass --ids <id[,id...]>")
    ids: list[str] = []
    for token in raw.replace(",", " ").split():
        token = token.strip()
        if token and token not in ids:
            ids.append(token)
    if not ids:
        raise ValueError("no ids given — pass --ids <id[,id...]>")
    return ids


def id_preview_rows(ids: list[str]) -> list[dict[str, Any]]:
    """Preview rows for explicitly-supplied ids (pure).

    Only the ids are known — the CLI never fetches for an ``--ids`` clear —
    so every other cell stays ``None`` instead of being guessed.
    """
    return [
        {"id": aid, "time": None, "device": None, "utterance": None, "status": None}
        for aid in ids or []
    ]


def plan_clear(
    rows: list[dict[str, Any]] | None,
    device: str | None = None,
    contains: str | None = None,
    ids: list[str] | None = None,
) -> dict[str, Any]:
    """Select the records a selective ``activity clear`` will delete (pure).

    ``ids`` (explicit activity ids) short-circuits the fetch/filters.  With
    ``device``/``contains`` the fetched legacy rows are filtered client-side
    with :func:`filter_rows` — the same predicates ``activity history`` uses —
    and only rows that carry an id can be selected.

    Returns ``{"ids": [...], "rows": [...]}``` — the plan.  An empty selection
    is a VALID answer ("nothing matched"), the CLI reports it rather than
    treating it as an error; the caller aborts, never the planner.
    """
    if ids is not None:
        picked = list(ids)
        return {"ids": picked, "rows": id_preview_rows(picked)}
    selected = [r for r in filter_rows(rows or [], device=device, contains=contains) if r.get("id")]
    return {"ids": [r["id"] for r in selected], "rows": selected}


def delete_envelope(response: Any) -> bool | None:
    """One ``DELETE /api/activities/<id>`` answer → three-valued ``deleted`` (pure).

    ``True`` (200 — the recording is gone), ``False`` (404 — Amazon says there
    is nothing to delete, the case alexapy logs), ``None`` (no answer, or any
    other status — "unconfirmed", never a quiet pass).
    """
    if response is None:
        return None
    status = getattr(response, "status", None)
    if status == 200:
        return True
    if status == 404:
        return False
    return None


def selective_clear_summary(entries: list[dict[str, Any]], requested: int) -> dict[str, Any]:
    """Summarise a selective clear (pure) — never a quiet pass.

    ``cleared`` is True only when EVERY id reported deleted.  A refused id
    (404) and an unanswered id (``None``) are each their own list with their
    own remedy, because they mean different things: one is gone from Amazon's
    ledger already, the other might still exist somewhere between us.
    """
    deleted = [e.get("id") for e in entries or [] if e.get("deleted") is True]
    refused = [e for e in entries or [] if e.get("deleted") is False]
    unconfirmed = [e.get("id") for e in entries or [] if e.get("deleted") is None]
    row: dict[str, Any] = {"requested": len(entries or []) or requested, "deleted": len(deleted)}
    row["cleared"] = not refused and not unconfirmed
    notes: list[str] = []
    if refused:
        row["refused"] = [{"id": e.get("id"), "status": e.get("status")} for e in refused]
        notes.append(
            "Amazon refused at least one id (no recording behind it); "
            "remove those manually in the Alexa app"
        )
    if unconfirmed:
        row["unconfirmed"] = unconfirmed
        notes.append(
            "Amazon did not answer every delete; those may still exist — "
            "re-check with `activity records`"
        )
    if notes:
        row["hint"] = "; ".join(notes)
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


async def activity_records(
    login,
    limit: Any = DEFAULT_HISTORY_LIMIT,
    device: str | None = None,
    contains: str | None = None,
) -> list[dict[str, Any]]:
    """The legacy ``/api/activities`` feed, with ids (network).

    ``device``/``contains`` apply the same client-side filters as
    ``activity history`` — the natural way to pick out the ids a selective
    ``activity clear`` will then delete.
    """
    from alexapy import AlexaAPI

    from cli_anything.alexa.core.devices_meta import fetch_devices

    count = normalize_limit(limit)
    payload = await AlexaAPI.get_activities(login, items=count)
    devices = await fetch_devices(login)
    return filter_rows(activity_rows(payload, devices), device=device, contains=contains)


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


async def delete_activities(login, ids: list[str]) -> list[dict[str, Any]]:
    """``DELETE /api/activities/<id>`` once per id (network).

    Mirrors alexapy's own per-id delete loop (including the URL-quoted id) so
    a selective clear behaves identically to the bulk one at the wire — the
    per-id bookkeeping is ours.
    """
    from urllib.parse import quote_plus

    from alexapy import AlexaAPI

    entries: list[dict[str, Any]] = []
    for aid in ids:
        response = await AlexaAPI._static_request(
            "delete", login, f"/api/activities/{quote_plus(aid)}"
        )
        entries.append(
            {
                "id": aid,
                "deleted": delete_envelope(response),
                "status": getattr(response, "status", None) if response is not None else None,
            }
        )
    return entries


async def apply_clear(login, plan: dict[str, Any]) -> dict[str, Any]:
    """Execute a planned selective clear (network) — the plan's ids, no others.

    The plan is built once (in the CLI, from the same fetched rows the preview
    showed), so the thing reviewed is the thing deleted.
    """
    ids = plan.get("ids") or []
    return selective_clear_summary(await delete_activities(login, ids), requested=len(ids) or 0)
