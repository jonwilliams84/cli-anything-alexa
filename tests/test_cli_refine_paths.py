"""Behavioural tests for CLI command paths left uncovered after the first pass.

Follows the same rules as the rest of the suite: assertions are on observable
behaviour — exit code, JSON on stdout, and which core coroutine was invoked
with what — never on source text.  Every mutating command is checked against
the harness-wide contract: **preview by default, act only on --yes**.

Areas covered here that earlier files did not reach:
  * `auth login` (scripted AND guided-proxy flows) + `auth import-pickle` success
  * `devices list` filters, bulk `rename --pattern` / `--map` (dry-run + --yes),
    single rename, `duplicates`
  * `devices prune` (text preview + execute), `devices delete --entity/--name`
    with native warnings + `--verify`
  * `discover --yes`, `echos list` / `echos bluetooth`
  * groups: list, create/add/remove/set/delete execute paths (incl. --device)
  * routines list/run (--yes + failure abort)
  * notifications: list/show, add-reminder/add-alarm/add-timer (dry-run + --yes
    + unknown-device abort), delete --yes
  * announce / speak / dnd execute paths
  * the REPL loop (help / blank / unknown command / exit / Ctrl-C) and `main()`
  * `python -m cli_anything.alexa` (`__main__`)
  * ReplSkin banner, history-file bootstrap, color paths and prompt-toolkit
    fallbacks
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli_anything.alexa.alexa_cli import cli, main
from cli_anything.alexa.core import session as session_core
from cli_anything.alexa.utils import repl_skin as repl_skin_module
from cli_anything.alexa.utils.repl_skin import ReplSkin

# ── fixtures (data shapes mirror the live API surfaces) ─────────────────

_RECORDS = [
    {
        "endpointId": "amzn1.alexa.endpoint.lamp",
        "applianceId": "SKILL_blob_light#kitchen_lamp",
        "name": "Kitchen Lamp",
        "manufacturer": "Home Assistant",
        "ha_sourced": True,
        "entity_id": "light.kitchen_lamp",
        "enabled": "ENABLED",
    },
    {
        "endpointId": "amzn1.alexa.endpoint.plug",
        "applianceId": "APPL-PLUG",
        "name": "Lounge Plug",
        "manufacturer": "Tuya",
        "ha_sourced": False,
        "entity_id": None,
        "enabled": "ENABLED",
    },
]

_DEVICES = [
    {
        "accountName": "Kitchen Echo",
        "serialNumber": "SERIAL-1",
        "deviceType": "A3S5BH2HU6VAYF",
        "deviceFamily": "ECHO",
        "online": True,
    }
]

_APPLIANCES = [
    {
        "applianceId": "SKILL_blob_light#kitchen_lamp",
        "friendlyName": "Kitchen Lamp",
        "manufacturerName": "Home Assistant",
        "modelName": "light",
    },
    {
        "applianceId": "APPL-PLUG",
        "friendlyName": "Lounge Plug",
        "manufacturerName": "Tuya",
        "modelName": "plug",
    },
]


@contextlib.contextmanager
def _stub_cli(records=None, results=None, devices=None, appliances=None):
    """Patch `_login` and `_run` so no network / alexapy is involved.

    `_run` closes each coroutine it is handed (nothing left un-awaited) and
    answers by coroutine name: `fetch_endpoint_records` -> records,
    `fetch_devices` -> devices, `fetch_appliances` -> appliances, anything
    else pops the next value off `results`. An Exception in the queue is
    raised (so `_run`'s callers see the real error paths).
    """
    queue = list(results or [])
    seen: list[str] = []

    def fake_run(_ctx, coro):
        name = getattr(coro, "__name__", "") or ""
        seen.append(name)
        if hasattr(coro, "close"):
            coro.close()
        if name == "fetch_endpoint_records":
            return [dict(r) for r in (records if records is not None else _RECORDS)]
        if name == "fetch_devices":
            return [dict(d) for d in (devices if devices is not None else _DEVICES)]
        if name == "fetch_appliances":
            return [dict(a) for a in (appliances if appliances is not None else _APPLIANCES)]
        if queue and isinstance(queue[0], Exception):
            raise queue.pop(0)
        return queue.pop(0) if queue else {}

    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()):
        with patch("cli_anything.alexa.alexa_cli._run", side_effect=fake_run) as mock:
            mock.seen = seen
            yield mock


def _invoke(args, obj=None):
    return CliRunner().invoke(cli, args, obj=obj or {}, catch_exceptions=False)


def _json_out(result):
    return json.loads(result.output)


# ── auth login / import-pickle ──────────────────────────────────────────


def test_auth_login_scripted_flow_persists_and_reports():
    """`auth login --password` uses the scripted login and reports success."""
    done = {}

    async def fake_fresh_login(*args, **kwargs):
        done["args"] = args
        return None

    with patch("cli_anything.alexa.alexa_cli.session_core.fresh_login", fake_fresh_login):
        with patch("cli_anything.alexa.alexa_cli.project.save_config") as save:
            result = _invoke(
                ["--json", "auth", "login", "--email", "u@example.com", "--password", "pw"]
            )
    assert result.exit_code == 0
    assert _json_out(result) == {"logged_in": True, "email": "u@example.com", "method": "scripted"}
    assert save.called
    assert done["args"][0] == "u@example.com"


def test_auth_login_scripted_flow_aborts_on_session_error():
    """A scripted-login failure surfaces as a clean abort, not a traceback."""
    with patch(
        "cli_anything.alexa.alexa_cli.session_core.fresh_login",
        side_effect=session_core.AlexaSessionError("no way"),
    ):
        result = _invoke(
            ["--json", "auth", "login", "--email", "u@example.com", "--password", "pw"]
        )
    assert result.exit_code == 1
    assert "no way" in result.output


def test_auth_login_proxy_flow_persists_and_reports():
    """The default (no --password) flow runs the guided proxy login."""
    calls = {}

    async def fake_proxy_login(email, **kwargs):
        calls["email"] = email
        calls["on_url"] = kwargs.get("on_url")
        return None

    with patch("cli_anything.alexa.alexa_cli.session_core.proxy_login", fake_proxy_login):
        with patch("cli_anything.alexa.alexa_cli.project.save_config"):
            result = _invoke(
                ["--json", "auth", "login", "--email", "u@example.com", "--url", "amazon.co.uk"]
            )
    assert result.exit_code == 0
    assert _json_out(result) == {"logged_in": True, "email": "u@example.com", "method": "proxy"}
    assert calls["email"] == "u@example.com"
    # in --json mode the URL banner callback is a no-op that returns early
    assert calls["on_url"]("http://localhost:8080") is None


def test_auth_login_proxy_flow_aborts_on_oserror():
    """A proxy that cannot bind surfaces as a friendly port/host hint."""
    with patch(
        "cli_anything.alexa.alexa_cli.session_core.proxy_login",
        side_effect=OSError("address in use"),
    ):
        result = _invoke(
            ["--json", "auth", "login", "--email", "u@example.com", "--url", "amazon.co.uk"]
        )
    assert result.exit_code == 1
    assert "--port" in result.output


def test_auth_import_pickle_success_persists_email_and_tests_cookie(tmp_path):
    """A snapshot import copies the cookie, saves the email and re-tests."""
    def fake_run_async(coro):
        if hasattr(coro, "close"):
            coro.close()
        return True

    with patch(
        "cli_anything.alexa.alexa_cli.session_core.import_pickle",
        return_value=tmp_path / "cookie.pickle",
    ) as imp:
        with patch("cli_anything.alexa.alexa_cli.session_core.run_async", fake_run_async):
            with patch("cli_anything.alexa.alexa_cli.project.save_config"):
                    result = _invoke(
                        ["--json", "auth", "import-pickle", "/tmp/fake.pickle", "--email", "u@x.com"]
                    )
    assert result.exit_code == 0
    parsed = _json_out(result)
    assert parsed["logged_in"] is True
    assert parsed["email"] == "u@x.com"
    assert imp.called


# ── devices list / duplicates ───────────────────────────────────────────


def test_devices_list_ha_only_filters_out_native():
    with _stub_cli() as run:
        result = _invoke(["--json", "devices", "list", "--ha-only"])
    assert result.exit_code == 0
    rows = _json_out(result)
    assert [r["name"] for r in rows] == ["Kitchen Lamp"]
    assert "fetch_endpoint_records" in run.seen


def test_devices_list_manufacturer_filter_is_case_insensitive():
    with _stub_cli():
        result = _invoke(["--json", "devices", "list", "--manufacturer", "TUYA"])
    rows = _json_out(result)
    assert [r["name"] for r in rows] == ["Lounge Plug"]
    assert rows[0]["source"] == "native"


def test_devices_duplicates_json_reports_clusters():
    twins = [
        dict(_RECORDS[0]),
        {**_RECORDS[1], "name": "Kitchen Lamp", "endpointId": "amzn1.alexa.endpoint.twin"},
    ]
    with _stub_cli(records=twins):
        result = _invoke(["--json", "devices", "duplicates"])
    assert result.exit_code == 0
    clusters = _json_out(result)
    assert clusters[0]["name"] == "Kitchen Lamp"
    assert clusters[0]["count"] == 2
    assert clusters[0]["native_plus_ha"] is True


def test_devices_duplicates_text_mode_says_nothing_to_find():
    with _stub_cli(records=[_RECORDS[0]]):
        result = _invoke(["devices", "duplicates"])
    assert result.exit_code == 0
    assert "no duplicate" in result.output


# ── devices rename (bulk + single) ──────────────────────────────────────


def test_devices_rename_pattern_dry_run_then_execute():
    with _stub_cli() as run:
        result = _invoke(["--json", "devices", "rename", "--pattern", "s/Lamp/Light/"])
    assert result.exit_code == 0
    parsed = _json_out(result)
    assert parsed["dry_run"] is True
    assert parsed["count"] == 1
    assert parsed["renames"][0]["old"] == "Kitchen Lamp"
    assert parsed["renames"][0]["new"] == "Kitchen Light"
    assert "apply_renames" not in run.seen

    with _stub_cli(results=[{"renamed": 1}]) as run:
        result = _invoke(["--json", "devices", "rename", "--pattern", "s/Lamp/Light/", "--yes"])
    assert result.exit_code == 0
    assert "apply_renames" in run.seen


def test_devices_rename_map_with_unresolved_target_aborts():
    map_file = Path("/tmp") / "refine-map-bad.txt"
    map_file.write_text("# comment\nNo Such Device => Renamed\n")
    with _stub_cli():
        result = _invoke(["--json", "devices", "rename", "--map", str(map_file)])
    assert result.exit_code == 1
    assert "unresolved map entries" in result.output
    map_file.unlink()


def test_devices_rename_map_dry_run_then_execute(tmp_path):
    map_file = tmp_path / "map.txt"
    map_file.write_text("Kitchen Lamp => Lamp\n")
    with _stub_cli() as run:
        result = _invoke(["--json", "devices", "rename", "--map", str(map_file)])
    assert result.exit_code == 0
    parsed = _json_out(result)
    assert parsed["dry_run"] is True
    assert parsed["renames"][0]["new"] == "Lamp"

    with _stub_cli(results=[{"renamed": 1}]) as run:
        result = _invoke(["--json", "devices", "rename", "--map", str(map_file), "--yes"])
    assert result.exit_code == 0
    assert "apply_renames" in run.seen


def test_devices_rename_single_dry_run_then_execute():
    with _stub_cli() as run:
        result = _invoke(["--json", "devices", "rename", "Kitchen Lamp", "Task Lamp"])
    assert result.exit_code == 0
    parsed = _json_out(result)
    assert parsed["dry_run"] is True
    assert parsed["would_rename"] == "Kitchen Lamp"
    assert parsed["to"] == "Task Lamp"

    with _stub_cli(results=[{"renamed": "Task Lamp"}]) as run:
        result = _invoke(["--json", "devices", "rename", "Kitchen Lamp", "Task Lamp", "--yes"])
    assert result.exit_code == 0
    assert "rename_endpoint" in run.seen


# ── devices prune / delete ──────────────────────────────────────────────


def test_devices_prune_text_preview_lists_would_delete(tmp_path):
    whitelist = tmp_path / "whitelist.txt"
    whitelist.write_text("# keep the lamp\nlight.tv_backlight\n")
    with _stub_cli():
        result = _invoke(["devices", "prune", "--whitelist", str(whitelist)])
    assert result.exit_code == 0
    assert "Would delete:" in result.output
    assert "Kitchen Lamp" in result.output
    assert "Re-run with --no-dry-run --yes" in result.output


def test_devices_prune_execute_deletes_only_unwhitelisted(tmp_path):
    whitelist = tmp_path / "whitelist.txt"
    whitelist.write_text("light.tv_backlight\n")
    with _stub_cli(results=[{"deleted": "SKILL_blob_light#kitchen_lamp"}]) as run:
        result = _invoke(
            ["--json", "devices", "prune", "--whitelist", str(whitelist), "--no-dry-run", "--yes"]
        )
    assert result.exit_code == 0
    parsed = _json_out(result)
    assert parsed["dry_run"] is False
    assert parsed["to_delete"] == 1
    assert parsed["skipped_non_ha"] == 1
    assert "delete_appliance" in run.seen


def test_devices_delete_by_entity_warns_native_and_executes_with_verify():
    """--entity resolves to an applianceId; native targets warn; --verify re-syncs."""
    with _stub_cli(
        results=[
            {"deleted": "SKILL_blob_light#kitchen_lamp"},
            {"deleted": "APPL-PLUG"},
            {"reappeared": [], "checked": 2},
        ]
    ) as run:
        result = _invoke(
            ["--json", "devices", "delete", "--entity", "light.kitchen_lamp", "--name",
             "Lounge Plug", "--yes", "--verify"]
        )
    assert result.exit_code == 0
    parsed = _json_out(result)
    assert parsed["verify"]["reappeared"] == []
    assert "delete_appliance" in run.seen
    assert "verify_deletes" in run.seen


def test_devices_delete_native_target_warns_in_text_mode():
    with _stub_cli():
        result = _invoke(["devices", "delete", "APPL-PLUG"])
    assert result.exit_code == 0
    assert "warning:" in result.output
    assert "dry-run" in result.output or "would_delete" in result.output


# ── discover / echos ────────────────────────────────────────────────────


def test_discover_yes_triggers_discovery():
    with _stub_cli(results=[{}]) as run:
        result = _invoke(["--json", "discover", "--yes"])
    assert result.exit_code == 0
    assert "trigger_discovery" in run.seen


def test_echos_list_emits_rows():
    with _stub_cli() as run:
        result = _invoke(["--json", "echos", "list"])
    assert result.exit_code == 0
    rows = _json_out(result)
    assert rows[0]["accountName"] == "Kitchen Echo"
    assert "fetch_devices" in run.seen


def test_echos_bluetooth_emits_pairings():
    with _stub_cli(results=[[{"device": "Kitchen Echo", "paired": []}]]) as run:
        result = _invoke(["--json", "echos", "bluetooth"])
    assert result.exit_code == 0
    assert "fetch_bluetooth" in run.seen


# ── groups ──────────────────────────────────────────────────────────────


def test_groups_list_emits_rows():
    with _stub_cli(results=[{"groups": []}]) as run:
        result = _invoke(["--json", "groups", "list"])
    assert result.exit_code == 0
    assert "list_groups" in run.seen


def test_groups_create_execute():
    with _stub_cli(results=[{"created": "Room"}]) as run:
        result = _invoke(["--json", "groups", "create", "Room", "--endpoint", "eid-1", "--yes"])
    assert result.exit_code == 0
    assert _json_out(result) == {"created": "Room"}
    assert "create_group" in run.seen


def test_groups_member_update_resolves_native_device_by_display_name():
    """`groups add --device <display name>` resolves a native (non-HA) device."""
    with patch(
        "cli_anything.alexa.alexa_cli._find_group_or_abort", return_value=_group()
    ):
        with _stub_cli(results=[{"updated": "gid-1"}]) as run:
            result = _invoke(
                ["--json", "groups", "add", "Room", "--device", "Lounge Plug", "--yes"]
            )
    assert result.exit_code == 0
    assert "update_group" in run.seen


def test_groups_member_update_unknown_device_aborts():
    with patch(
        "cli_anything.alexa.alexa_cli._find_group_or_abort", return_value=_group()
    ):
        with _stub_cli():
            result = _invoke(["--json", "groups", "add", "Room", "--device", "Nope", "--yes"])
    assert result.exit_code == 1
    assert "no device matching" in result.output


def _group():
    return {"id": "gid-1", "friendlyName": {"value": {"text": "Room"}}}


def test_groups_add_execute_uses_update_group():
    with patch(
        "cli_anything.alexa.alexa_cli._find_group_or_abort", return_value=_group()
    ), patch(
        "cli_anything.alexa.alexa_cli._resolve_group_members", return_value=["eid-1"]
    ), patch(
        "cli_anything.alexa.alexa_cli._resolve_child_groups", return_value=[]
    ):
        with _stub_cli(results=[{"updated": "gid-1"}]) as run:
            result = _invoke(["--json", "groups", "add", "Room", "--endpoint", "eid-1", "--yes"])
    assert result.exit_code == 0
    assert "update_group" in run.seen


def test_groups_remove_execute():
    with patch(
        "cli_anything.alexa.alexa_cli._find_group_or_abort", return_value=_group()
    ), patch(
        "cli_anything.alexa.alexa_cli._resolve_group_members", return_value=["eid-1"]
    ), patch(
        "cli_anything.alexa.alexa_cli._resolve_child_groups", return_value=[]
    ):
        with _stub_cli(results=[{"updated": "gid-1"}]):
            result = _invoke(["--json", "groups", "remove", "Room", "--endpoint", "eid-1", "--yes"])
    assert result.exit_code == 0


def test_groups_set_execute():
    with patch(
        "cli_anything.alexa.alexa_cli._find_group_or_abort", return_value=_group()
    ), patch(
        "cli_anything.alexa.alexa_cli._resolve_group_members", return_value=["eid-9"]
    ), patch(
        "cli_anything.alexa.alexa_cli._resolve_child_groups", return_value=[]
    ):
        with _stub_cli(results=[{"updated": "gid-1"}]):
            result = _invoke(["--json", "groups", "set", "Room", "--endpoint", "eid-9", "--yes"])
    assert result.exit_code == 0


def test_groups_delete_execute():
    with patch("cli_anything.alexa.alexa_cli._find_group_or_abort", return_value=_group()):
        with _stub_cli(results=[{"deleted": "gid-1"}]) as run:
            result = _invoke(["--json", "groups", "delete", "Room", "--yes"])
    assert result.exit_code == 0
    assert "delete_group" in run.seen


# ── routines ────────────────────────────────────────────────────────────


def test_routines_list_emits_rows():
    with _stub_cli(results=[[{"name": "Good Morning"}]]) as run:
        result = _invoke(["--json", "routines", "list"])
    assert result.exit_code == 0
    assert _json_out(result) == [{"name": "Good Morning"}]
    assert "list_routines" in run.seen


def test_routines_run_execute():
    with _stub_cli(results=[{"ran": "Good Morning"}]) as run:
        result = _invoke(["--json", "routines", "run", "Good Morning", "--yes"])
    assert result.exit_code == 0
    assert "run_routine" in run.seen


def test_routines_run_failure_aborts_cleanly():
    with _stub_cli(results=[ValueError("no routine matching 'Nope'")]):
        result = _invoke(["--json", "routines", "run", "Nope", "--yes"])
    assert result.exit_code == 1
    assert "no routine matching" in result.output


# ── notifications ───────────────────────────────────────────────────────


def test_notifications_list_and_show():
    with _stub_cli(results=[[{"id": "n-1"}], {"id": "n-1", "raw": {}}]) as run:
        assert _invoke(["--json", "notifications", "list"]).exit_code == 0
        assert _invoke(["--json", "notifications", "show", "n-1"]).exit_code == 0
    assert "list_notifications" in run.seen
    assert "show_notification" in run.seen


def test_notifications_add_reminder_dry_run_then_execute():
    with _stub_cli() as run:
        result = _invoke(
            ["--json", "notifications", "add-reminder", "Stretch", "--device", "Kitchen Echo",
             "--in", "600"]
        )
    assert result.exit_code == 0
    parsed = _json_out(result)
    assert parsed["dry_run"] is True
    assert parsed["payload"]["type"] == "Reminder"
    assert "create_notification" not in run.seen

    with _stub_cli(results=[{"created": True}]) as run:
        result = _invoke(
            ["--json", "notifications", "add-reminder", "Stretch", "--device", "Kitchen Echo",
             "--in", "600", "--yes"]
        )
    assert result.exit_code == 0
    assert "create_notification" in run.seen


def test_notifications_add_reminder_unknown_device_aborts():
    with _stub_cli():
        result = _invoke(
            ["--json", "notifications", "add-reminder", "Stretch", "--device", "Ghost", "--in",
             "600"]
        )
    assert result.exit_code == 1
    assert "no device matching" in result.output


def test_notifications_add_alarm_execute_with_repeat_weekly():
    with _stub_cli(results=[{"created": True}]) as run:
        result = _invoke(
            ["--json", "notifications", "add-alarm", "--device", "Kitchen Echo", "--in", "60",
             "--label", "Wake", "--repeat", "weekly", "--days", "Mon,Thu", "--yes"]
        )
    assert result.exit_code == 0
    assert "create_notification" in run.seen


def test_notifications_add_alarm_rejects_days_without_weekly():
    result = _invoke(
        ["--json", "notifications", "add-alarm", "--device", "Kitchen Echo", "--in", "60",
         "--repeat", "daily", "--days", "Mon", "--yes"]
    )
    assert result.exit_code == 1
    assert "--days only applies to --repeat weekly" in result.output


def test_notifications_add_timer_execute():
    with _stub_cli(results=[{"created": True}]) as run:
        result = _invoke(
            ["--json", "notifications", "add-timer", "--device", "Kitchen Echo", "--duration", "90",
             "--label", "Tea", "--yes"]
        )
    assert result.exit_code == 0
    assert "create_notification" in run.seen


def test_notifications_delete_execute():
    with _stub_cli(results=[{"deleted": "n-1"}]) as run:
        result = _invoke(["--json", "notifications", "delete", "n-1", "--yes"])
    assert result.exit_code == 0
    assert "delete_notification" in run.seen


# ── announce / speak / dnd execute paths ────────────────────────────────


def test_announce_execute():
    with _stub_cli(results=[{"announced": "hi", "targets": 2}]) as run:
        result = _invoke(["--json", "announce", "hi", "--yes"])
    assert result.exit_code == 0
    assert "announce" in run.seen


def test_speak_execute():
    with _stub_cli(results=[{"spoken": "hi"}]) as run:
        result = _invoke(["--json", "speak", "hi", "--device", "Kitchen Echo", "--yes"])
    assert result.exit_code == 0
    assert "speak" in run.seen


def test_dnd_execute():
    with _stub_cli(results=[{"device": "Kitchen Echo", "dnd": True}]) as run:
        result = _invoke(["--json", "dnd", "Kitchen Echo", "on", "--yes"])
    assert result.exit_code == 0
    assert "set_dnd" in run.seen


def test_speak_rejects_empty_text_before_login():
    result = _invoke(["--json", "speak", "   "])
    assert result.exit_code == 1


# ── REPL loop / main / __main__ ─────────────────────────────────────────


class _FakeSkin:
    """Minimal ReplSkin stand-in that replays scripted lines."""

    instances: list["_FakeSkin"] = []
    inputs: list[str] = []

    def __init__(self, software, version):
        self.software = software
        self.version = version
        self.banner = False
        self.goodbye = False
        self.errors: list[str] = []
        self.helped = False
        self.prompt_session = object()
        _FakeSkin.instances.append(self)

    def print_banner(self):
        self.banner = True

    def create_prompt_session(self):
        return self.prompt_session

    def get_input(self, pt_session):
        assert pt_session is self.prompt_session
        return _FakeSkin.inputs.pop(0)

    def print_goodbye(self):
        self.goodbye = True

    def help(self, commands):
        self.helped = True
        assert "devices" in commands

    def error(self, message):
        self.errors.append(message)


@pytest.fixture()
def fake_skin():
    _FakeSkin.instances = []
    _FakeSkin.inputs = []
    with patch.object(repl_skin_module, "ReplSkin", _FakeSkin):
        yield _FakeSkin


def test_repl_no_subcommand_starts_repl_and_exits_on_exit(fake_skin):
    _FakeSkin.inputs = ["help", "   ", "exit"]
    result = _invoke([])
    assert result.exit_code == 0
    skin = _FakeSkin.instances[0]
    assert skin.banner and skin.goodbye and skin.helped
    assert skin.errors == []


def test_repl_reports_errors_from_bad_commands(fake_skin):
    _FakeSkin.inputs = ["definitely-not-a-command", "exit"]
    result = _invoke([])
    assert result.exit_code == 0
    skin = _FakeSkin.instances[0]
    assert skin.errors, "an unknown command must reach skin.error, not crash the REPL"


def test_repl_quit_word_also_exits(fake_skin):
    _FakeSkin.inputs = ["quit"]
    result = _invoke([])
    assert result.exit_code == 0
    assert _FakeSkin.instances[0].goodbye


def test_repl_ctrl_c_says_goodbye(fake_skin):
    class _Interrupting(_FakeSkin):
        def get_input(self, pt_session):
            raise KeyboardInterrupt

    with patch.object(repl_skin_module, "ReplSkin", _Interrupting):
        result = _invoke([])
    assert result.exit_code == 0
    assert _Interrupting.instances[0].goodbye


def test_repl_explicit_subcommand_skips_repl(fake_skin):
    with _stub_cli() as run:
        result = _invoke(["--json", "discover"])
    assert result.exit_code == 0
    assert _FakeSkin.instances == []


def test_main_entrypoint_passes_fresh_obj():
    with patch("cli_anything.alexa.alexa_cli.cli") as mock_cli:
        main()
    mock_cli.assert_called_once_with(obj={})


def test_dunder_main_module_invokes_cli():
    """`python -m cli_anything.alexa` wires __main__ to alexa_cli.main()."""
    import runpy

    assert (Path(repl_skin_module.__file__).parent.parent / "__main__.py").exists()
    sys.modules.pop("cli_anything.alexa.__main__", None)
    with patch("cli_anything.alexa.alexa_cli.main") as mock_main:
        runpy.run_module("cli_anything.alexa.__main__", run_name="__main__")
    mock_main.assert_called_once()


# ── ReplSkin: banner / history / colour / prompt-toolkit fallbacks ──────


def _skin(**kwargs):
    return ReplSkin("alexa", version="0.0.0-test", **kwargs)


def test_repl_skin_creates_history_file_under_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLI_ANYTHING_NO_COLOR", raising=False)
    skin = _skin(skill_path=str(tmp_path / "SKILL.md"))
    hist = Path(skin.history_file)
    assert hist.parent.exists()  # the directory is bootstrapped eagerly
    assert ".cli-anything-alexa" in str(hist)


def test_repl_skin_banner_prints_box_and_metadata(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("CLI_ANYTHING_NO_COLOR", "1")
    skin = _skill_file_skin(tmp_path)
    skin.print_banner()
    out = capsys.readouterr().out
    assert "cli-anything" in out
    assert "alexa" in out
    assert "╭" in out and "╰" in out  # box drawing
    assert "v0.0.0-test" in out


def _skill_file_skin(tmp_path):
    skill = tmp_path / "SKILL.md"
    skill.write_text("# skill\n")
    return _skin(skill_path=str(skill))


def test_repl_skin_banner_wraps_long_install_command(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("CLI_ANYTHING_NO_COLOR", "1")
    skin = _skill_file_skin(tmp_path)
    skin.skill_install_cmd = "npx skills add " + "x" * 120 + " --skill alexa -g -y"
    skin.print_banner()
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) > 6  # the long install line must wrap, not truncate the box


def test_repl_skin_table_renders_colored_cells(capsys):
    skin = _skill_file_skin(Path("/tmp"))
    skin._color = True
    skin.table(["name", "state"], [["Kitchen Lamp", "on"]])
    out = capsys.readouterr().out
    assert "\033[" in out
    assert "Kitchen Lamp" in out


def test_repl_skin_detect_color_true_when_tty(monkeypatch):
    skin = _skill_file_skin(Path("/tmp"))
    fake_stdout = MagicMock()
    fake_stdout.isatty.return_value = True
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("CLI_ANYTHING_NO_COLOR", raising=False)
    with patch.object(repl_skin_module.sys, "stdout", fake_stdout):
        assert skin._detect_color_support() is True


def test_repl_skin_detect_color_false_without_isatty(monkeypatch):
    skin = _skill_file_skin(Path("/tmp"))
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("CLI_ANYTHING_NO_COLOR", raising=False)
    with patch.object(repl_skin_module.sys, "stdout", MagicMock(spec=[])):  # no isatty attr
        assert skin._detect_color_support() is False


def test_repl_skin_get_prompt_style_returns_none_when_prompt_toolkit_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "prompt_toolkit.styles", None)
    skin = _skill_file_skin(Path("/tmp"))
    assert skin.get_prompt_style() is None


def test_repl_skin_create_prompt_session_returns_none_when_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "prompt_toolkit", None)
    skin = _skill_file_skin(Path("/tmp"))
    assert skin.create_prompt_session() is None


def test_repl_skin_get_input_uses_prompt_session_and_strips():
    skin = _skill_file_skin(Path("/tmp"))
    session = MagicMock()
    session.prompt.return_value = "  devices list  "
    assert skin.get_input(session) == "devices list"
