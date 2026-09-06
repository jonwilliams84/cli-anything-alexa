"""End-to-end workflow tests for the `lists` surface.

Unlike the unit / CLI-path files, these run the REAL core module against an
in-memory fake of the v2 lists API (a faithful state machine for the four
endpoints) and drive multi-command workflows: add → items → check → items →
rename → remove.  This is where the cross-command contracts live:

* the version an edit sends is the one a fresh items read produced (and a
  stale version is REFUSED by the API);
* resolution by name works identically from every command;
* verify-by-reread semantics flow through to the reported `ok`/`verified`.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from cli_anything.alexa.core import lists


def _run(coro):
    return asyncio.run(coro)


class FakeListsAPI:
    """State machine for /alexashoppinglists/api/v2 on the www host.

    Routes on (method, path suffix) exactly like the module's calls; PUT/DELETE
    enforce the version gate (a wrong version answers non-JSON, like the real
    API's error page).
    """

    def __init__(self, lists=None, items=None, per_page=100):
        self.lists = lists or []
        self.items = items or {}  # list_id -> list of item dicts
        self.calls = []
        self.per_page = per_page

    async def __call__(self, method, login, path, data=None, query=None, sub_domain=None):
        self.calls.append((method, path, data, query, sub_domain))
        assert sub_domain == "www"
        if path == "/alexashoppinglists/api/v2/lists/fetch":
            return _resp({"listInfoList": self.lists})
        if path.endswith("/items/fetch"):
            list_id = path.split("/")[-3]
            limit = int((query or {}).get("limit", 100))
            token = (data or {}).get("nextToken")
            records = self.items.get(list_id, [])
            start = int(token) if token else 0
            page = records[start : start + limit]
            nxt = start + limit
            return _resp({
                "itemInfoList": page,
                "nextToken": str(nxt) if nxt < len(records) else None,
            })
        if path.endswith("/items") and method == "post":
            list_id = path.split("/")[-2]
            for spec in (data or {}).get("items", []):
                self.items.setdefault(list_id, []).append({
                    "itemId": f"it-{len(self.items.get(list_id, [])) + 1}",
                    "itemName": spec["itemName"],
                    "itemStatus": "ACTIVE",
                    "version": 1,
                })
            return _resp({})
        # /items/<item_id>
        list_id, item_id = path.split("/")[-3], path.split("/")[-1]
        record = next((r for r in self.items.get(list_id, []) if r["itemId"] == item_id), None)
        if record is None or (query or {}).get("version") != record["version"]:
            return _resp("<html>conflict</html>", status=409)
        if method == "put":
            for attr in (data or {}).get("itemAttributesToUpdate", []):
                if attr["type"] == "itemStatus":
                    record["itemStatus"] = attr["value"]
                    record["version"] += 1
                elif attr["type"] == "itemName":
                    record["itemName"] = attr["value"]
                    record["version"] += 1
            return _resp({})
        if method == "delete":
            self.items[list_id].remove(record)
            return _resp({})
        raise AssertionError(f"unrouted: {method} {path}")


def _resp(body, status=200):
    from unittest.mock import AsyncMock

    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value=body if isinstance(body, str) else json.dumps(body))
    return resp


SHOPPING = {"listId": "amzn1.list.SHOP.1", "listType": "SHOP", "listName": ""}
WEEKEND = {"listId": "amzn1.list.CUSTOM.9", "listType": "CUSTOM", "listName": "Weekend"}


def _api(**kw):
    return FakeListsAPI(lists=[SHOPPING, WEEKEND], **kw)


# ── the shopping-trip workflow ───────────────────────────────────────────


def test_add_check_rename_remove_round_trip():
    api = _api()
    with patch("alexapy.AlexaAPI._static_request", new=api):
        # 1. add two items
        out = _run(lists.add_to_list(MagicMock(), "Shopping", ["Tea", "Biscuits"]))
        assert out["list"] == "Shopping"
        assert out["found"] == {"Tea": "active", "Biscuits": "active"}
        # 2. items read shows them active
        page = _run(lists.list_items(MagicMock(), "shopping"))
        assert [(i["name"], i["checked"]) for i in page["items"]] == [
            ("Tea", False), ("Biscuits", False),
        ]
        # 3. check one — resolved by NAME, version read fresh
        done = _run(lists.set_checked(MagicMock(), "Shopping", "Tea", True))
        assert done["item"] == "Tea"
        assert done["ok"] is True
        # 4. the re-read agrees
        page = _run(lists.list_items(MagicMock(), "Shopping", status="complete"))
        assert [i["name"] for i in page["items"]] == ["Tea"]
        # 5. rename the other one
        renamed = _run(lists.rename_item(MagicMock(), "Shopping", "Biscuits", "Digestives"))
        assert renamed["ok"] is True
        page = _run(lists.list_items(MagicMock(), "Shopping"))
        assert [i["name"] for i in page["items"]] == ["Tea", "Digestives"]
        # 6. remove both — verified by absence
        removed = _run(lists.remove_item(MagicMock(), "Shopping", "Tea"))
        assert removed["verified"] is True
        removed = _run(lists.remove_item(MagicMock(), "Shopping", "Digestives"))
        assert removed["verified"] is True
        page = _run(lists.list_items(MagicMock(), "Shopping"))
        assert page["items"] == []


def test_stale_version_is_refused_by_the_version_gate():
    api = _api()
    with patch("alexapy.AlexaAPI._static_request", new=api):
        _run(lists.add_to_list(MagicMock(), "Shopping", ["Tea"]))
        fresh, _ = _run(lists.fetch_items_pageable(MagicMock(), "amzn1.list.SHOP.1"))
        record = fresh[0]
        # bump the version behind the caller's back (HA-style concurrent edit) —
        # in the STORE, since re-read records are deserialized copies.
        api.items["amzn1.list.SHOP.1"][0]["version"] += 1
        with pytest.raises(RuntimeError, match="did not answer JSON"):
            _run(
                lists.update_item(
                    MagicMock(), "amzn1.list.SHOP.1", record["itemId"],
                    record["version"] - 1, checked=True,
                )
            )


def test_ambiguous_list_name_refuses_and_never_writes():
    api = _api()
    api.lists.append({"listId": "amzn1.list.CUSTOM.10", "listType": "CUSTOM", "listName": "Weekend"})
    with patch("alexapy.AlexaAPI._static_request", new=api):
        with pytest.raises(ValueError, match="2 lists are named"):
            _run(lists.add_to_list(MagicMock(), "Weekend", ["Tea"]))
    writes = [c for c in api.calls if c[0] in ("put", "delete") or (c[0] == "post" and c[1].endswith("/items"))]
    assert writes == []


def test_pagination_workflow_two_pages_then_token_exhausted():
    api = _api(per_page=2)
    api.items["amzn1.list.SHOP.1"] = [
        {"itemId": f"it-{n}", "itemName": f"Item {n}", "itemStatus": "ACTIVE", "version": 1}
        for n in range(1, 6)
    ]
    with patch("alexapy.AlexaAPI._static_request", new=api):
        page = _run(lists.list_items(MagicMock(), "Shopping", limit=2))
        assert len(page["items"]) == 2
        assert page["nextToken"] == "2"
        # ask for the rest
        all_rows, _ = _run(lists.fetch_items_pageable(MagicMock(), "amzn1.list.SHOP.1", limit=2, pages=10))
        assert len(all_rows) == 5


def test_add_verify_none_when_the_list_is_longer_than_one_page():
    api = _api(per_page=100)
    # 100 existing items fill page 1 (the API's max per fetch); Tea lands as
    # item #101, off the verification page.
    api.items["amzn1.list.SHOP.1"] = [
        {"itemId": f"old-{n}", "itemName": f"Old {n}", "itemStatus": "ACTIVE", "version": 1}
        for n in range(100)
    ]
    with patch("alexapy.AlexaAPI._static_request", new=api):
        out = _run(lists.add_to_list(MagicMock(), "Shopping", ["Tea"]))
        # the verify must report None ("added, not visible on page 1 yet"),
        # not success and not failure.
        assert out["found"] == {"Tea": None}
