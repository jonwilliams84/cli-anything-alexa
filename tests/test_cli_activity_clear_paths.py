"""Behavioural tests for the selective `activity clear` CLI paths.

`activity clear` has three modes: the unchanged bulk `--items N` delete of the
most-recent block, a filter-based selective delete (`--device` / `--contains`
over the fetched legacy rows) and an explicit `--ids` delete.  `activity
records` gained the same two filter options so the ids a selective clear will
delete can be listed first.

Assertions are on observable behaviour — exit code, JSON on stdout, the
requests the fake `AlexaAPI` saw — never on source text.  Every destructive
run is checked against the harness-wide contract: **preview by default, act
only on --yes**, and the executed run deletes the exact ids the preview
advertised.  The workflow tests run real core code against the fake, so a
preview and a `--yes` run of the same arguments provably select the same ids.
"""

from __future__ import annotations

import contextlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from cli_anything.alexa.alexa_cli import cli
from cli_anything.alexa.core import activity as activity_core


def _invoke(args, obj=None):
    return CliRunner().invoke(cli, args, obj=obj or {}, catch_exceptions=False)


def _json_out(result):
    return json.loads(result.output)


_LEGACY_PAYLOAD = {
    "activities": [
        {
            "id": "act-1",
            "creationTimestamp": 1_750_000_000_000,
            "activityStatus": "SUCCESS",
            "sourceDeviceIds": [{"serialNumber": "SN1", "deviceType": "ECHO"}],
            "description": json.dumps({"summary": "turn on the lights"}),
        },
        {
            "id": "act-2",
            "creationTimestamp": 1_750_000_001_000,
            "activityStatus": "SUCCESS",
            "sourceDeviceIds": [{"serialNumber": "SN2", "deviceType": "ECHO"}],
            "description": json.dumps({"summary": "set a timer"}),
        },
        {
            "id": "act-3",
            "creationTimestamp": 1_750_000_002_000,
            "activityStatus": "SUCCESS",
            "sourceDeviceIds": [{"serialNumber": "SN1", "deviceType": "ECHO"}],
            "description": json.dumps({"summary": "set another timer"}),
        },
        {
            # a record with no description/serial at all survives, id-less-free
            "id": "act-4",
            "creationTimestamp": 1_750_000_003_000,
            "activityStatus": "SUCCESS",
            "sourceDeviceIds": [],
            "description": None,
        },
    ]
}

_DEVICES = [
    {"serialNumber": "SN1", "accountName": "Kitchen Echo"},
    {"serialNumber": "SN2", "accountName": "Study Dot"},
]


def _fake_alexa(delete_answers=(), activities=_LEGACY_PAYLOAD, devices=_DEVICES):
    """Fake AlexaAPI: get_devices/get_activities as canned, DELETEs answered."""
    fake_cls = MagicMock()
    fake_cls.get_devices = AsyncMock(return_value=devices)
    fake_cls.get_activities = AsyncMock(return_value=activities)
    answers = list(delete_answers)
    calls = []

    async def static_request(method, login, path, *a, **kw):
        calls.append((method, path))
        answer = answers.pop(0) if answers else None
        return SimpleNamespace(status=answer) if answer is not None else None

    fake_cls._static_request = AsyncMock(side_effect=static_request)
    fake_cls.calls = calls
    return fake_cls


@contextlib.contextmanager
def _stub_login():
    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()) as login:
        yield login


# ── the bulk path is unchanged ───────────────────────────────────────────


def test_bulk_preview_is_unchanged_and_needs_no_fetch():
    fake_cls = _fake_alexa()
    with _stub_login():
        result = _invoke(["--json", "activity", "clear"])
    assert result.exit_code == 0
    fake_cls.get_activities.assert_not_awaited()
    parsed = _json_out(result)
    assert parsed["dry_run"] is True
    assert parsed["would_delete"] == 50  # the documented default
    assert parsed["irreversible"] is True
    assert "--yes" in parsed["hint"]


def test_bulk_yes_routes_to_the_bulk_delete():
    fake_cls = _fake_alexa()
    fake_cls.clear_history = AsyncMock(return_value=True)
    with _stub_login(), patch("alexapy.AlexaAPI", fake_cls):
        result = _invoke(["--json", "activity", "clear", "--items", "5", "--yes"])
    assert result.exit_code == 0
    fake_cls.clear_history.assert_awaited_once()
    summary = _json_out(result)
    assert summary == {"requested": 5, "cleared": True}


# ── the mode conflicts are refused BEFORE any login ─────────────────────


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["activity", "clear", "--device", "Kitchen", "--items", "5"], "bulk-only"),
        (["activity", "clear", "--contains", "timer", "--items", "2"], "bulk-only"),
        (["activity", "clear", "--ids", "act-1", "--items", "2"], "bulk-only"),
        (["activity", "clear", "--ids", "act-1", "--device", "Kitchen"], "drop --device"),
        (["activity", "clear", "--ids", "act-1", "--contains", "tim"], "drop --device"),
        (["activity", "clear", "--limit", "5"], "--limit only applies"),
        (["activity", "clear", "--ids", "act-1", "--limit", "5"], "--limit only applies"),
        (["activity", "clear", "--ids", ""], "no ids given"),
        (
            ["activity", "clear", "--ids", "act-1", "--device", "K", "--items", "2"],
            "bulk-only",
        ),
    ],
)
def test_selective_conflicts_refused_before_login(argv, expected):
    with _stub_login() as login:
        result = _invoke(["--json", *argv])
    assert result.exit_code == 1
    assert expected in result.output
    login.assert_not_called()


# ── selective: explicit ids ──────────────────────────────────────────────


def test_ids_preview_never_fetches_and_never_deletes():
    fake_cls = _fake_alexa()
    with _stub_login(), patch("alexapy.AlexaAPI", fake_cls):
        result = _invoke(["--json", "activity", "clear", "--ids", "act-1, act-2"])
    assert result.exit_code == 0
    fake_cls.get_activities.assert_not_awaited()
    assert fake_cls.calls == []
    parsed = _json_out(result)
    assert parsed["dry_run"] is True
    assert parsed["would_delete"] == ["act-1", "act-2"]
    assert parsed["irreversible"] is True
    assert [r["id"] for r in parsed["rows"]] == ["act-1", "act-2"]


def test_ids_yes_deletes_exactly_the_named_ids_for_real():
    fake_cls = _fake_alexa([200])
    with _stub_login(), patch("alexapy.AlexaAPI", fake_cls):
        result = _invoke(["--json", "activity", "clear", "--ids", "act-1", "--yes"])
    assert result.exit_code == 0
    assert fake_cls.calls == [("delete", "/api/activities/act-1")]
    assert _json_out(result)["deleted"] == 1


def test_ids_are_deduplicated_and_cleaned():
    fake_cls = _fake_alexa([200, 200])
    with _stub_login(), patch("alexapy.AlexaAPI", fake_cls):
        result = _invoke(["--json", "activity", "clear", "--ids", " act-1 ,,act-1,act-2", "--yes"])
    assert result.exit_code == 0
    assert fake_cls.calls == [
        ("delete", "/api/activities/act-1"),
        ("delete", "/api/activities/act-2"),
    ]


def test_ids_yes_reports_a_refused_id_never_a_quiet_pass():
    fake_cls = _fake_alexa([200, 404])
    with _stub_login(), patch("alexapy.AlexaAPI", fake_cls):
        result = _invoke(["--json", "activity", "clear", "--ids", "act-1,act-3", "--yes"])
    assert result.exit_code == 0
    summary = _json_out(result)
    assert summary["deleted"] == 1
    assert summary["cleared"] is False
    assert summary["refused"] == [{"id": "act-3", "status": 404}]
    assert "manually in the Alexa app" in summary["hint"]


# ── selective: filters over the fetched rows ────────────────────────────


def test_filter_preview_fetches_but_never_deletes():
    fake_cls = _fake_alexa()
    with _stub_login(), patch("alexapy.AlexaAPI", fake_cls):
        result = _invoke(["--json", "activity", "clear", "--device", "Kitchen"])
    assert result.exit_code == 0
    fake_cls.get_activities.assert_awaited_once()
    assert fake_cls.calls == []  # nothing was deleted in a preview
    parsed = _json_out(result)
    assert parsed["dry_run"] is True
    assert parsed["would_delete"] == ["act-1", "act-3"]
    assert [r["id"] for r in parsed["rows"]] == ["act-1", "act-3"]
    assert "irreversible" in parsed
    assert "--yes" in parsed["hint"]


def test_filter_preview_passes_the_limit_through():
    fake_cls = _fake_alexa(activities={"activities": []})
    with _stub_login(), patch("alexapy.AlexaAPI", fake_cls):
        result = _invoke(["--json", "activity", "clear", "--device", "Kitchen", "--limit", "7"])
    assert result.exit_code == 0
    assert fake_cls.get_activities.call_args.kwargs == {"items": 7}


def test_filter_yes_deletes_exactly_the_previewed_ids():
    fake_cls = _fake_alexa([200, 200])
    preview_runs = []
    with _stub_login(), patch("alexapy.AlexaAPI", fake_cls):
        for argv in (
            ["activity", "clear", "--device", "Kitchen"],
            ["activity", "clear", "--device", "Kitchen", "--yes"],
        ):
            result = _invoke(["--json", *argv])
            assert result.exit_code == 0
            preview_runs.append(_json_out(result))
    # the preview advertised act-1 + act-3; the executed run deleted exactly them
    assert preview_runs[0]["would_delete"] == ["act-1", "act-3"]
    assert fake_cls.calls == [
        ("delete", "/api/activities/act-1"),
        ("delete", "/api/activities/act-3"),
    ]
    assert preview_runs[1] == {"requested": 2, "deleted": 2, "cleared": True}


def test_contains_preview_selects_only_the_matching_text():
    fake_cls = _fake_alexa()
    with _stub_login(), patch("alexapy.AlexaAPI", fake_cls):
        result = _invoke(["--json", "activity", "clear", "--contains", "another timer"])
    assert result.exit_code == 0
    assert _json_out(result)["would_delete"] == ["act-3"]


def test_a_device_and_contains_can_be_combined():
    fake_cls = _fake_alexa()
    with _stub_login(), patch("alexapy.AlexaAPI", fake_cls):
        result = _invoke(
            ["--json", "activity", "clear", "--device", "Kitchen", "--contains", "tim"]
        )
    assert result.exit_code == 0
    assert _json_out(result)["would_delete"] == ["act-3"]


def test_nothing_matching_reports_nothing_deleted():
    fake_cls = _fake_alexa()
    with _stub_login(), patch("alexapy.AlexaAPI", fake_cls):
        result = _invoke(["--json", "activity", "clear", "--device", "Attic", "--yes"])
    assert result.exit_code == 0
    assert fake_cls.calls == []
    assert _json_out(result) == {
        "matching": 0,
        "deleted": 0,
        "hint": "nothing matched; nothing was deleted",
    }


def test_a_bad_limit_is_refused_before_login_for_selective_filters():
    fake_cls = _fake_alexa()
    with _stub_login() as login, patch("alexapy.AlexaAPI", fake_cls):
        result = _invoke(["--json", "activity", "clear", "--device", "Kitchen", "--limit", "many"])
    assert result.exit_code == 1
    assert "whole number" in result.output
    login.assert_not_called()
    fake_cls.get_activities.assert_not_awaited()


def test_a_refused_delete_reports_partial_with_the_remedy():
    fake_cls = _fake_alexa([200, 404])
    with _stub_login(), patch("alexapy.AlexaAPI", fake_cls):
        result = _invoke(["--json", "activity", "clear", "--device", "Kitchen", "--yes"])
    assert result.exit_code == 0
    summary = _json_out(result)
    assert summary["requested"] == 2
    assert summary["deleted"] == 1
    assert summary["cleared"] is False
    assert summary["refused"] == [{"id": "act-3", "status": 404}]
    assert "manually in the Alexa app" in summary["hint"]


def test_an_unreachable_delete_is_reported_unconfirmed():
    fake_cls = _fake_alexa([200, None])
    with _stub_login(), patch("alexapy.AlexaAPI", fake_cls):
        result = _invoke(["--json", "activity", "clear", "--device", "Kitchen", "--yes"])
    assert result.exit_code == 0
    summary = _json_out(result)
    assert summary["requested"] == 2
    assert summary["deleted"] == 1
    assert summary["cleared"] is False
    assert summary["unconfirmed"] == ["act-3"]
    assert "activity records" in summary["hint"]


# ── activity records gains the same filters ─────────────────────────────


def test_records_filters_help_find_the_ids_a_clear_will_delete():
    fake_cls = _fake_alexa()
    with _stub_login(), patch("alexapy.AlexaAPI", fake_cls):
        result = _invoke(["--json", "activity", "records", "--device", "Kitchen"])
    assert result.exit_code == 0
    assert [r["id"] for r in _json_out(result)] == ["act-1", "act-3"]


def test_records_contains_filter_still_carries_its_ids():
    fake_cls = _fake_alexa()
    with _stub_login(), patch("alexapy.AlexaAPI", fake_cls):
        result = _invoke(["--json", "activity", "records", "--contains", "timer"])
    assert result.exit_code == 0
    assert [r["id"] for r in _json_out(result)] == ["act-2", "act-3"]
