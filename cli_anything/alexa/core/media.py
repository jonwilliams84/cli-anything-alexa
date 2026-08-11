"""Media transport + player state for physical Echo devices.

Wraps the device-bound half of ``AlexaAPI`` that the harness previously did not
expose at all: transport control (``play``/``pause``/``next``/``previous``/
``forward``/``rewind``/``stop``), ``set_volume``, ``shuffle``/``repeat``,
``play_music`` and the ``get_state`` player read.

Two things are worth knowing before extending this module:

* **Everything here is device-bound**, so the raw ``get_devices()`` record is
  wrapped in :class:`~cli_anything.alexa.core.device_ref.DeviceRef` — alexapy
  reads ``device_serial_number``/``_device_type`` as *attributes* off
  ``self._device`` and blows up on a plain dict.
* **alexapy's volume is a 0.0–1.0 float**, not a percentage: ``set_volume``
  multiplies by 100 before sending.  The CLI takes the human 0–100 and
  :func:`normalize_volume` does the conversion once, in one tested place, so a
  ``50`` can never be sent as "5000%".

Transport verbs and the player-state flattening are pure and unit-tested; only
the thin ``async def`` wrappers touch the network.
"""

from __future__ import annotations

from typing import Any

from cli_anything.alexa.core.device_ref import DeviceRef, to_device_ref
from cli_anything.alexa.core.devices_meta import fetch_devices, find_device

#: CLI verb → alexapy ``AlexaAPI`` method name.  All are zero-argument
#: transport commands posting to ``/api/np/command``.  ``stop`` is deliberately
#: NOT here: it goes through the sequence API and takes ``all_devices``.
TRANSPORT_COMMANDS: dict[str, str] = {
    "play": "play",
    "pause": "pause",
    "next": "next",
    "previous": "previous",
    "forward": "forward",
    "rewind": "rewind",
}

#: Music providers accepted by ``Alexa.Music.PlaySearchPhrase``.  Amazon takes
#: the provider id verbatim, so this is a convenience/validation list of the
#: commonly working ones rather than an exhaustive enum — hence
#: :func:`normalize_provider` only upper-cases and does not reject unknowns.
KNOWN_MUSIC_PROVIDERS: tuple[str, ...] = (
    "AMAZON_MUSIC",
    "CLOUDPLAYER",
    "TUNEIN",
    "SPOTIFY",
    "APPLE_MUSIC",
    "DEEZER",
    "I_HEART_RADIO",
)

DEFAULT_MUSIC_PROVIDER = "AMAZON_MUSIC"


# ── pure helpers ─────────────────────────────────────────────────────────


def normalize_volume(level: float | int | str) -> float:
    """Convert a human 0–100 volume to alexapy's 0.0–1.0 float (pure).

    Raises ``ValueError`` with a caller-facing message on anything that is not
    a number in range — the CLI surfaces those verbatim.
    """
    try:
        value = float(level)
    except (TypeError, ValueError):
        raise ValueError(f"volume must be a number between 0 and 100, got {level!r}") from None
    if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
        raise ValueError(f"volume must be a number between 0 and 100, got {level!r}")
    if not 0 <= value <= 100:
        raise ValueError(f"volume must be between 0 and 100, got {level!r}")
    return value / 100.0


def normalize_provider(provider: str | None) -> str:
    """Normalise a music-provider id (pure). Empty → the Amazon Music default."""
    cleaned = (provider or "").strip().replace("-", "_").replace(" ", "_")
    return cleaned.upper() if cleaned else DEFAULT_MUSIC_PROVIDER


def player_row(state: dict[str, Any] | None, device: str | None = None) -> dict[str, Any]:
    """Flatten a ``/api/np/player`` response into a display row (pure).

    The payload nests everything under ``playerInfo`` with the track split
    across ``infoText`` (title / subText1 = artist / subText2 = album).  Every
    field is optional — an idle speaker returns ``playerInfo: {}`` or even a
    bare ``{}`` — so each lookup is defensive and missing values come back as
    ``None`` rather than raising.
    """
    info = ((state or {}).get("playerInfo") or {}) if isinstance(state, dict) else {}
    text = info.get("infoText") or {}
    volume = info.get("volume") or {}
    provider = info.get("provider") or {}
    progress = info.get("progress") or {}
    return {
        "device": device,
        "state": info.get("state"),
        "title": text.get("title"),
        "artist": text.get("subText1"),
        "album": text.get("subText2"),
        "provider": provider.get("providerName"),
        "volume": volume.get("volume"),
        "muted": volume.get("muted"),
        "progress_seconds": progress.get("mediaProgress"),
        "duration_seconds": progress.get("mediaLength"),
    }


# ── device resolution ────────────────────────────────────────────────────


async def resolve_device(login, device: str | None, *, required: bool = True) -> DeviceRef:
    """Resolve a device name/serial to a :class:`DeviceRef`.

    With no name, falls back to the first *online* Echo (then the first Echo at
    all), matching how ``announce``/``run_routine`` pick a runner.  ``required``
    is kept for symmetry with callers that may allow an empty account.
    """
    devices = await fetch_devices(login)
    if not devices:
        raise ValueError("no Alexa devices found on the account")
    if device:
        target = find_device(devices, device)
        if not target:
            raise ValueError(f"no device matching {device!r}")
    elif required:
        target = next((d for d in devices if d.get("online")), devices[0])
    else:  # pragma: no cover - reserved for future optional-target callers
        return None
    return to_device_ref(target)


async def _api_for(login, device: str | None):
    """(DeviceRef, AlexaAPI) bound to one Echo."""
    from alexapy import AlexaAPI

    ref = await resolve_device(login, device)
    return ref, AlexaAPI(ref, login)


# ── live operations ──────────────────────────────────────────────────────


async def player_status(login, device: str | None = None) -> dict[str, Any]:
    """Read what an Echo is currently playing."""
    ref, api = await _api_for(login, device)
    state = await api.get_state()
    return player_row(state, device=ref.account_name)


async def transport(login, device: str | None, action: str) -> dict[str, Any]:
    """Run a transport command (play/pause/next/previous/forward/rewind)."""
    verb = (action or "").strip().lower()
    method = TRANSPORT_COMMANDS.get(verb)
    if not method:
        supported = ", ".join(sorted(TRANSPORT_COMMANDS))
        raise ValueError(f"unknown media action {action!r}; expected one of: {supported}")
    ref, api = await _api_for(login, device)
    await getattr(api, method)()
    return {"device": ref.account_name, "action": verb, "ok": True}


async def stop(login, device: str | None = None, all_devices: bool = False) -> dict[str, Any]:
    """Stop playback on one Echo, or on every device with ``all_devices``."""
    ref, api = await _api_for(login, device)
    await api.stop(all_devices=all_devices)
    return {
        "device": "all" if all_devices else ref.account_name,
        "action": "stop",
        "ok": True,
    }


async def set_volume(login, device: str | None, level: float | int | str) -> dict[str, Any]:
    """Set an Echo's volume from a 0–100 percentage."""
    fraction = normalize_volume(level)
    ref, api = await _api_for(login, device)
    await api.set_volume(fraction)
    return {"device": ref.account_name, "volume": round(fraction * 100)}


async def set_shuffle(login, device: str | None, enabled: bool) -> dict[str, Any]:
    """Turn shuffle on/off."""
    ref, api = await _api_for(login, device)
    await api.shuffle(bool(enabled))
    return {"device": ref.account_name, "shuffle": "on" if enabled else "off"}


async def set_repeat(login, device: str | None, enabled: bool) -> dict[str, Any]:
    """Turn repeat on/off."""
    ref, api = await _api_for(login, device)
    await api.repeat(bool(enabled))
    return {"device": ref.account_name, "repeat": "on" if enabled else "off"}


async def play_music(
    login,
    device: str | None,
    search_phrase: str,
    provider: str | None = None,
) -> dict[str, Any]:
    """Start music matching a search phrase from a provider."""
    phrase = (search_phrase or "").strip()
    if not phrase:
        raise ValueError("a search phrase is required (e.g. 'jazz radio')")
    provider_id = normalize_provider(provider)
    ref, api = await _api_for(login, device)
    await api.play_music(provider_id, phrase)
    return {"device": ref.account_name, "provider": provider_id, "search": phrase}
