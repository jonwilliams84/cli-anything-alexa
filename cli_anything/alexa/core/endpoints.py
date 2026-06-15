"""Endpoint-level operations over the modern Alexa **GraphQL** API.

The ``endpoints`` query is the one source of truth that ties together the three
ids a device carries:

    * ``id``                          = the GraphQL **endpoint id**
                                        ``amzn1.alexa.endpoint.<uuid>`` — used by
                                        groups + ``setEndpointFriendlyName`` (rename).
    * ``legacyAppliance.applianceId`` = the **applianceId** used by the phoenix
                                        DELETE. For HA-sourced devices its tail
                                        ``_<domain>#<object_id>`` decodes back to
                                        the HA entity (see ``appliances.parse_entity_id``).
    * ``friendlyNameObject.value.text`` = the current **display name**.

``manufacturerName == "Home Assistant"`` marks an HA-sourced device; anything
else (e.g. ``"Belkin International Inc."`` for Tasmota-Wemo plugs) is native and
has no HA entity — such devices can only be targeted by display name.

All network calls go through ``AlexaAPI._static_request`` so auth/headers/host
are correct (the nexus host, NOT the web host — see ``groups.py``). The pure
resolution / duplicate-detection logic below has no ``alexapy`` dependency and
is unit-tested.

Reachability note: the ``Endpoint`` GraphQL type exposes ``enablement`` (a clean
enum, e.g. ``ENABLED``) plus ``connections`` / ``endpointReports``. Only
``enablement`` introspected as a clean, consistently-present scalar, so
``device_rows`` surfaces it as ``enabled``; a true online/reachability column was
deliberately skipped because the nested ``connections`` / ``endpointReports``
shapes were not consistently available on the live account.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from cli_anything.alexa.core.appliances import HA_MANUFACTURER, parse_entity_id
from cli_anything.alexa.core.groups import normalize_name

# ── GraphQL documents ──────────────────────────────────────────────────────

# The canonical device query — reused for rename / delete-by-name|entity /
# duplicates / groups --device. Pulls every id a device carries.
ENDPOINTS_QUERY = (
    "query{ endpoints(endpointsQueryParams:{paginationParams:"
    "{disablePagination:true}}){ items{ id legacyAppliance{ applianceId "
    "manufacturerName friendlyName } friendlyNameObject{ value{ text } } "
    "enablement } } }"
)

_RENAME_MUTATION = (
    "mutation($in:SetEndpointFriendlyNameInput!){ "
    "setEndpointFriendlyName(input:$in){ __typename } }"
)


# ── pure helpers: flatten + classify ───────────────────────────────────────

def endpoint_record(item: dict[str, Any]) -> dict[str, Any]:
    """Flatten one raw ``endpoints`` item into a stable record (pure).

    The display name prefers ``friendlyNameObject.value.text`` (what the app
    shows) and falls back to ``legacyAppliance.friendlyName``.
    """
    item = item or {}
    legacy = item.get("legacyAppliance") or {}
    appliance_id = legacy.get("applianceId") or ""
    manufacturer = legacy.get("manufacturerName")
    display = (((item.get("friendlyNameObject") or {}).get("value") or {})
               .get("text")) or legacy.get("friendlyName")
    ha_sourced = manufacturer == HA_MANUFACTURER
    entity_id = parse_entity_id(appliance_id) if ha_sourced else None
    return {
        "endpointId": item.get("id"),
        "applianceId": appliance_id,
        "name": display,
        "manufacturer": manufacturer,
        "ha_sourced": ha_sourced,
        "entity_id": entity_id,
        "enabled": item.get("enablement"),
    }


def endpoint_records(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten a list of raw ``endpoints`` items to records (pure)."""
    return [endpoint_record(it) for it in (items or [])]


def device_rows(
    records: list[dict[str, Any]],
    *,
    native_only: bool = False,
    manufacturer: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Display rows for ``devices list`` (pure).

    Adds a ``source`` marker (``HA`` vs ``native``) and supports the
    ``--native-only`` / ``--manufacturer <substr>`` filters. ``manufacturer`` is
    a case-insensitive substring match.
    """
    out: list[dict[str, Any]] = []
    man_sub = (manufacturer or "").strip().lower()
    for r in records or []:
        if native_only and r.get("ha_sourced"):
            continue
        if man_sub and man_sub not in (r.get("manufacturer") or "").lower():
            continue
        out.append(
            {
                "name": r.get("name"),
                "manufacturer": r.get("manufacturer"),
                "source": "HA" if r.get("ha_sourced") else "native",
                "enabled": r.get("enabled"),
                "entity_id": r.get("entity_id"),
                "applianceId": r.get("applianceId"),
                "endpointId": r.get("endpointId"),
            }
        )
    return out


# ── pure helpers: target resolution ────────────────────────────────────────

def resolve_target(records: list[dict[str, Any]], target: str) -> list[dict[str, Any]]:
    """Resolve a ``<target>`` to matching endpoint records (pure).

    Precedence (first tier that yields any match wins):
      1. exact ``applianceId``
      2. exact endpoint id (``amzn1.alexa.endpoint.*``)
      3. exact display name (case-sensitive)
      4. normalized / case-insensitive display name

    Returns the list of matches for the winning tier. The caller decides what
    to do with 0 (no such device), 1 (resolved), or >1 (ambiguous — abort and
    disambiguate). A native + HA twin can legitimately share a display name, so
    name-based tiers may return more than one.
    """
    records = records or []
    t = (target or "").strip()
    if not t:
        return []
    # 1. exact applianceId
    hits = [r for r in records if r.get("applianceId") == t]
    if hits:
        return hits
    # 2. exact endpoint id
    hits = [r for r in records if r.get("endpointId") == t]
    if hits:
        return hits
    # 3. exact display name
    hits = [r for r in records if r.get("name") == t]
    if hits:
        return hits
    # 4. normalized display name
    norm = normalize_name(t)
    if norm:
        hits = [r for r in records if normalize_name(r.get("name") or "") == norm]
        if hits:
            return hits
    return []


def resolve_by_entity(records: list[dict[str, Any]], entity_id: str) -> list[dict[str, Any]]:
    """Resolve an HA ``<domain>.<object_id>`` entity id to records (pure)."""
    ent = (entity_id or "").strip()
    if not ent:
        return []
    return [r for r in (records or []) if r.get("entity_id") == ent]


def resolve_by_name(records: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    """Resolve a device by normalized display name -> records (pure).

    This is how native / non-HA devices (e.g. Tasmota-Wemo plugs) are targeted —
    they have no HA entity, only a friendly name.
    """
    norm = normalize_name(name or "")
    if not norm:
        return []
    return [r for r in (records or []) if normalize_name(r.get("name") or "") == norm]


def ambiguous_matches(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compact descriptor rows for an ambiguity abort message (pure)."""
    return [
        {
            "name": r.get("name"),
            "source": "HA" if r.get("ha_sourced") else "native",
            "manufacturer": r.get("manufacturer"),
            "entity_id": r.get("entity_id"),
            "applianceId": r.get("applianceId"),
            "endpointId": r.get("endpointId"),
        }
        for r in (records or [])
    ]


# ── pure helpers: duplicate detection ──────────────────────────────────────

def find_duplicates(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Detect the same physical device exposed twice (pure).

    Groups records by normalized display name and reports any name shared by
    more than one endpoint. Each reported pair/cluster flags whether it is the
    classic native+HA twin (a native appliance and an HA-sourced appliance under
    the same name — usually the thing to de-dupe). The output lets a human
    decide which copy to drop; nothing is deleted here.
    """
    by_name: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for r in records or []:
        norm = normalize_name(r.get("name") or "")
        if not norm:
            continue
        if norm not in by_name:
            by_name[norm] = []
            order.append(norm)
        by_name[norm].append(r)

    out: list[dict[str, Any]] = []
    for norm in order:
        group = by_name[norm]
        if len(group) < 2:
            continue
        has_ha = any(g.get("ha_sourced") for g in group)
        has_native = any(not g.get("ha_sourced") for g in group)
        out.append(
            {
                "name": group[0].get("name"),
                "count": len(group),
                "native_plus_ha": bool(has_ha and has_native),
                "endpoints": [
                    {
                        "source": "HA" if g.get("ha_sourced") else "native",
                        "manufacturer": g.get("manufacturer"),
                        "entity_id": g.get("entity_id"),
                        "applianceId": g.get("applianceId"),
                        "endpointId": g.get("endpointId"),
                    }
                    for g in group
                ],
            }
        )
    return out


# ── pure helpers: native-source delete warning + re-sync verify ────────────
#
# Native (non-HA) devices re-sync from their cloud skill / bridge, so deleting
# them in Alexa alone doesn't stick (proven: a Tuya device re-synced from Smart
# Life; a Philips Hue device re-synced from the bridge). HA-sourced devices are
# safe to delete from Alexa.

# Best-effort manufacturer -> "remove it here" source hint for the warning.
_NATIVE_SOURCE_HINTS = {
    "signify netherlands b.v.": "Hue bridge (Philips Hue app)",
    "philips": "Hue bridge (Philips Hue app)",
    "tuya": "the Tuya / Smart Life app",
    "smart life": "the Smart Life app",
    "belkin international inc.": "the Wemo app",
}


def native_source_hint(manufacturer: Optional[str]) -> str:
    """A human source name for where a native device re-syncs from (pure)."""
    m = (manufacturer or "").strip().lower()
    for key, hint in _NATIVE_SOURCE_HINTS.items():
        if key in m:
            return hint
    if m:
        return f"its source app / bridge ({manufacturer})"
    return "its source app / bridge"


def native_delete_warning(record: dict[str, Any]) -> Optional[str]:
    """Warn if deleting ``record`` won't stick because it's native (pure).

    Returns ``None`` for HA-sourced devices (safe to delete). For a native
    device (``manufacturerName != "Home Assistant"``) returns a sentence telling
    the user it will re-sync and where to remove it permanently.
    """
    if record.get("ha_sourced"):
        return None
    name = record.get("name") or record.get("applianceId") or "device"
    man = record.get("manufacturer")
    hint = native_source_hint(man)
    man_part = f" {man}" if man else ""
    return (f"{name} is a native{man_part} device — deleting may not stick; it "
            f"re-syncs from its source. Remove it at source ({hint}) to clear it "
            "permanently.")


def reappeared_after_delete(
    deleted: list[dict[str, Any]],
    records_after: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Which just-deleted devices re-appeared after a re-discovery (pure).

    ``deleted`` rows carry ``applianceId`` / ``name``; ``records_after`` is the
    post-discovery endpoint record set. A delete is considered "reappeared" when
    a record matches by ``applianceId`` (exact) or, failing that, by normalized
    ``name``. Returns the reappeared rows annotated with ``reappeared_as``.
    """
    after_appliance = {r.get("applianceId") for r in (records_after or []) if r.get("applianceId")}
    after_names = {normalize_name(r.get("name") or "") for r in (records_after or [])}
    out: list[dict[str, Any]] = []
    for d in deleted or []:
        aid = d.get("applianceId")
        name = d.get("name")
        if aid and aid in after_appliance:
            out.append({**d, "reappeared_as": "applianceId"})
        elif name and normalize_name(name) in after_names:
            out.append({**d, "reappeared_as": "name"})
    return out


# ── pure helpers: GraphQL variables builders ───────────────────────────────

def build_rename_variables(endpoint_id: str, friendly_name: str) -> dict[str, Any]:
    """Variables for ``setEndpointFriendlyName`` (pure)."""
    return {"in": {"endpointId": endpoint_id, "friendlyName": friendly_name}}


# ── pure helpers: DACS speakable-name validation ───────────────────────────
#
# Amazon's rename API (``setEndpointFriendlyName``) validates the new name
# through DACS, which REJECTS non-speakable names — notably anything with a
# hyphen — with ``"Invalid input. Invalid input from DACS"`` (errorCode
# ``BAD_REQUEST``). Proven live: ``"elt-k8s-1 Temperature"`` was refused,
# ``"elt k8s 1 Temperature"`` accepted. So we (a) offer a transform that makes a
# name speakable and (b) a predicate to pre-warn before we ever hit the API.

# Characters DACS is known to reject in a friendly name.
_DACS_BAD_CHARS_RE = re.compile(r"-")
# Control chars (e.g. a stray \x05) that creep into names and must be stripped.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def speakable_name(s: str) -> str:
    """Transform a name into a DACS-speakable form (pure).

    Hyphens -> spaces, control chars stripped, whitespace collapsed/ trimmed.
    e.g. ``"elt-k8s-1 Temperature"`` -> ``"elt k8s 1 Temperature"``.
    """
    if not s:
        return ""
    out = _CONTROL_CHARS_RE.sub("", s)
    out = out.replace("-", " ")
    out = re.sub(r"\s+", " ", out).strip()
    return out


def is_speakable(s: str) -> bool:
    """True when ``s`` is already DACS-speakable (no transform needed) (pure).

    A name is speakable when it carries no hyphen and no control char. (Whitespace
    quirks are cosmetic and tolerated by DACS, so they don't flip this.)
    """
    if s is None:
        return True
    if _DACS_BAD_CHARS_RE.search(s):
        return False
    if _CONTROL_CHARS_RE.search(s):
        return False
    return True


def speakable_warning(s: str) -> Optional[str]:
    """A friendly "Amazon will reject ..." warning for a non-speakable name, or None.

    Used both to pre-validate a planned rename and to explain an actual
    BAD_REQUEST / DACS error from the API. Returns ``None`` when ``s`` is fine.
    """
    if is_speakable(s):
        return None
    return (f"Amazon will reject {s!r} — non-speakable (hyphen / control char); "
            f"suggest {speakable_name(s)!r}")


def is_dacs_error(message: str) -> bool:
    """True when an API error message is the DACS/BAD_REQUEST rename rejection (pure)."""
    m = (message or "").lower()
    return "dacs" in m or "bad_request" in m or "invalid input" in m


# ── pure helpers: bulk / pattern rename planning ───────────────────────────

class PatternError(ValueError):
    """A malformed ``s/REGEX/REPL/`` pattern."""


def parse_sed(pattern: str) -> tuple[re.Pattern, str, bool]:
    """Parse a sed-style ``s/REGEX/REPL/[flags]`` substitution (pure).

    Supports the ``i`` (case-insensitive) and ``g`` (global) flags. The
    delimiter is whatever follows ``s`` (usually ``/``), so ``s|a|b|`` works
    too. Backslash-escaped delimiters inside the pattern/replacement are kept.
    Returns ``(compiled_regex, replacement, global_flag)``. The replacement uses
    Python ``re`` backrefs (``\\1`` etc.). ``count`` for ``re.sub`` is
    ``0`` when ``g`` else ``1``.
    """
    if not pattern or len(pattern) < 2 or pattern[0] != "s":
        raise PatternError(
            f"not a substitution pattern (expected s/REGEX/REPL/): {pattern!r}")
    delim = pattern[1]
    body = pattern[2:]
    # Split on UNescaped delimiters into exactly: regex, repl, flags.
    parts: list[str] = []
    cur: list[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            # keep escape pairs verbatim (so \/ inside regex survives)
            cur.append(ch)
            cur.append(body[i + 1])
            i += 2
            continue
        if ch == delim:
            parts.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    parts.append("".join(cur))
    if len(parts) < 3:
        raise PatternError(
            f"malformed substitution (need s{delim}REGEX{delim}REPL{delim}): {pattern!r}")
    regex_src, repl, flags = parts[0], parts[1], parts[2]
    # Unescape the delimiter in regex/repl (\/ -> /) now that splitting is done.
    if delim != "\\":
        regex_src = regex_src.replace("\\" + delim, delim)
        repl = repl.replace("\\" + delim, delim)
    re_flags = 0
    is_global = False
    for f in flags:
        if f == "i":
            re_flags |= re.IGNORECASE
        elif f == "g":
            is_global = True
        else:
            raise PatternError(f"unsupported flag {f!r} in {pattern!r} (only i/g)")
    try:
        compiled = re.compile(regex_src, re_flags)
    except re.error as exc:
        raise PatternError(f"bad regex in {pattern!r}: {exc}") from exc
    return compiled, repl, is_global


def apply_sed(pattern: str, name: str) -> str:
    """Apply a sed substitution to a single name (pure)."""
    compiled, repl, is_global = parse_sed(pattern)
    return compiled.sub(repl, name or "", count=0 if is_global else 1)


def plan_pattern_renames(
    records: list[dict[str, Any]],
    pattern: str,
    *,
    speakable: bool = False,
) -> list[dict[str, Any]]:
    """Plan the rename set produced by applying ``pattern`` to every name (pure).

    For each record whose current display name CHANGES under the substitution,
    emit ``{endpointId, applianceId, old, new, source, warning}``. No-ops (new ==
    old, or an empty new) are skipped. When ``speakable`` is set, ``new`` is run
    through :func:`speakable_name`; otherwise ``warning`` carries the DACS
    pre-validation message (or ``None``) so the CLI can warn but still proceed.
    """
    compiled, repl, is_global = parse_sed(pattern)
    out: list[dict[str, Any]] = []
    for r in records or []:
        old = r.get("name") or ""
        new = compiled.sub(repl, old, count=0 if is_global else 1)
        if speakable:
            new = speakable_name(new)
        if not new or new == old:
            continue
        out.append({
            "endpointId": r.get("endpointId"),
            "applianceId": r.get("applianceId"),
            "old": old,
            "new": new,
            "source": "HA" if r.get("ha_sourced") else "native",
            "warning": speakable_warning(new),
        })
    return out


def parse_rename_map(text: str) -> list[tuple[str, str]]:
    """Parse a ``--map`` file body into ``(target, new_name)`` pairs (pure).

    Each non-blank, non-``#``-comment line is ``current name => new name`` (or
    ``endpointId => new name`` — the left side is whatever ``resolve_target``
    accepts). Whitespace around each side is trimmed; a trailing inline
    ``# comment`` is NOT stripped (names may legitimately contain ``#``), so put
    comments on their own line. The separator is ``=>``.
    """
    out: list[tuple[str, str]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=>" not in line:
            raise ValueError(f"map line is missing '=>': {raw!r}")
        target, _, new = line.partition("=>")
        target = target.strip()
        new = new.strip()
        if not target or not new:
            raise ValueError(f"map line has an empty side: {raw!r}")
        out.append((target, new))
    return out


def plan_map_renames(
    records: list[dict[str, Any]],
    pairs: list[tuple[str, str]],
    *,
    speakable: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve ``(target, new)`` map pairs against records (pure).

    Returns ``(planned, problems)``. Each planned entry is the same shape as
    :func:`plan_pattern_renames`. ``problems`` collects unresolved (``count``
    0) or ambiguous (``count`` >1) targets so the CLI can abort/report. No-ops
    (new == current) are skipped. ``speakable`` runs ``new`` through
    :func:`speakable_name`.
    """
    planned: list[dict[str, Any]] = []
    problems: list[dict[str, Any]] = []
    for target, new in pairs:
        matches = resolve_target(records, target)
        if len(matches) != 1:
            problems.append({
                "target": target,
                "new": new,
                "count": len(matches),
                "reason": "no match" if not matches else "ambiguous",
            })
            continue
        rec = matches[0]
        old = rec.get("name") or ""
        final = speakable_name(new) if speakable else new
        if not final or final == old:
            continue
        planned.append({
            "endpointId": rec.get("endpointId"),
            "applianceId": rec.get("applianceId"),
            "old": old,
            "new": final,
            "source": "HA" if rec.get("ha_sourced") else "native",
            "warning": speakable_warning(final),
        })
    return planned, problems


# ── network (alexapy GraphQL / phoenix via _static_request) ────────────────

async def _graphql(login, query: str, variables: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """POST a GraphQL doc to /nexus/v1/graphql via alexapy's static request.

    Reuses ``AlexaAPI._static_request`` so auth/headers/host are correct.
    Raises ``RuntimeError`` if the response carries GraphQL ``errors``.
    """
    from alexapy import AlexaAPI

    data: dict[str, Any] = {"query": query}
    if variables is not None:
        data["variables"] = variables
    resp = await AlexaAPI._static_request(
        "post", login, "/nexus/v1/graphql", data=data
    )
    body = json.loads(await resp.text())
    errors = body.get("errors")
    if errors:
        raise RuntimeError(f"GraphQL error: {json.dumps(errors)[:300]}")
    return body


async def fetch_endpoints(login) -> list[dict[str, Any]]:
    """Raw items from the canonical ``endpoints`` query."""
    body = await _graphql(login, ENDPOINTS_QUERY)
    items = ((body.get("data") or {}).get("endpoints") or {}).get("items") or []
    return list(items)


async def fetch_endpoint_records(login) -> list[dict[str, Any]]:
    """Flattened endpoint records (id/applianceId/name/manufacturer/entity)."""
    return endpoint_records(await fetch_endpoints(login))


async def rename_endpoint(login, endpoint_id: str, friendly_name: str) -> dict[str, Any]:
    """setEndpointFriendlyName — rename a device by its endpoint id.

    On a DACS / BAD_REQUEST rejection (non-speakable name, e.g. a hyphen) the
    raw GraphQL error is caught and re-raised as a ``ValueError`` carrying the
    friendly ``speakable_warning`` suggestion instead of the opaque API blob.
    """
    variables = build_rename_variables(endpoint_id, friendly_name)
    try:
        body = await _graphql(login, _RENAME_MUTATION, variables)
    except RuntimeError as exc:
        if is_dacs_error(str(exc)):
            warn = speakable_warning(friendly_name) or (
                f"Amazon rejected {friendly_name!r} (non-speakable name)")
            raise ValueError(warn) from exc
        raise
    return {
        "endpointId": endpoint_id,
        "friendlyName": friendly_name,
        "result": (body.get("data") or {}).get("setEndpointFriendlyName"),
    }


async def apply_renames(login, planned: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Execute a planned bulk rename set (each entry has ``endpointId`` + ``new``).

    Per-entry errors (e.g. a DACS rejection surfaced as ``ValueError``) are
    captured into that entry's result so one bad name doesn't abort the batch.
    """
    results: list[dict[str, Any]] = []
    for p in planned or []:
        eid = p.get("endpointId")
        new = p.get("new")
        entry = {"old": p.get("old"), "new": new, "endpointId": eid}
        if not eid:
            results.append({**entry, "ok": False, "error": "no endpoint id"})
            continue
        try:
            res = await rename_endpoint(login, eid, new)
            entry["ok"] = True
            entry["result"] = res.get("result")
        except ValueError as exc:
            entry["ok"] = False
            entry["error"] = str(exc)
        results.append(entry)
    return results
