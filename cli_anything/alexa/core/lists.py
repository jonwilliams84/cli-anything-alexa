"""Alexa shopping & to-do lists (the ``/alexashoppinglists/api/v2`` surface).

The one big Alexa capability no earlier harness module touched: the shopping
list, the to-do list and any custom lists in the Alexa app. Amazon moved this
surface OFF the ``alexa.amazon.<tld>`` web host onto
``https://www.amazon.<tld>/alexashoppinglists/api/v2/...`` (same session
cookies alexapy already holds), and alexapy does not wrap it — so this module
talks to it via ``AlexaAPI._static_request(..., sub_domain="www")``, exactly
like ``groups.py`` reuses the helper for the nexus GraphQL host. Do NOT
hand-roll the host/headers: ``_static_request`` picks up the login session,
the auth headers and the 401-retry correctly.

Endpoints (reverse-engineered from the live Alexa app traffic, cross-checked
against the ``pyalexatodo`` / ``ha-alexa-todo-lists`` clients):

* ``POST /alexashoppinglists/api/v2/lists/fetch`` — body ``{}`` →
  ``{"listInfoList": [{"listId", "listType": "SHOP|TODO|CUSTOM", "listName"}]}``
* ``POST /alexashoppinglists/api/v2/lists/<id>/items/fetch?limit=N`` — body
  ``{}`` (or ``{"nextToken": t}``) → ``{"itemInfoList": [{"itemId",
  "itemStatus": "ACTIVE|COMPLETE", "itemName", "version"}], "nextToken"}``
* ``POST /alexashoppinglists/api/v2/lists/<id>/items`` — body
  ``{"items": [{"itemType": "KEYWORD", "itemName": n}]}``
* ``PUT /alexashoppinglists/api/v2/lists/<id>/items/<itemId>?version=V`` —
  body ``{"itemAttributesToUpdate": [{"type": "itemStatus"|"itemName",
  "value": v}], "itemAttributesToRemove": []}`` (check/uncheck/rename)
* ``DELETE /alexashoppinglists/api/v2/lists/<id>/items/<itemId>?version=V``

Two API traits that shape the module (both baked in below):

* **Version-gated writes.** Every item carries a monotone ``version``; the
  PUT/DELETE URL must send the version *as read*, so an edit always goes
  item → (id, version) straight from a fresh items fetch — never from a stale
  cached row.
* **The writes answer 200 with no useful body** — like the kids writes, the
  only truth is a **re-read**: add re-reads page 1 looking for the new name,
  check/uncheck/rename re-read and pin the item's status/name, delete
  re-reads and reports the item's absence.

Status words the CLI accepts: ``active`` / ``complete`` (``complete`` also
matches ``completed``/``done``); anything else is refused BEFORE login.
"""

from __future__ import annotations

import json
from typing import Any

#: The lists host is ``www`` (NOT ``alexa``) — see module docstring.
_SUBDOMAIN = "www"

_LISTS_PATH = "/alexashoppinglists/api/v2/lists/fetch"
_ITEMS_FETCH_PATH = "/alexashoppinglists/api/v2/lists/{list_id}/items/fetch"
_ITEMS_PATH = "/alexashoppinglists/api/v2/lists/{list_id}/items"
_ITEM_PATH = "/alexashoppinglists/api/v2/lists/{list_id}/items/{item_id}"

#: Per-page limit for the items fetch — 100 is Amazon's documented maximum.
MAX_PAGE_LIMIT = 100

# ── pure helpers ───────────────────────────────────────────────────────────


def normalize_list_type(value: Any) -> str:
    """``SHOP``/``TODO``/``CUSTOM`` (or junk) → a display word (pure)."""
    v = (value or "").strip().upper() if isinstance(value, str) else ""
    return {"SHOP": "shopping", "TODO": "to-do", "CUSTOM": "custom"}.get(v, v.lower() or "unknown")


def list_name(record: dict[str, Any]) -> str:
    """Display name of a raw list record — custom name, else the type word."""
    name = (record.get("listName") or "").strip()
    return name or normalize_list_type(record.get("listType")).capitalize()


def list_rows(payload: Any) -> list[dict[str, Any]]:
    """Flatten ``lists/fetch`` output into display rows (pure).

    Accepts either the raw record list (``fetch_lists``) or the full response
    payload (``{"listInfoList": [...]}``).
    """
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        records = payload.get("listInfoList")
    else:
        records = None
    out: list[dict[str, Any]] = []
    for rec in records or []:
        if not isinstance(rec, dict):
            continue
        out.append(
            {
                "id": rec.get("listId"),
                "name": list_name(rec),
                "type": normalize_list_type(rec.get("listType")),
            }
        )
    return out


def normalize_name(name: str) -> str:
    """Normalize a list/item name for case/space-insensitive match (pure)."""
    if not name:
        return ""
    return " ".join(str(name).strip().lower().split())


def find_list(rows: list[dict[str, Any]], name_or_id: str) -> dict[str, Any] | None:
    """Resolve a list by exact id, then by name (case-insensitive).

    Returns ``None`` when nothing matches; raises ``ValueError`` when several
    lists share the name (custom lists are user-named — ambiguity is real).
    """
    if not name_or_id:
        return None
    for row in rows or []:
        if row.get("id") == name_or_id:
            return row
    matches = [r for r in rows or [] if normalize_name(r.get("name")) == normalize_name(name_or_id)]
    if not matches:
        return None
    if len(matches) > 1:
        ids = ", ".join(sorted(str(m.get("id")) for m in matches))
        raise ValueError(
            f"{len(matches)} lists are named {name_or_id!r} — pass the list id instead ({ids})"
        )
    return matches[0]


def normalize_status(value: Any) -> str:
    """``ACTIVE``/``COMPLETE`` (or junk) → a display word (pure)."""
    v = (value or "").strip().upper() if isinstance(value, str) else ""
    return {"ACTIVE": "active", "COMPLETE": "complete"}.get(v, v.lower() or "unknown")


STATUS_WORDS = {
    "active": "ACTIVE",
    "complete": "COMPLETE",
    "completed": "COMPLETE",
    "done": "COMPLETE",
}


def normalize_status_word(word: str) -> str:
    """CLI status filter word → the API's ``itemStatus`` value (pure).

    Raises ``ValueError`` for anything but active/complete(completed/done).
    """
    v = STATUS_WORDS.get((word or "").strip().lower())
    if not v:
        raise ValueError("status must be 'active' or 'complete'")
    return v


def item_rows(payload: Any) -> list[dict[str, Any]]:
    """Flatten an items-fetch response into display rows (pure).

    ``version`` is kept on the row because the write URLs are version-gated —
    it is the field every edit depends on, so it must not be dropped.
    """
    records = (payload or {}).get("itemInfoList") if isinstance(payload, dict) else None
    out: list[dict[str, Any]] = []
    for rec in records or []:
        if not isinstance(rec, dict):
            continue
        status = normalize_status(rec.get("itemStatus"))
        out.append(
            {
                "id": rec.get("itemId"),
                "name": rec.get("itemName"),
                "status": status,
                "checked": status == "complete",
                "version": rec.get("version"),
            }
        )
    return out


def resolve_item(items: list[dict[str, Any]], name_or_id: str) -> dict[str, Any] | None:
    """Resolve a list item by exact id, then by name (case-insensitive).

    Same contract as :func:`find_list`: ``None`` when absent, ``ValueError``
    when several items share the name.
    """
    if not name_or_id:
        return None
    for row in items or []:
        if row.get("id") == name_or_id:
            return row
    matches = [
        r for r in items or [] if normalize_name(r.get("name")) == normalize_name(name_or_id)
    ]
    if not matches:
        return None
    if len(matches) > 1:
        raise ValueError(
            f"{len(matches)} items are named {name_or_id!r} — pass the item id instead"
        )
    return matches[0]


def filter_items(
    rows: list[dict[str, Any]], status: str | None = None, contains: str | None = None
) -> list[dict[str, Any]]:
    """Apply the CLI's ``--status`` / ``--contains`` filters (pure)."""
    out = rows or []
    if status:
        want = normalize_status_word(status)
        out = [r for r in out if (r.get("status") or "").upper() == want]
    if contains:
        needle = normalize_name(contains)
        out = [r for r in out if needle in normalize_name(r.get("name"))]
    return out


def build_add_payload(names: list[str]) -> dict[str, Any]:
    """Body for the add-items POST: ``KEYWORD`` items (pure)."""
    return {"items": [{"itemType": "KEYWORD", "itemName": n} for n in (names or []) if n]}


def build_attributes_update(
    checked: bool | None = None, new_name: str | None = None
) -> dict[str, Any]:
    """Body for the item PUT (check/uncheck and/or rename) (pure).

    Raises ``ValueError`` when called with nothing to change — Amazon rejects
    an empty ``itemAttributesToUpdate``.
    """
    attrs: list[dict[str, str]] = []
    if checked is not None:
        attrs.append({"type": "itemStatus", "value": "COMPLETE" if checked else "ACTIVE"})
    if new_name is not None:
        attrs.append({"type": "itemName", "value": new_name})
    if not attrs:
        raise ValueError("nothing to change — pass checked and/or new_name")
    return {"itemAttributesToUpdate": attrs, "itemAttributesToRemove": []}


def add_verify_summary(names: list[str], items: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare added names against a fresh items page (pure).

    Returns ``{name: "active"|"complete"|None}`` per requested name — ``None``
    means "not on page 1 (yet)", which is deliberately NOT collapsed into
    "failed" (a busy list paginates; the app itself shows the item anyway).
    """
    by_name = {}
    for row in items or []:
        key = normalize_name(row.get("name"))
        if key and key not in by_name:
            by_name[key] = row.get("status")
    return {n: by_name.get(normalize_name(n)) for n in names or []}


# ── network (AlexaAPI._static_request on the www host) ─────────────────────


async def _list_request(
    login,
    method: str,
    path: str,
    data: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """POST/PUT/DELETE a JSON body to the lists host via alexapy's helper.

    Reuses ``AlexaAPI._static_request`` with ``sub_domain="www"`` so auth,
    headers and the 401-retry stay correct. Raises ``RuntimeError`` when the
    response is missing or not JSON (the v2 API answers errors as HTML).
    """
    from alexapy import AlexaAPI

    resp = await AlexaAPI._static_request(
        method, login, path, data=data, query=query, sub_domain=_SUBDOMAIN
    )
    if resp is None:
        raise RuntimeError(f"the lists API returned no response for {method} {path}")
    text = await resp.text()
    try:
        body = json.loads(text)
    except (TypeError, ValueError):
        raise RuntimeError(
            f"the lists API did not answer JSON for {method} {path} "
            f"(status {resp.status}): {text[:200]!r}"
        ) from None
    if not isinstance(body, dict):
        raise RuntimeError(f"unexpected lists API answer for {method} {path}: {text[:200]!r}")
    return body


async def fetch_lists(login) -> list[dict[str, Any]]:
    """Raw list records (shopping / to-do / custom) — ``lists/fetch``."""
    body = await _list_request(login, "post", _LISTS_PATH, data={})
    return list(body.get("listInfoList") or [])


async def list_lists(login) -> list[dict[str, Any]]:
    """Display rows for every list on the account."""
    return list_rows(await fetch_lists(login))


async def fetch_list_items(
    login, list_id: str, limit: int = MAX_PAGE_LIMIT, next_token: str | None = None
) -> dict[str, Any]:
    """One page of raw item records (``items/fetch``), verbatim payload."""
    limit = max(1, min(int(limit or MAX_PAGE_LIMIT), MAX_PAGE_LIMIT))
    data: dict[str, Any] = {"nextToken": next_token} if next_token else {}
    body = await _list_request(
        login,
        "post",
        _ITEMS_FETCH_PATH.format(list_id=list_id),
        data=data,
        query={"limit": limit},
    )
    return {"itemInfoList": body.get("itemInfoList") or [], "nextToken": body.get("nextToken")}


async def fetch_items_pageable(
    login, list_id: str, limit: int = MAX_PAGE_LIMIT, pages: int = 1
) -> tuple[list[dict[str, Any]], str | None]:
    """Up to ``pages`` pages of items, following ``nextToken``.

    Returns ``(raw_records, next_token)`` — the token lets the caller know
    more pages exist without spending them.
    """
    records: list[dict[str, Any]] = []
    token: str | None = None
    for _ in range(max(1, int(pages or 1))):
        page = await fetch_list_items(login, list_id, limit=limit, next_token=token)
        records.extend(page["itemInfoList"])
        token = page["nextToken"]
        if not token:
            break
    return records, token


async def list_items(
    login,
    list_ref: str,
    limit: int = MAX_PAGE_LIMIT,
    pages: int = 1,
    status: str | None = None,
    contains: str | None = None,
) -> dict[str, Any]:
    """Resolve the list, read items (paged), flatten + filter.

    ``list_ref`` is a list id or name (ambiguity-aware). Raises ``ValueError``
    for an unknown list.
    """
    rows = list_rows(await fetch_lists(login))
    lst = find_list(rows, list_ref)
    if not lst:
        raise ValueError(f"no Alexa list matching {list_ref!r}")
    raw, next_token = await fetch_items_pageable(login, lst["id"], limit=limit, pages=pages)
    return {
        "list": lst["name"],
        "listId": lst["id"],
        "items": filter_items(item_rows({"itemInfoList": raw}), status=status, contains=contains),
        "nextToken": next_token,
    }


async def add_items(login, list_id: str, names: list[str]) -> dict[str, Any]:
    """Add items to a list, then re-read page 1 to report what landed.

    Returns ``{"added": names, "found": {name: status|None}}`` — a ``None``
    status means "added, not yet visible on page 1" (see add_verify_summary).
    """
    payload = build_add_payload(names)
    await _list_request(login, "post", _ITEMS_PATH.format(list_id=list_id), data=payload)
    page = await fetch_list_items(login, list_id)
    return {"added": list(names or []), "found": add_verify_summary(names, item_rows(page))}


async def update_item(
    login,
    list_id: str,
    item_id: str,
    version: int,
    checked: bool | None = None,
    new_name: str | None = None,
) -> dict[str, Any]:
    """Version-gated PUT on one item (check/uncheck and/or rename).

    Verified by re-reading the items and pinning the item's fresh
    status/name — ``ok`` is ``True``/``False`` when the re-read settles it,
    ``None`` when the item can no longer be seen on page 1 (busy-list
    pagination; never silently reported as success).
    """
    payload = build_attributes_update(checked=checked, new_name=new_name)
    await _list_request(
        login,
        "put",
        _ITEM_PATH.format(list_id=list_id, item_id=item_id),
        data=payload,
        query={"version": version},
    )
    page = await fetch_list_items(login, list_id)
    row = next((r for r in item_rows(page) if r.get("id") == item_id), None)
    ok: bool | None = None
    if row is not None:
        ok = True
        if checked is not None:
            ok = ok and row["checked"] == checked
        if new_name is not None:
            ok = ok and row.get("name") == new_name
    return {"itemId": item_id, "version": version, "ok": ok, "row": row}


async def delete_item(login, list_id: str, item_id: str, version: int) -> dict[str, Any]:
    """Version-gated DELETE on one item; verified by absence on re-read."""
    await _list_request(
        login,
        "delete",
        _ITEM_PATH.format(list_id=list_id, item_id=item_id),
        data={},
        query={"version": version},
    )
    page = await fetch_list_items(login, list_id)
    gone = all(r.get("id") != item_id for r in item_rows(page))
    return {"itemId": item_id, "deleted": True, "verified": gone}


# ── high-level operations the CLI drives (resolve → act → verify) ──────────


async def add_to_list(login, list_ref: str, names: list[str]) -> dict[str, Any]:
    """Resolve the list by id/name, add the items, verify on re-read."""
    rows = list_rows(await fetch_lists(login))
    lst = find_list(rows, list_ref)
    if not lst:
        raise ValueError(f"no Alexa list matching {list_ref!r}")
    out = await add_items(login, lst["id"], names)
    return {"list": lst["name"], "listId": lst["id"], **out}


async def set_checked(login, list_ref: str, item_ref: str, checked: bool) -> dict[str, Any]:
    """Check/uncheck an item (by id or name) — version read fresh, then PUT."""
    rows = list_rows(await fetch_lists(login))
    lst = find_list(rows, list_ref)
    if not lst:
        raise ValueError(f"no Alexa list matching {list_ref!r}")
    raw, _ = await fetch_items_pageable(login, lst["id"])
    items = item_rows({"itemInfoList": raw})
    item = resolve_item(items, item_ref)
    if not item:
        raise ValueError(f"no item matching {item_ref!r} in {lst['name']!r}")
    out = await update_item(login, lst["id"], item["id"], item["version"], checked=checked)
    return {
        "list": lst["name"],
        "listId": lst["id"],
        "item": item["name"],
        **out,
    }


async def rename_item(login, list_ref: str, item_ref: str, new_name: str) -> dict[str, Any]:
    """Rename an item (by id or name) — version read fresh, then PUT."""
    rows = list_rows(await fetch_lists(login))
    lst = find_list(rows, list_ref)
    if not lst:
        raise ValueError(f"no Alexa list matching {list_ref!r}")
    raw, _ = await fetch_items_pageable(login, lst["id"])
    items = item_rows({"itemInfoList": raw})
    item = resolve_item(items, item_ref)
    if not item:
        raise ValueError(f"no item matching {item_ref!r} in {lst['name']!r}")
    out = await update_item(login, lst["id"], item["id"], item["version"], new_name=new_name)
    return {"list": lst["name"], "listId": lst["id"], "item": item["name"], **out}


async def remove_item(login, list_ref: str, item_ref: str) -> dict[str, Any]:
    """Delete an item (by id or name) — version read fresh, then DELETE."""
    rows = list_rows(await fetch_lists(login))
    lst = find_list(rows, list_ref)
    if not lst:
        raise ValueError(f"no Alexa list matching {list_ref!r}")
    raw, _ = await fetch_items_pageable(login, lst["id"])
    items = item_rows({"itemInfoList": raw})
    item = resolve_item(items, item_ref)
    if not item:
        raise ValueError(f"no item matching {item_ref!r} in {lst['name']!r}")
    out = await delete_item(login, lst["id"], item["id"], item["version"])
    return {"list": lst["name"], "listId": lst["id"], "item": item["name"], **out}
