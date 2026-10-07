"""groups add/remove: one id per call, then verify by re-reading the group.

Live 2026-10-07: a REMOVE carrying five member ids removed only the first and
still answered success, so multi-id deltas must be split and verified.
"""
import asyncio

from cli_anything.alexa.core import groups as G


def test_split_add_remove_one_id_per_call():
    assert G.split_update(["a", "b"], "REMOVE") == [(["a"], []), (["b"], [])]
    assert G.split_update(["a"], "add", ["g1", "g2"]) == [(["a"], []), ([], ["g1"]), ([], ["g2"])]


def test_split_replace_and_single_stay_one_call():
    assert G.split_update(["a", "b"], "REPLACE") == [(["a", "b"], [])]
    assert G.split_update(["a"], "REMOVE") == [(["a"], [])]


def _group(members, children=()):
    return {"id": "g", "memberDevices": {"items": [{"id": m} for m in members]},
            "childDeviceGroups": [{"id": c} for c in children]}


def test_unapplied_remove_and_add():
    assert G.unapplied(_group(["a", "b"]), ["a", "c"], None, "REMOVE") == {"memberDeviceIds": ["a"], "childDeviceGroupIds": []}
    assert G.unapplied(_group(["a"]), ["a", "c"], ["k"], "ADD") == {"memberDeviceIds": ["c"], "childDeviceGroupIds": ["k"]}
    assert G.unapplied(None, ["a"], None, "ADD") == {"memberDeviceIds": ["a"], "childDeviceGroupIds": []}


def test_update_group_splits_and_reports_partial(monkeypatch):
    """The server honouring only the first id of a call is caught, not reported as success."""
    state = {"members": {"a", "b", "c"}}
    calls = []

    async def fake_graphql(login, query, variables=None):
        if "updateDeviceGroup" in query:
            inp = variables["in"]
            calls.append(list(inp["memberDeviceIds"]))
            first = inp["memberDeviceIds"][:1]            # the live quirk: only the first id lands
            if inp["memberDeviceIdsUpdateOperation"] == "REMOVE":
                state["members"] -= set(first)
            return {"data": {"updateDeviceGroup": {"__typename": "UpdateDeviceGroupsResponse"}}}
        return {"data": {"listDeviceGroups": {"deviceGroups": [_group(sorted(state["members"]))]}}}

    monkeypatch.setattr(G, "_graphql", fake_graphql)
    out = asyncio.run(G.update_group(None, "g", ["a", "b"], "REMOVE"))
    assert calls == [["a"], ["b"]]                          # split: one id per call
    assert out["verified"] and state["members"] == {"c"}

    monkeypatch.setattr(G, "split_update", lambda m, o, c=None: [(list(m), list(c or []))])   # unsplit
    state["members"] = {"a", "b", "c"}
    out = asyncio.run(G.update_group(None, "g", ["a", "b"], "REMOVE"))
    assert not out["verified"] and out["unapplied"]["memberDeviceIds"] == ["b"]
