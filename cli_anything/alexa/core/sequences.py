"""The **behaviour/sequence** surface: make an Echo do anything you could *say*.

Everything the Alexa app can trigger on a speaker that is not transport
(``media``) or plain TTS (``speak``) goes through one endpoint —
``POST /api/behaviors/preview`` — driven by alexapy's ``send_sequence`` /
``run_behavior``.  This module wraps the four useful entry points into it:

* :func:`run_command` — ``AlexaAPI.run_custom``: send **the literal text you
  would say** ("turn off the kitchen lights", "what's the weather").  This is
  the single highest-leverage call in the whole API: anything Alexa understands
  by voice, including skills and devices the harness has no typed command for,
  is reachable through it.
* :func:`run_sequence` — the built-in ``Alexa.*.Play`` behaviours (weather,
  traffic, flash briefing, good morning/night, joke, story, calendar…).
* :func:`run_skill` — ``Alexa.Operation.SkillConnections.Launch`` by skill id.
* :func:`play_sound` — the Alexa soundbank (``Alexa.Sound``).

Four things are worth knowing before extending this module:

* **All four are device-bound.**  ``send_sequence`` reads ``_device_type`` /
  ``device_serial_number`` / ``_locale`` as *attributes* off ``self._device``,
  so the raw ``get_devices()`` record must be wrapped in a
  :class:`~cli_anything.alexa.core.device_ref.DeviceRef` — the same contract
  ``media``/``control`` obey.  Device resolution is shared with ``media`` so
  "no device given" means the first *online* Echo everywhere.
* **``queue_delay`` is a real behavioural knob, not noise.**  ``run_behavior``
  batches every sequence issued within that window into ONE serial/parallel
  node, which is how the app plays several commands back to back.  alexapy's
  defaults differ per call (``run_custom``/``run_skill`` = 0, sound/sequence =
  1.5), so :func:`normalize_queue_delay` returns ``None`` for "not specified"
  and the wrappers then omit the argument rather than flattening those
  defaults to one of our own.
* **Unknown ids are passed through, unknown *names* are rejected.**  Amazon
  keeps adding sequence types and soundbank ids, so anything already shaped
  like one (``Alexa.…``/a bare snake_case sound id) is sent verbatim; a
  friendly alias that is not in the catalog is refused locally with the list of
  alternatives, because the API answers an unknown sequence with a generic
  failure that tells the user nothing.
* **A text command is a *voice* command.**  Alexa answers out loud on the
  target speaker; there is no response payload to read back (the behaviours
  endpoint returns an empty body).  Read what happened afterwards with
  ``activity history`` — that pairing is the intended workflow.

Everything above the "live operations" divider is pure and unit-tested; only
the thin ``async def`` wrappers touch the network.
"""

from __future__ import annotations

import re
from typing import Any

from cli_anything.alexa.core.media import resolve_device

#: Friendly CLI name → Alexa sequence type.  Taken from alexapy's documented
#: ``send_sequence`` list (the alexa_media_player wiki's "sequence commands").
SEQUENCE_COMMANDS: dict[str, str] = {
    "weather": "Alexa.Weather.Play",
    "traffic": "Alexa.Traffic.Play",
    "flash-briefing": "Alexa.FlashBriefing.Play",
    "good-morning": "Alexa.GoodMorning.Play",
    "good-night": "Alexa.GoodNight.Play",
    "sing": "Alexa.SingASong.Play",
    "story": "Alexa.TellStory.Play",
    "fun-fact": "Alexa.FunFact.Play",
    "joke": "Alexa.Joke.Play",
    "clean-up": "Alexa.CleanUp.Play",
    "calendar-today": "Alexa.Calendar.PlayToday",
    "calendar-tomorrow": "Alexa.Calendar.PlayTomorrow",
    "calendar-next": "Alexa.Calendar.PlayNext",
}

#: A curated slice of the Alexa Skills Kit soundbank (``Alexa.Sound``).  Not
#: exhaustive by design — any other soundbank id is accepted verbatim.
SOUND_ALIASES: dict[str, str] = {
    "air-horn": "air_horn_03",
    "alarm": "amzn_sfx_scifi_alarm_04",
    "applause": "amzn_sfx_crowd_applause_01",
    "bell": "bell_02",
    "boing": "boing_01",
    "camera": "camera_01",
    "cat": "amzn_sfx_cat_meow_1x_01",
    "cheer": "amzn_sfx_large_crowd_cheer_01",
    "church-bell": "amzn_sfx_church_bell_1x_02",
    "dog": "amzn_sfx_dog_med_bark_1x_02",
    "doorbell": "amzn_sfx_doorbell_chime_01",
    "lion": "amzn_sfx_lion_roar_02",
    "rooster": "amzn_sfx_rooster_crow_01",
    "ticking": "clock_ticking_01",
    "trumpet": "amzn_sfx_trumpet_bugle_04",
    "zap": "zap_01",
}

#: alexapy's own ``run_custom`` skill id — recorded here only so the docs and
#: tests can name the mechanism; the CLI never sends it directly.
TEXT_COMMAND_SKILL_ID = "amzn1.ask.1p.tellalexa"

#: Skill ids Amazon issues.  ``amzn1.ask.skill.<uuid>`` is a third-party skill;
#: ``amzn1.ask.1p.*`` is a first-party (Amazon-built) one.
_SKILL_ID_RE = re.compile(r"^amzn1\.ask\.(skill\.[0-9a-zA-Z-]+|1p\.[0-9a-zA-Z._-]+)$")

#: A raw soundbank id: lowercase, digits and underscores (``amzn_sfx_…``,
#: ``air_horn_03``, ``bell_02``).
_SOUND_ID_RE = re.compile(r"^[a-z][a-z0-9_]*[0-9a-z]$")


# ── pure helpers ─────────────────────────────────────────────────────────


def _slug(name: str) -> str:
    """Normalise a friendly name: case/space/underscore/hyphen insensitive."""
    return re.sub(r"[\s_]+", "-", (name or "").strip().lower()).strip("-")


def normalize_command_text(text: str | None) -> str:
    """Validate the literal utterance for :func:`run_command` (pure).

    Only emptiness is refused: Amazon parses the text exactly as if it had been
    spoken, so second-guessing the wording here would block legitimate phrases.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        raise ValueError("a command is required, e.g. 'turn off the kitchen lights'")
    return cleaned


def normalize_sequence(name: str | None) -> str:
    """Friendly name → ``Alexa.*.Play`` sequence type (pure).

    An id that already looks like a sequence type (``Alexa.…``) is passed
    through untouched so newly added behaviours work without a release.
    """
    raw = (name or "").strip()
    if not raw:
        raise ValueError(f"a sequence is required, one of: {', '.join(sorted(SEQUENCE_COMMANDS))}")
    if raw.startswith("Alexa."):
        return raw
    slug = _slug(raw)
    if slug in SEQUENCE_COMMANDS:
        return SEQUENCE_COMMANDS[slug]
    raise ValueError(
        f"unknown sequence {name!r}; expected one of: {', '.join(sorted(SEQUENCE_COMMANDS))} "
        "(or a raw 'Alexa.*.Play' type)"
    )


def normalize_sound(name: str | None) -> str:
    """Friendly alias or raw soundbank id → the id Amazon wants (pure)."""
    raw = (name or "").strip()
    if not raw:
        raise ValueError(f"a sound is required, e.g. one of: {', '.join(sorted(SOUND_ALIASES))}")
    slug = _slug(raw)
    if slug in SOUND_ALIASES:
        return SOUND_ALIASES[slug]
    candidate = raw.lower()
    if _SOUND_ID_RE.match(candidate):
        # A raw soundbank id (amzn_sfx_…): the catalog is Amazon's and grows,
        # so accept it verbatim rather than gate playback on our own list.
        return candidate
    raise ValueError(
        f"unknown sound {name!r}; expected a soundbank id (e.g. amzn_sfx_doorbell_chime_01) "
        f"or one of: {', '.join(sorted(SOUND_ALIASES))}"
    )


def normalize_skill_id(skill_id: str | None) -> str:
    """Validate an Alexa skill id (pure).

    Launching a bad id fails silently on the device (the behaviour node just
    does nothing), so the shape is checked locally where it can be explained.
    """
    raw = (skill_id or "").strip()
    if not raw:
        raise ValueError("a skill id is required, e.g. amzn1.ask.skill.<uuid>")
    if not _SKILL_ID_RE.match(raw):
        raise ValueError(
            f"{skill_id!r} is not an Alexa skill id; expected amzn1.ask.skill.<uuid> "
            "(or amzn1.ask.1p.<name> for a first-party skill)"
        )
    return raw


def normalize_queue_delay(value: float | int | str | None) -> float | None:
    """Validate ``--queue-delay`` seconds (pure). ``None`` = alexapy's default.

    Returning ``None`` rather than a number of our own matters: alexapy uses a
    different default per call (0 for text commands/skills, 1.5 for sounds and
    sequences), and the wrappers omit the argument entirely when it is ``None``.
    """
    if value is None or value == "":
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"queue delay must be a number of seconds, got {value!r}") from None
    if seconds != seconds or seconds in (float("inf"), float("-inf")):  # NaN / inf
        raise ValueError(f"queue delay must be a number of seconds, got {value!r}")
    if seconds < 0:
        raise ValueError(f"queue delay must not be negative, got {value!r}")
    return seconds


def sequence_rows() -> list[dict[str, Any]]:
    """Catalog rows for the built-in sequences (pure, no network)."""
    return [
        {"name": name, "sequence": SEQUENCE_COMMANDS[name]} for name in sorted(SEQUENCE_COMMANDS)
    ]


def sound_rows() -> list[dict[str, Any]]:
    """Catalog rows for the known soundbank aliases (pure, no network)."""
    return [{"name": name, "sound": SOUND_ALIASES[name]} for name in sorted(SOUND_ALIASES)]


def catalog(kind: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """The `run catalog` payload: sequences, sounds, or both (pure)."""
    want = (kind or "all").strip().lower()
    if want in ("all", ""):
        return {"sequences": sequence_rows(), "sounds": sound_rows()}
    if want in ("sequence", "sequences"):
        return {"sequences": sequence_rows()}
    if want in ("sound", "sounds"):
        return {"sounds": sound_rows()}
    raise ValueError(f"unknown catalog {kind!r}; expected 'sequences', 'sounds' or 'all'")


# ── live operations ──────────────────────────────────────────────────────


async def _api_for(login, device: str | None):
    """(DeviceRef, AlexaAPI) bound to one Echo — shared with ``media``."""
    from alexapy import AlexaAPI

    ref = await resolve_device(login, device)
    return ref, AlexaAPI(ref, login)


def _delay_kwargs(queue_delay: float | None) -> dict[str, float]:
    """Only pass ``queue_delay`` when the user asked for one (see module docs)."""
    return {} if queue_delay is None else {"queue_delay": queue_delay}


async def run_command(
    login,
    device: str | None,
    text: str,
    queue_delay: float | int | str | None = None,
) -> dict[str, Any]:
    """Speak-to-Alexa-in-text: run the literal utterance on one Echo."""
    utterance = normalize_command_text(text)
    delay = normalize_queue_delay(queue_delay)
    ref, api = await _api_for(login, device)
    await api.run_custom(utterance, **_delay_kwargs(delay))
    return {"device": ref.account_name, "command": utterance, "ok": True}


async def run_sequence(
    login,
    device: str | None,
    name: str,
    queue_delay: float | int | str | None = None,
) -> dict[str, Any]:
    """Run one of Alexa's built-in behaviours (weather, joke, good night…)."""
    sequence = normalize_sequence(name)
    delay = normalize_queue_delay(queue_delay)
    ref, api = await _api_for(login, device)
    await api.send_sequence(sequence, **_delay_kwargs(delay))
    return {"device": ref.account_name, "sequence": sequence, "ok": True}


async def run_skill(
    login,
    device: str | None,
    skill_id: str,
    queue_delay: float | int | str | None = None,
) -> dict[str, Any]:
    """Launch a skill by id on one Echo."""
    skill = normalize_skill_id(skill_id)
    delay = normalize_queue_delay(queue_delay)
    ref, api = await _api_for(login, device)
    await api.run_skill(skill, **_delay_kwargs(delay))
    return {"device": ref.account_name, "skill": skill, "ok": True}


async def play_sound(
    login,
    device: str | None,
    sound: str,
    queue_delay: float | int | str | None = None,
) -> dict[str, Any]:
    """Play a soundbank sound on one Echo."""
    sound_id = normalize_sound(sound)
    delay = normalize_queue_delay(queue_delay)
    ref, api = await _api_for(login, device)
    await api.play_sound(sound_id, **_delay_kwargs(delay))
    return {"device": ref.account_name, "sound": sound_id, "ok": True}
