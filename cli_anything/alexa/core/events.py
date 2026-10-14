"""Alexa **push events** — the real-time WebSocket event stream.

The one alexapy surface the harness never touched: after a login, Amazon
keeps a WebSocket open on ``wss://dp-gw-na.<domain>/tcomm/`` (``dp-gw-na-js``
on ``amazon.com``) and PUSHES device events out to the app as they happen —
state changes, music playback turns, routine/activity notices, connection
handshakes. Every other surface in this harness is **pull** (the CLI asks);
this is the only **push** one, which is exactly what makes it the only way to
watch the smart home live without a real Alexa device speaking.

alexapy ships the whole client: ``alexapy.alexawebsocket.WebsocketEchoClient``
(the TWIG binary protocol port from openHAB's Echo-control binding). This
module is a thin, honest wrapper around it — the binary protocol itself is
NOT re-implemented here:

* The client needs the account's device-signing key (``login.mac_dms``,
  the key Amazon hands to the companion-app session at login). alexapy
  raises on it when absent; the wrapper pre-checks (:func:`mac_dms_ready`)
  and refuses with the fix ("re-authenticate") instead of a bare
  ``AlexapyLoginError`` traceback.
* Events arrive as alexapy ``Message`` objects (binary TWIG frames it has
  already decoded); :func:`event_row` flattens one into a display row —
  type-safe (a half-decoded message or junk yields ``None``, skipped, never
  a traceback). amazon.com uses the ``dp-gw-na-js`` subdomain — the choice
  alexapy makes, mirrored by the pure :func:`websocket_url` so
  ``events status`` can show the URL without any socket.
* :func:`listen_events` connects, calls an optional ``on_event`` row
  callback per event (the CLI prints live / tests collect), stops on
  timeout, a ``max_events`` cap, the peer closing, or a truthy callback
  return, and ALWAYS closes the socket. Every failure is reported in the
  summary (``error``) — the socket never turns an exception into a silent
  "no events".
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .session import AlexaSessionError, validate_region

_log = logging.getLogger(__name__)

#: Poll interval while the listener loop waits for events / the timeout.
LISTEN_POLL_SECONDS = 0.5

#: Known GW channels (the values alexapy's own decoder switches on), mapped
#: to the readable names the CLI's ``--channel`` filter accepts.
CHANNEL_NAMES: dict[int, str] = {
    0x361: "handshake",
    0x362: "events",
    0x65: "heartbeat",
}

#: The service half of the TWIG frame: FABE (protocol messages) / TUNE (tuning)
#: — kept so the row is honest about the source, and so "TUNE" chatter can be
#: filtered out with ``--service fabe``.
KNOWN_SERVICES: tuple[str, ...] = ("FABE", "TUNE")


# ── pure helpers ──────────────────────────────────────────────────────────


def websocket_url(url: str = "amazon.co.uk") -> str:
    """The account's push-event WebSocket URL (pure).

    Validates the region against the same allow-list every other command
    uses and mirrors alexapy's subdomain choice: ``dp-gw-na-js`` for
    ``amazon.com``, ``dp-gw-na`` everywhere else.
    """
    url = validate_region(url)
    subdomain = "dp-gw-na-js" if url == "amazon.com" else "dp-gw-na"
    return f"wss://{subdomain}.{url}/tcomm/"


def mac_dms_ready(login: Any) -> bool:
    """Does this login carry the device-signing key the push stream needs? (pure).

    A ``mac_dms`` is minted by alexapy's own login dance; cookies imported
    from an arbitrary source may lack it, and alexapy raises
    ``AlexapyLoginError("mac_dms not detected")`` the moment the client is
    built. Checking it here means the CLI refuses *before* connecting with
    a message that names the fix. Both halves alexapy signs every request
    with must be present and non-empty — ``adp_token`` AND
    ``device_private_key`` (a dict holding either half alone would die in
    alexapy's ``_create_request_signature``, so "present but half-baked" is
    NOT ready).
    """
    try:
        mac_dms = getattr(login, "mac_dms", None)
        if not isinstance(mac_dms, dict):
            return False
        return all(
            bool(str(mac_dms.get(key) or "").strip()) for key in ("adp_token", "device_private_key")
        )
    except Exception:  # noqa: BLE001 — a hostile login object is "not ready", never a traceback
        return False


def channel_name(value: Any) -> str | None:
    """A GW channel number → readable name (``handshake``/``events``/``heartbeat``).

    Unknown channels stay reportable: they come back as the raw hex, because
    "unknown but seen" is real information and hiding it would lie the way
    collapsing it into ``events`` would.
    """
    if isinstance(value, str) and value.strip():
        value = int(value, 16)
    elif isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return CHANNEL_NAMES.get(value, f"0x{value:x}")


def _content_field(content: Any, name: str) -> Any:
    """One field off a decoded message's ``content`` half (pure).

    alexapy's FABE decoder puts ``Content`` objects here; TUNE messages put a
    ``dict`` or plain string instead. ``getattr`` on those with a ``None``
    default keeps every variant honest.
    """
    if content is None:
        return None
    if isinstance(content, dict):
        return content.get(name)
    try:
        return getattr(content, name)
    except Exception:  # noqa: BLE001 — junk is skip
        return None


def event_row(message: Any) -> dict[str, Any] | None:
    """One decoded alexapy ``Message`` → a display row (pure).

    ``None`` when the object is not a decodable message (junk, ``None``, a
    message that carries nothing at all) — the listener skips those rather
    than emitting half-built rows. The row keeps the fields a watcher needs:
    the TWIG ``service`` (FABE/TUNE), the readable ``channel``, the message
    type, the device the event is about, and the payload — the decoded
    JSON when the message carried one, else the raw payload string.
    """
    service = getattr(message, "service", None)
    if not isinstance(service, str) or not service:
        return None
    content = getattr(message, "content", None)
    payload_json = getattr(message, "json_payload", None)
    content_payload = _content_field(content, "payload")
    content_channel = _content_field(content, "channel")
    device = _content_field(content, "device_id_urn")
    msg_type = (
        _content_field(content, "message_type")
        if _content_field(content, "message_type")
        else getattr(message, "message_type", None)
    )
    sub_type = _content_field(content, "submessage_type")
    row: dict[str, Any] = {
        "service": service,
        "messageType": msg_type,
        "channel": channel_name(
            content_channel if content_channel is not None else getattr(message, "channel", None)
        ),
        "type": sub_type if sub_type else msg_type,
        "device": device,
        "payload": payload_json if payload_json else (content_payload or None),
    }
    return row


def filter_events(
    rows: list[dict[str, Any]],
    service: str | None = None,
    channel: str | None = None,
    contains: str | None = None,
) -> list[dict[str, Any]]:
    """Client-side selection over collected event rows (pure).

    ``--service`` matches the TWIG service (case-insensitive substring),
    ``--channel`` the readable channel name (or a hex form ``0x362``),
    ``--contains`` a substring of the rendered row (device URN, JSON dump,
    type) for the "show me only the music events" use. All combinable; all
    case-insensitive; empty rows pass through untouched.
    """
    service_key = (service or "").strip().lower()
    channel_key = (channel or "").strip().lower()
    contains_key = (contains or "").strip().lower()
    if not (service_key or channel_key or contains_key):
        return list(rows)
    out = []
    for row in rows:
        if service_key and service_key not in str(row.get("service") or "").lower():
            continue
        if channel_key and channel_key not in str(row.get("channel") or "").lower():
            continue
        if contains_key:
            blob = json_blob_row(row)
            if contains_key not in blob.lower():
                continue
        out.append(row)
    return out


def json_blob_row(row: dict[str, Any]) -> str:
    """A row flattened to searchable text (pure).

    The ``--contains`` haystack: payload JSON is dumped so nested strings
    are findable; everything else is stringified.
    """
    import json as _json

    parts = [str(v) for k, v in (row or {}).items() if k != "payload"]
    payload = (row or {}).get("payload")
    if isinstance(payload, (dict, list)):
        with_suppress = payload
        try:
            parts.append(_json.dumps(with_suppress, default=str, sort_keys=True))
        except (TypeError, ValueError):
            parts.append(str(payload))
    else:
        parts.append(str(payload or ""))
    return " ".join(parts)


def listen_summary(
    rows: list[dict[str, Any]], connected: bool, duration: float, error: str | None = None
) -> dict[str, Any]:
    """The row ``events listen`` reports back (pure).

    ``count``/``events`` separated so a long session can be summarised; the
    raw rows ride along as ``events``. ``error`` names what the socket
    reported (or a clean ``None``) — a silent failure would read as "nothing
    happened", which is never true.
    """
    return {
        "connected": bool(connected),
        "duration": round(max(0.0, float(duration)), 1),
        "count": len(rows),
        "events": list(rows),
        "error": error,
    }


# ── network (thin async wrapper over alexapy's websocket client) ─────────


async def listen_events(
    login,
    timeout: float = 60.0,
    max_events: int | None = None,
    on_event=None,
) -> dict[str, Any]:
    """Open the push-event stream and collect events until the stop condition.

    Builds alexapy's ``WebsocketEchoClient`` (callbacks record what happened,
    nothing is re-implemented), runs it until the ``timeout`` elapses, the
    ``max_events`` cap is hit, the peer closes, or ``on_event`` returns a
    truthy value to mean "stop here". ALWAYS closes the socket — timeout,
    cap and cancellation included.

    Raises ``AlexaSessionError`` up front (no socket ever opened) when the
    login lacks the ``mac_dms`` device-signing key — the fix is
    re-authentication, and alexapy's own error for it is a bare traceback.
    """
    try:
        from alexapy.alexawebsocket import WebsocketEchoClient
    except ImportError as exc:  # pragma: no cover - only without alexapy
        raise AlexaSessionError(
            "alexapy is not installed / too old for the push-event stream "
            f"({exc}). Upgrade with `pip install -U alexapy`."
        ) from exc

    if not mac_dms_ready(login):
        raise AlexaSessionError(
            "the saved session has no device-signing key (mac_dms), which the "
            "push-event stream requires. Re-authenticate (`auth login`, or "
            "re-import a fresh Home Assistant cookie) and try again."
        )

    state = {"connected": False, "closed": False, "error": None}
    rows: list[dict[str, Any]] = []

    async def _on_open() -> None:
        state["connected"] = True

    async def _on_close(*_args: Any) -> None:
        state["closed"] = True

    async def _on_error(err: str = "Unspecified") -> None:
        state["error"] = err or "Unspecified"

    async def _on_message(message: Any) -> None:
        row = event_row(message)
        if row is None:
            return
        rows.append(row)
        if on_event is not None and bool(on_event(row)):
            state["closed"] = True

    client = WebsocketEchoClient(
        login,
        msg_callback=_on_message,
        open_callback=_on_open,
        close_callback=_on_close,
        error_callback=_on_error,
    )
    started = time.monotonic()
    try:
        await client.async_run()
        deadline = started + max(0.0, float(timeout))
        while not state["closed"]:
            if time.monotonic() >= deadline:
                break
            if max_events is not None and len(rows) >= int(max_events):
                break
            await asyncio.sleep(LISTEN_POLL_SECONDS)
        try:
            websocket = getattr(client, "websocket", None)
            if websocket is not None:
                await websocket.close()
        except Exception:  # noqa: BLE001 — the socket is ending anyway; a close hiccup must not mask the collected rows
            _log.debug("websocket.close() failed", exc_info=True)
    except Exception as exc:  # noqa: BLE001 — connect/protocol failures land in the summary, never a silent empty listen
        state["error"] = state["error"] or f"{type(exc).__name__}: {exc}"
    duration = time.monotonic() - started
    return listen_summary(rows, state["connected"], duration, state["error"])
