"""Behavioural tests for core/lists.py (shopping & to-do lists).

The pure layer (list/item row flattening, list + item resolution with
ambiguity refusal, add/update payload builders, filtering, the add-verify
summary) is tested directly.  The network wrappers run against a fake
``AlexaAPI._static_request`` so the two rules that matter are pinned without
an account:

* every call rides ``_static_request(..., sub_domain="www")`` — the lists API
  lives on the www host, not the alexa host (the groups.py lesson, again);
* the writes return no usable body, so each is **verified by re-reading** and
  reports ``ok``/``verified`` three-valuedly (True/False/None when the item
  has left page 1).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_anything.alexa.core import lists


def _run(coro):
    return asyncio.run(coro)


def _mock_response(body_dict, status=200):
    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value=json.dumps(body_dict))
    return resp


LISTS_PAYLOAD = {
    "listInfoList": [
        {"listId": "amzn1.list.SHOP.1", "listType": "SHOP", "listName": ""},
        {"listId": "amzn1.list.TODO.1", "listType": "TODO", "listName": ""},
        {"listId": "amzn1.list.CUSTOM.9", "listType": "CUSTOM", "listName": "Weekend"},
    ]
}

ITEMS_PAYLOAD = {
    "itemInfoList": [
        {"itemId": "it-1", "itemName": "Milk", "itemStatus": "ACTIVE", "version": 3},
        {"itemId": "it-2", "itemName": "Eggs", "itemStatus": "COMPLETE", "version": 1},
    ],
    "nextToken": None,
}


@contextlib.contextmanager
def _capture_request(body=ITEMS_PAYLOAD):
    """Patch _static_request; return (mock, captured-calls list)."""
    calls = []

    async def fake(method, login, path, data=None, query=None, sub_domain=None):
        calls.append(
            {"method": method, "path": path, "data": data, "query": query, "sub": sub_domain}
        )
        return _mock_response(body)

    with patch("alexapy.AlexaAPI._static_request", new=fake) as mock:
        yield mock, calls


# ── list_rows / list_name ────────────────────────────────────────────────


def test_list_rows_flattens_with_display_names():
    rows = lists.list_rows(LISTS_PAYLOAD)
    assert rows == [
        {"id": "amzn1.list.SHOP.1", "name": "Shopping", "type": "shopping"},
        {"id": "amzn1.list.TODO.1", "name": "To-do", "type": "to-do"},
        {"id": "amzn1.list.CUSTOM.9", "name": "Weekend", "type": "custom"},
    ]


@pytest.mark.parametrize("payload", [None, {}, {"listInfoList": [None, "junk", 3]}])
def test_list_rows_survives_junk(payload):
    assert lists.list_rows(payload) == []


def test_normalize_list_type_vocab():
    assert lists.normalize_list_type("SHOP") == "shopping"
    assert lists.normalize_list_type("todo") == "to-do"
    assert lists.normalize_list_type("CUSTOM") == "custom"
    assert lists.normalize_list_type("odd") == "odd"
    assert lists.normalize_list_type(None) == "unknown"


# ── find_list ────────────────────────────────────────────────────────────


def test_find_list_by_exact_id_then_by_name_case_insensitive():
    rows = lists.list_rows(LISTS_PAYLOAD)
    assert lists.find_list(rows, "amzn1.list.TODO.1")["name"] == "To-do"
    assert lists.find_list(rows, "weekend")["id"] == "amzn1.list.CUSTOM.9"


def test_find_list_not_found_returns_none():
    assert lists.find_list(lists.list_rows(LISTS_PAYLOAD), "nope") is None


def test_find_list_ambiguous_refuses():
    rows = [
        {"id": "a", "name": "Groceries", "type": "custom"},
        {"id": "b", "name": "Groceries", "type": "custom"},
    ]
    with pytest.raises(ValueError, match="2 lists are named"):
        lists.find_list(rows, "Groceries")


# ── item rows / status ───────────────────────────────────────────────────


def test_item_rows_keeps_version_and_checked():
    rows = lists.item_rows(ITEMS_PAYLOAD)
    assert rows == [
        {"id": "it-1", "name": "Milk", "status": "active", "checked": False, "version": 3},
        {"id": "it-2", "name": "Eggs", "status": "complete", "checked": True, "version": 1},
    ]


@pytest.mark.parametrize("payload", [None, [], {"itemInfoList": [None, 5]}])
def test_item_rows_survives_junk(payload):
    assert lists.item_rows(payload) == []


def test_normalize_status_word_vocab():
    assert lists.normalize_status_word("active") == "ACTIVE"
    assert lists.normalize_status_word("Complete") == "COMPLETE"
    assert lists.normalize_status_word("completed") == "COMPLETE"
    assert lists.normalize_status_word("done") == "COMPLETE"
    with pytest.raises(ValueError, match="active"):
        lists.normalize_status_word("nope")


def test_filter_items_status_and_contains():
    rows = lists.item_rows(ITEMS_PAYLOAD)
    assert [r["name"] for r in lists.filter_items(rows, status="complete")] == ["Eggs"]
    assert [r["name"] for r in lists.filter_items(rows, contains="mil")] == ["Milk"]
    assert lists.filter_items(rows, contains="ZZZ") == []


# ── resolve_item ─────────────────────────────────────────────────────────


def test_resolve_item_by_id_then_name():
    rows = lists.item_rows(ITEMS_PAYLOAD)
    assert lists.resolve_item(rows, "it-2")["name"] == "Eggs"
    assert lists.resolve_item(rows, "milk")["id"] == "it-1"
    assert lists.resolve_item(rows, "absent") is None


def test_resolve_item_ambiguous_refuses():
    rows = [
        {"id": "x1", "name": "Tea", "status": "active", "checked": False, "version": 1},
        {"id": "x2", "name": "tea", "status": "active", "checked": False, "version": 2},
    ]
    with pytest.raises(ValueError, match="2 items are named"):
        lists.resolve_item(rows, "tea")


# ── payload builders ─────────────────────────────────────────────────────


def test_build_add_payload_keyword_items():
    assert lists.build_add_payload(["Milk", "Tea"]) == {
        "items": [
            {"itemType": "KEYWORD", "itemName": "Milk"},
            {"itemType": "KEYWORD", "itemName": "Tea"},
        ]
    }


def test_build_attributes_update_check_uncheck_rename():
    assert lists.build_attributes_update(checked=True) == {
        "itemAttributesToUpdate": [{"type": "itemStatus", "value": "COMPLETE"}],
        "itemAttributesToRemove": [],
    }
    assert lists.build_attributes_update(checked=False) == {
        "itemAttributesToUpdate": [{"type": "itemStatus", "value": "ACTIVE"}],
        "itemAttributesToRemove": [],
    }
    assert lists.build_attributes_update(new_name="Oat milk") == {
        "itemAttributesToUpdate": [{"type": "itemName", "value": "Oat milk"}],
        "itemAttributesToRemove": [],
    }
    both = lists.build_attributes_update(checked=True, new_name="X")
    assert [a["type"] for a in both["itemAttributesToUpdate"]] == ["itemStatus", "itemName"]


def test_build_attributes_update_nothing_to_change_refuses():
    with pytest.raises(ValueError, match="nothing to change"):
        lists.build_attributes_update()


def test_add_verify_summary_reports_active_complete_or_none():
    rows = lists.item_rows(ITEMS_PAYLOAD)
    assert lists.add_verify_summary(["Milk", "Eggs", "Tea"], rows) == {
        "Milk": "active",
        "Eggs": "complete",
        "Tea": None,
    }


# ── network: host + paths + pagination ───────────────────────────────────


def test_fetch_lists_rides_the_www_subdomain():
    with _capture_request(LISTS_PAYLOAD) as (_mock, calls):
        rows = _run(lists.list_lists(MagicMock()))
    assert rows[0]["name"] == "Shopping"
    assert calls[0]["method"] == "post"
    assert calls[0]["path"] == "/alexashoppinglists/api/v2/lists/fetch"
    assert calls[0]["data"] == {}
    assert calls[0]["sub"] == "www"


def test_fetch_list_items_sends_limit_and_returns_token():
    token_payload = dict(ITEMS_PAYLOAD, nextToken="tok-9")
    with _capture_request(token_payload) as (_mock, calls):
        page = _run(lists.fetch_list_items(MagicMock(), "L1", limit=50))
    assert page["nextToken"] == "tok-9"
    assert calls[0]["path"] == "/alexashoppinglists/api/v2/lists/L1/items/fetch"
    assert calls[0]["query"] == {"limit": 50}
    assert calls[0]["data"] == {}


def test_fetch_list_items_sends_next_token_when_following():
    with _capture_request(ITEMS_PAYLOAD) as (_mock, calls):
        _run(lists.fetch_list_items(MagicMock(), "L1", next_token="tok-9"))
    assert calls[0]["data"] == {"nextToken": "tok-9"}


def test_fetch_list_items_clamps_limit_to_amazon_maximum():
    with _capture_request(ITEMS_PAYLOAD) as (_mock, calls):
        _run(lists.fetch_list_items(MagicMock(), "L1", limit=5000))
    assert calls[0]["query"] == {"limit": lists.MAX_PAGE_LIMIT}


def test_fetch_items_pageable_follows_next_token_until_exhausted():
    page1 = dict(ITEMS_PAYLOAD, nextToken="tok-1")
    page2 = {
        "itemInfoList": [{"itemId": "it-3", "itemName": "Tea", "itemStatus": "ACTIVE", "version": 1}],
        "nextToken": None,
    }
    pages = [page1, page2]

    async def fake(method, login, path, data=None, query=None, sub_domain=None):
        return _mock_response(page2 if data.get("nextToken") else page1)

    with patch("alexapy.AlexaAPI._static_request", new=fake):
        raw, token = _run(lists.fetch_items_pageable(MagicMock(), "L1", pages=5))
    assert [r["itemId"] for r in raw] == ["it-1", "it-2", "it-3"]
    assert token is None


def test_list_items_resolves_by_name_and_filters():
    with _capture_request(LISTS_PAYLOAD):
        with patch.object(lists, "fetch_items_pageable", new=AsyncMock(return_value=(ITEMS_PAYLOAD["itemInfoList"], None))):
            out = _run(lists.list_items(MagicMock(), "shopping", status="complete"))
    assert out["list"] == "Shopping"
    assert [i["name"] for i in out["items"]] == ["Eggs"]


def test_list_items_unknown_list_refuses():
    with _capture_request(LISTS_PAYLOAD):
        with pytest.raises(ValueError, match="no Alexa list matching"):
            _run(lists.list_items(MagicMock(), "nope"))


# ── error handling: HTML / missing response ──────────────────────────────


def test_non_json_response_raises_runtime_error():
    resp = MagicMock()
    resp.status = 403
    resp.text = AsyncMock(return_value="<html>nope</html>")
    with patch("alexapy.AlexaAPI._static_request", new=AsyncMock(return_value=resp)):
        with pytest.raises(RuntimeError, match="did not answer JSON"):
            _run(lists.fetch_lists(MagicMock()))


def test_missing_response_raises_runtime_error():
    with patch("alexapy.AlexaAPI._static_request", new=AsyncMock(return_value=None)):
        with pytest.raises(RuntimeError, match="no response"):
            _run(lists.fetch_lists(MagicMock()))


# ── writes: payload + version gating + verify ────────────────────────────


def test_add_items_posts_keyword_payload_and_verifies():
    added = {"itemInfoList": ITEMS_PAYLOAD["itemInfoList"] + [
        {"itemId": "it-9", "itemName": "Tea", "itemStatus": "ACTIVE", "version": 1}
    ], "nextToken": None}
    with _capture_request(added) as (_mock, calls):
        out = _run(lists.add_items(MagicMock(), "L1", ["Tea"]))
    assert calls[0]["method"] == "post"
    assert calls[0]["path"] == "/alexashoppinglists/api/v2/lists/L1/items"
    assert calls[0]["data"] == {"items": [{"itemType": "KEYWORD", "itemName": "Tea"}]}
    assert out == {"added": ["Tea"], "found": {"Tea": "active"}}


def test_add_items_not_yet_visible_is_none_not_false():
    with _capture_request(ITEMS_PAYLOAD) as (_mock, calls):
        out = _run(lists.add_items(MagicMock(), "L1", ["Tea"]))
    assert out["found"]["Tea"] is None


def test_update_item_puts_version_gated_body_and_pins_ok():
    checked = {"itemInfoList": [dict(ITEMS_PAYLOAD["itemInfoList"][0], itemStatus="COMPLETE")], "nextToken": None}
    with _capture_request(checked) as (_mock, calls):
        out = _run(lists.update_item(MagicMock(), "L1", "it-1", 3, checked=True))
    assert calls[0]["method"] == "put"
    assert calls[0]["path"] == "/alexashoppinglists/api/v2/lists/L1/items/it-1"
    assert calls[0]["query"] == {"version": 3}
    assert calls[0]["data"] == {
        "itemAttributesToUpdate": [{"type": "itemStatus", "value": "COMPLETE"}],
        "itemAttributesToRemove": [],
    }
    assert out["ok"] is True


def test_update_item_reports_false_when_re_read_disagrees():
    with _capture_request(ITEMS_PAYLOAD) as (_mock, _calls):
        out = _run(lists.update_item(MagicMock(), "L1", "it-1", 3, checked=True))
    assert out["ok"] is False


def test_update_item_ok_none_when_item_left_page1():
    with _capture_request({"itemInfoList": [], "nextToken": None}):
        out = _run(lists.update_item(MagicMock(), "L1", "it-1", 3, checked=True))
    assert out["ok"] is None


def test_delete_item_verifies_absence():
    with _capture_request({"itemInfoList": [ITEMS_PAYLOAD["itemInfoList"][1]], "nextToken": None}) as (_mock, calls):
        out = _run(lists.delete_item(MagicMock(), "L1", "it-1", 3))
    assert calls[0]["method"] == "delete"
    assert calls[0]["query"] == {"version": 3}
    assert out == {"itemId": "it-1", "deleted": True, "verified": True}


def test_delete_item_reports_false_when_still_present():
    with _capture_request(ITEMS_PAYLOAD):
        out = _run(lists.delete_item(MagicMock(), "L1", "it-1", 3))
    assert out["verified"] is False


# ── high-level: resolve → act → verify ───────────────────────────────────


def test_add_to_list_resolves_then_verifies():
    added = {"itemInfoList": ITEMS_PAYLOAD["itemInfoList"] + [
        {"itemId": "it-9", "itemName": "Tea", "itemStatus": "ACTIVE", "version": 1}
    ], "nextToken": None}

    async def dispatch(method, login, path, data=None, query=None, sub_domain=None):
        if path.endswith("/lists/fetch"):
            return _mock_response(LISTS_PAYLOAD)
        if path.endswith("/items/fetch"):
            return _mock_response(added)
        return _mock_response({})

    with patch("alexapy.AlexaAPI._static_request", new=dispatch):
        out = _run(lists.add_to_list(MagicMock(), "weekend", ["Tea"]))
    assert out["list"] == "Weekend"
    assert out["found"] == {"Tea": "active"}


def test_set_checked_reads_fresh_version_then_puts():
    checked = {"itemInfoList": [
        dict(ITEMS_PAYLOAD["itemInfoList"][0], itemStatus="COMPLETE")
    ], "nextToken": None}
    # call order: lists/fetch, items/fetch (version read), PUT, items/fetch (verify)
    script = [
        LISTS_PAYLOAD,
        ITEMS_PAYLOAD,
        checked,
        checked,
    ]

    async def dispatch(method, login, path, data=None, query=None, sub_domain=None):
        assert method in ("post", "put")
        return _mock_response(script.pop(0))

    with patch("alexapy.AlexaAPI._static_request", new=dispatch):
        out = _run(lists.set_checked(MagicMock(), "shopping", "milk", True))
    assert out["item"] == "Milk"
    assert out["ok"] is True


def test_high_level_unknown_item_refuses():
    async def dispatch(method, login, path, data=None, query=None, sub_domain=None):
        if path.endswith("/lists/fetch"):
            return _mock_response(LISTS_PAYLOAD)
        return _mock_response(ITEMS_PAYLOAD)

    with patch("alexapy.AlexaAPI._static_request", new=dispatch):
        with pytest.raises(ValueError, match="no item matching"):
            _run(lists.set_checked(MagicMock(), "shopping", "absent", True))
        with pytest.raises(ValueError, match="no item matching"):
            _run(lists.rename_item(MagicMock(), "shopping", "absent", "X"))
        with pytest.raises(ValueError, match="no item matching"):
            _run(lists.remove_item(MagicMock(), "shopping", "absent"))


def test_high_level_unknown_list_refuses():
    with _capture_request(LISTS_PAYLOAD):
        with pytest.raises(ValueError, match="no Alexa list matching"):
            _run(lists.set_checked(MagicMock(), "nope", "milk", True))
