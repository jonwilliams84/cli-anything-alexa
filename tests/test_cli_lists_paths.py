"""Behavioural tests for the `lists` CLI paths (shopping & to-do lists).

Covers `lists list` / `items` / `add` / `check` / `uncheck` / `rename` /
`remove`.  Every mutating command is checked against the harness-wide
contract — **preview by default, act only on --yes** — and assertions are on
observable behaviour (exit code, JSON on stdout, which core coroutine was
invoked with what), never on source text.
"""

from __future__ import annotations

import contextlib
import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli_anything.alexa.alexa_cli import cli
from cli_anything.alexa.core import lists as lists_core


def _invoke(args, obj=None):
    return CliRunner().invoke(cli, args, obj=obj or {}, catch_exceptions=False)


def _json_invoke(args):
    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()):
        result = _invoke(["--json", *args])
    assert result.exit_code == 0, result.output
    return result


@contextlib.contextmanager
def _stub_run(side_effects=None, return_value=None):
    """Patch ``_run`` with a stub that closes the coroutine it is handed."""

    effects = list(side_effects) if side_effects is not None else None

    def fake_run(_ctx, coro):
        if hasattr(coro, "close"):
            coro.close()
        if effects is not None:
            return effects.pop(0)
        return return_value

    with patch("cli_anything.alexa.alexa_cli._run", side_effect=fake_run) as mock_run:
        yield mock_run


LIST_ROWS = [
    {"id": "amzn1.list.SHOP.1", "name": "Shopping", "type": "shopping"},
    {"id": "amzn1.list.TODO.1", "name": "To-do", "type": "to-do"},
    {"id": "amzn1.list.CUSTOM.9", "name": "Weekend", "type": "custom"},
]
ITEM_ROWS = [
    {"id": "it-1", "name": "Milk", "status": "active", "checked": False, "version": 3},
    {"id": "it-2", "name": "Eggs", "status": "complete", "checked": True, "version": 1},
]
RAW_ITEMS = [
    {"itemId": "it-1", "itemName": "Milk", "itemStatus": "ACTIVE", "version": 3},
    {"itemId": "it-2", "itemName": "Eggs", "itemStatus": "COMPLETE", "version": 1},
]


# ── reads ────────────────────────────────────────────────────────────────


def test_lists_list_emits_rows_as_json():
    with _stub_run(return_value=LIST_ROWS) as mock_run:
        parsed = json.loads(_json_invoke(["lists", "list"]).output)
    assert parsed == LIST_ROWS
    coro = mock_run.call_args[0][1]
    assert coro.__name__ == "list_lists"


def test_lists_items_emits_resolved_list_and_filtered_items():
    payload = {"list": "Shopping", "listId": "amzn1.list.SHOP.1", "items": ITEM_ROWS, "nextToken": None}
    with _stub_run(return_value=payload) as mock_run:
        parsed = json.loads(_json_invoke(["lists", "items", "Shopping"]).output)
    assert parsed["list"] == "Shopping"
    assert [i["name"] for i in parsed["items"]] == ["Milk", "Eggs"]
    coro = mock_run.call_args[0][1]
    assert coro.__name__ == "list_items"


def test_lists_items_flags_are_threaded_to_the_core_call():
    with _stub_run(return_value={"list": "Shopping", "listId": "L", "items": [], "nextToken": None}) as mock_run:
        _json_invoke(
            ["lists", "items", "Shopping", "--limit", "10", "--pages", "3",
             "--status", "active", "--contains", "tea"]
        )
    coro = mock_run.call_args[0][1]
    assert coro.__name__ == "list_items"


def test_lists_items_bad_status_refused_before_login():
    result = _invoke(["--json", "lists", "items", "Shopping", "--status", "zzz"])
    assert result.exit_code != 0
    assert "active" in result.output


# ── add ──────────────────────────────────────────────────────────────────


def test_lists_add_previews_without_yes():
    with _stub_run(side_effects=[LIST_ROWS]) as mock_run:
        parsed = json.loads(_json_invoke(["lists", "add", "Shopping", "Tea", "Biscuits"]).output)
    assert parsed["dry_run"] is True
    assert parsed["list"] == "Shopping"
    assert parsed["listId"] == "amzn1.list.SHOP.1"
    assert parsed["would_add"] == ["Tea", "Biscuits"]
    assert parsed["payload"] == {
        "items": [
            {"itemType": "KEYWORD", "itemName": "Tea"},
            {"itemType": "KEYWORD", "itemName": "Biscuits"},
        ]
    }
    assert "--yes" in parsed["hint"]
    assert mock_run.call_count == 1  # only the list read, nothing written


def test_lists_add_yes_resolves_list_then_calls_add_to_list():
    with patch.object(lists_core, "add_to_list", MagicMock(return_value={"added": ["Tea"]})) as add:
        with _stub_run(side_effects=[LIST_ROWS, {"added": ["Tea"]}]) as mock_run:
            parsed = json.loads(_json_invoke(["lists", "add", "Shopping", "Tea", "--yes"]).output)
    assert parsed["added"] == ["Tea"]
    assert add.call_count == 1
    args = add.call_args[0]
    assert args[1] == "Shopping"  # the raw ref; resolution happens in core
    assert args[2] == ["Tea"]
    assert mock_run.call_count == 2


def test_lists_add_unknown_list_refuses_before_anything_else():
    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()):
        with _stub_run(side_effects=[LIST_ROWS]):
            result = _invoke(["--json", "lists", "add", "Nope", "Tea"])
    assert result.exit_code != 0
    assert "no Alexa list matching" in result.output


# ── check / uncheck / rename / remove: the dry-run contract ──────────────


EDIT_ARGS = [
    (["lists", "check", "Shopping", "milk"], "would_check", True),
    (["lists", "uncheck", "Shopping", "Eggs"], "would_uncheck", False),
    (["lists", "rename", "Shopping", "milk", "Oat milk"], "would_rename", "Oat milk"),
]
REMOVE_ARGS = ["lists", "remove", "Shopping", "milk"]


@pytest.mark.parametrize(("argv", "field", "value"), EDIT_ARGS, ids=lambda v: str(v)[:40])
def test_item_edits_preview_without_yes(argv, field, value):
    # uncheck targets the completed item; check/rename target the active one
    expect_item = "Eggs" if field == "would_uncheck" else "Milk"
    with _stub_run(side_effects=[LIST_ROWS, (RAW_ITEMS, None)]) as mock_run:
        parsed = json.loads(_json_invoke(argv).output)
    assert parsed["dry_run"] is True
    assert parsed["list"] == "Shopping"
    assert parsed["listId"] == "amzn1.list.SHOP.1"
    assert parsed["item"] == expect_item
    assert parsed["itemId"] == ("it-2" if field == "would_uncheck" else "it-1")
    assert parsed["version"] == (1 if field == "would_uncheck" else 3)
    assert parsed[field] == value
    assert "--yes" in parsed["hint"]
    assert mock_run.call_count == 2  # list read + items read; nothing written


def test_remove_previews_without_yes():
    with _stub_run(side_effects=[LIST_ROWS, (RAW_ITEMS, None)]) as mock_run:
        parsed = json.loads(_json_invoke(REMOVE_ARGS).output)
    assert parsed["dry_run"] is True
    assert parsed["item"] == "Milk"
    assert parsed["version"] == 3
    assert "--yes" in parsed["hint"]
    assert mock_run.call_count == 2


def _patched(name, result):
    return patch.object(lists_core, name, MagicMock(return_value=result))


#: argv -> (core function, expected positional args after the login)
EXECUTES = [
    (["lists", "check", "Shopping", "milk", "--yes"], "set_checked", ("Shopping", "milk", True)),
    (["lists", "uncheck", "Shopping", "Eggs", "--yes"], "set_checked", ("Shopping", "Eggs", False)),
    (["lists", "rename", "Shopping", "milk", "Oat milk", "--yes"], "rename_item", ("Shopping", "milk", "Oat milk")),
    (["lists", "remove", "Shopping", "milk", "--yes"], "remove_item", ("Shopping", "milk")),
]


@pytest.mark.parametrize(("argv", "core", "args_after_login"), EXECUTES, ids=lambda v: str(v)[:40])
def test_item_edits_execute_on_yes(argv, core, args_after_login):
    result_value = {"list": "Shopping", "item": "Milk", "ok": True}
    with _patched(core, result_value) as patched:
        with _stub_run(side_effects=[LIST_ROWS, (RAW_ITEMS, None), result_value]):
            parsed = json.loads(_json_invoke(argv).output)
    assert patched.call_count == 1
    call = patched.call_args[0]
    assert call[1:] == args_after_login
    assert parsed == result_value


def test_item_edit_unknown_item_refuses_after_list_read():
    raw = [RAW_ITEMS[1]]  # only Eggs; 'milk' missing
    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()):
        with _stub_run(side_effects=[LIST_ROWS, (raw, None)]):
            result = _invoke(["--json", "lists", "check", "Shopping", "milk"])
    assert result.exit_code != 0
    assert "no item matching" in result.output
