"""cli-anything-alexa — manage Amazon Alexa from the command line.

Built on the unofficial Alexa web API (the one the app uses) via `alexapy`,
reusing an existing cookie session (e.g. Home Assistant's alexa_media
pickle) so there is no per-call MFA. Sibling of cli-anything-homeassistant
and cli-anything-zigbee2mqtt — same Click + REPL + `--json` conventions.
"""

from __future__ import annotations

import json
import shlex
import sys
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path

import click

from cli_anything.alexa.core import project
from cli_anything.alexa.core import appliances as appliances_pure
from cli_anything.alexa.core import devices as devices_core
from cli_anything.alexa.core import devices_meta as devices_meta_core
from cli_anything.alexa.core import notifications as notifications_core
from cli_anything.alexa.core import routines as routines_core
from cli_anything.alexa.core import control as control_core
from cli_anything.alexa.core import groups as groups_core
from cli_anything.alexa.core import session as session_core
from cli_anything.alexa.core.formatting import render_table

CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


def _resolve_version() -> str:
    try:
        return _pkg_version("cli-anything-alexa")
    except PackageNotFoundError:
        return "0.1.0+unknown"


__version__ = _resolve_version()


# ──────────────────────────────────────────────────────── helpers

def _abort(message: str) -> None:
    click.echo(f"error: {message}", err=True)
    sys.exit(1)


def emit(ctx: click.Context, data) -> None:
    if ctx.obj.get("as_json"):
        click.echo(json.dumps(data, indent=2, default=str, sort_keys=True))
        return
    if data is None:
        return
    if isinstance(data, str):
        click.echo(data)
        return
    if isinstance(data, list):
        if data and isinstance(data[0], dict):
            click.echo(render_table(data))
        else:
            for item in data:
                click.echo(str(item))
        return
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, (dict, list)):
                click.echo(f"{k}: {json.dumps(v, default=str)}")
            else:
                click.echo(f"{k}: {v}")
        return
    click.echo(str(data))


def _require_email(ctx) -> str:
    email = ctx.obj.get("email")
    if not email:
        _abort(
            "no Amazon account email configured. Set it with:\n"
            "  cli-anything-alexa --email you@example.com config save\n"
            "then `auth import-pickle <ha-pickle>` or `auth login`."
        )
    return email


def _login(ctx):
    """Load + validate a session, aborting cleanly on failure."""
    email = _require_email(ctx)
    try:
        return session_core.run_async(
            session_core.load_session(
                email, url=ctx.obj.get("url", "amazon.co.uk"),
                config_dir=session_core.DEFAULT_CONFIG_DIR,
            )
        )
    except session_core.AlexaSessionError as exc:
        _abort(str(exc))


# ──────────────────────────────────────────────────────── root

@click.group(context_settings=CONTEXT_SETTINGS, invoke_without_command=True)
@click.option("--email", default=None, help="Amazon account email")
@click.option("--url", default=None, help="Account region host (default amazon.co.uk)")
@click.option("--config", "config_path", default=None, type=click.Path(),
              help="Profile path (default ~/.config/cli-anything-alexa/config.json)")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit machine-readable JSON output")
@click.version_option(version=__version__, prog_name="cli-anything-alexa")
@click.pass_context
def cli(ctx, email, url, config_path, as_json):
    """cli-anything-alexa — Amazon Alexa management over the unofficial web API."""
    ctx.ensure_object(dict)
    cfg_path_obj = Path(config_path).expanduser() if config_path else None
    cfg = project.load_config(cfg_path_obj)
    cfg = project.merge_cli_overrides(cfg, email=email, url=url)
    ctx.obj.update(cfg)
    ctx.obj["as_json"] = as_json
    ctx.obj["config_path"] = cfg_path_obj
    if ctx.invoked_subcommand is None:
        ctx.invoke(repl)


# ──────────────────────────────────────────────────────── profile

@cli.group()
def config():
    """Local connection profile (~/.config/cli-anything-alexa/config.json)."""


@config.command("show")
@click.pass_context
def config_show(ctx):
    safe = {k: v for k, v in ctx.obj.items()
            if k not in ("config_path", "as_json")}
    emit(ctx, safe)


@config.command("save")
@click.pass_context
def config_save(ctx):
    out = project.save_config(dict(ctx.obj), ctx.obj.get("config_path"))
    emit(ctx, {"saved": str(out)})


# ──────────────────────────────────────────────────────── auth

@cli.group()
def auth():
    """Manage the Alexa session (cookie import / fresh login / status)."""


@auth.command("import-pickle")
@click.argument("pickle_path", type=click.Path())
@click.option("--email", default=None,
              help="Override the account email (else uses the profile's)")
@click.pass_context
def auth_import_pickle(ctx, pickle_path, email):
    """Import an existing alexapy cookie (e.g. HA's alexa_media.<email>.pickle).

    Copies it into ~/.config/cli-anything-alexa/ under the name alexapy
    expects, so every later command reuses the session with no MFA.
    """
    em = email or ctx.obj.get("email")
    if not em:
        _abort("need --email or a configured email to name the cookie")
    try:
        dest = session_core.import_pickle(pickle_path, em)
    except session_core.AlexaSessionError as exc:
        _abort(str(exc))
    # persist the email into the profile for convenience
    cfg = dict(ctx.obj)
    cfg["email"] = em
    project.save_config(cfg, ctx.obj.get("config_path"))
    ok = session_core.run_async(
        session_core.test_loggedin(em, url=ctx.obj.get("url", "amazon.co.uk"))
    )
    emit(ctx, {"imported": str(dest), "email": em, "logged_in": ok})


@auth.command("login")
@click.option("--email", default=None)
@click.option("--password", default=None,
              help="Account password (prompted if omitted)")
@click.pass_context
def auth_login(ctx, email, password):
    """Fresh email/password/OTP login, persisting the cookie locally.

    Importing HA's existing cookie via `import-pickle` is more reliable
    (Amazon often gates fresh logins with a captcha); use that when you can.
    """
    em = email or ctx.obj.get("email")
    if not em:
        em = click.prompt("Amazon email")
    if not password:
        password = click.prompt("Password", hide_input=True)

    def otp_cb():
        return click.prompt("OTP / 2FA code")

    try:
        login = session_core.run_async(
            session_core.fresh_login(
                em, password, url=ctx.obj.get("url", "amazon.co.uk"),
                otp_callback=otp_cb,
            )
        )
    except session_core.AlexaSessionError as exc:
        _abort(str(exc))
    cfg = dict(ctx.obj)
    cfg["email"] = em
    project.save_config(cfg, ctx.obj.get("config_path"))
    emit(ctx, {"logged_in": True, "email": em})


@auth.command("status")
@click.pass_context
def auth_status(ctx):
    """Validate the saved cookie (test_loggedin)."""
    email = _require_email(ctx)
    ok = session_core.run_async(
        session_core.test_loggedin(email, url=ctx.obj.get("url", "amazon.co.uk"))
    )
    emit(ctx, {"email": email, "logged_in": ok})
    if not ok:
        sys.exit(1)


# ──────────────────────────────────────────────────────── devices (appliances)

@cli.group()
def devices():
    """Smart-home appliances — list / prune orphans / delete."""


@devices.command("list")
@click.option("--ha-only", is_flag=True, help="Only Home-Assistant-sourced appliances")
@click.pass_context
def devices_list(ctx, ha_only):
    """List every smart-home appliance Alexa knows about."""
    login = _login(ctx)
    rows = session_core.run_async(devices_core.list_appliances(login))
    if ha_only:
        rows = [r for r in rows if r.get("ha_sourced")]
    emit(ctx, rows)


@devices.command("prune")
@click.option("--whitelist", "whitelist_file", required=True,
              type=click.Path(exists=True, dir_okay=False),
              help="File of allowed HA entity ids (one per line)")
@click.option("--dry-run/--no-dry-run", default=True,
              help="Preview only (default). --no-dry-run + --yes to execute.")
@click.option("--yes", is_flag=True, default=False,
              help="Required to actually DELETE (guards live mutation)")
@click.pass_context
def devices_prune(ctx, whitelist_file, dry_run, yes):
    """Delete HA-sourced appliances whose mapped entity isn't whitelisted.

    Non-HA appliances (Hue/Wemo/etc.) are never touched. Dry-run by default;
    pass --no-dry-run --yes to execute the deletes.
    """
    login = _login(ctx)
    whitelist = appliances_pure.load_whitelist(Path(whitelist_file).read_text())
    raw = session_core.run_async(devices_core.fetch_appliances(login))
    plan = appliances_pure.plan_prune(raw, whitelist)

    execute = (not dry_run) and yes
    summary = {
        "dry_run": not execute,
        "whitelist_size": len(whitelist),
        "to_delete": len(plan["delete"]),
        "to_keep": len(plan["keep"]),
        "skipped_non_ha": len(plan["skipped"]),
    }
    if not execute:
        if ctx.obj.get("as_json"):
            emit(ctx, {**summary, "delete": plan["delete"]})
        else:
            emit(ctx, summary)
            if plan["delete"]:
                click.echo("\nWould delete:")
                click.echo(render_table(plan["delete"]))
                click.echo("\nRe-run with --no-dry-run --yes to execute.")
        return

    results = []
    for row in plan["delete"]:
        res = session_core.run_async(
            devices_core.delete_appliance(login, row["applianceId"])
        )
        results.append(res)
    emit(ctx, {**summary, "results": results})


@devices.command("delete")
@click.argument("appliance_ids", nargs=-1, required=True)
@click.option("--yes", is_flag=True, default=False,
              help="Required to actually delete (guards live mutation)")
@click.pass_context
def devices_delete(ctx, appliance_ids, yes):
    """Delete one or more appliances by applianceId."""
    login = _login(ctx)
    if not yes:
        emit(ctx, {
            "dry_run": True,
            "would_delete": list(appliance_ids),
            "hint": "re-run with --yes to execute",
        })
        return
    results = [
        session_core.run_async(devices_core.delete_appliance(login, aid))
        for aid in appliance_ids
    ]
    emit(ctx, results)


# ──────────────────────────────────────────────────────── echo devices

@cli.group("echos")
def echos():
    """Physical Echo devices (announce/dnd/routine targets)."""


@echos.command("list")
@click.pass_context
def echos_list(ctx):
    """List the Echo speakers on the account."""
    login = _login(ctx)
    raw = session_core.run_async(devices_meta_core.fetch_devices(login))
    emit(ctx, devices_meta_core.device_rows(raw))


# ──────────────────────────────────────────────────────── groups

@cli.group()
def groups():
    """Smart-home device-groups / rooms — list / create / add / remove / set / delete."""


def _resolve_group_members(ctx, login, entities, endpoints):
    """Resolve --entity + --endpoint to endpoint ids; abort on any unresolved."""
    ent_map = {}
    if entities:
        ent_map = session_core.run_async(groups_core.fetch_endpoint_map(login))
    member_ids, unresolved = groups_core.resolve_members(
        list(entities), list(endpoints), ent_map
    )
    if unresolved:
        _abort(
            "could not resolve these entities to Alexa endpoints "
            f"(not exposed to Alexa?): {', '.join(unresolved)}"
        )
    if not member_ids:
        _abort("no members given — pass at least one --entity or --endpoint")
    return member_ids


def _find_group_or_abort(ctx, login, name_or_id):
    """Fetch groups and resolve a name/id to a raw group record, or abort."""
    raw = session_core.run_async(groups_core.fetch_groups(login))
    g = groups_core.find_group(raw, name_or_id)
    if not g:
        _abort(f"no group matching {name_or_id!r}")
    return g


@groups.command("list")
@click.pass_context
def groups_list(ctx):
    """List Alexa smart-home device-groups (name, id, member count/names)."""
    login = _login(ctx)
    emit(ctx, session_core.run_async(groups_core.list_groups(login)))


@groups.command("create")
@click.argument("name")
@click.option("--entity", "entities", multiple=True,
              help="HA entity id to add as a member (repeatable)")
@click.option("--endpoint", "endpoints", multiple=True,
              help="Alexa endpoint id (amzn1.alexa.endpoint.*) to add (repeatable)")
@click.option("--yes", is_flag=True, default=False,
              help="Required to actually create (guards live mutation)")
@click.pass_context
def groups_create(ctx, name, entities, endpoints, yes):
    """Create a device-group with the given members (dry-run unless --yes)."""
    login = _login(ctx)
    member_ids = _resolve_group_members(ctx, login, entities, endpoints)
    if not yes:
        emit(ctx, {"dry_run": True, "would_create": name,
                   "memberDeviceIds": member_ids,
                   "hint": "re-run with --yes to execute"})
        return
    emit(ctx, session_core.run_async(
        groups_core.create_group(login, name, member_ids)))


def _groups_member_update(ctx, group, entities, endpoints, operation, yes):
    """Shared add/remove/set body: resolve members + updateDeviceGroup."""
    login = _login(ctx)
    g = _find_group_or_abort(ctx, login, group)
    gid = g.get("id")
    member_ids = _resolve_group_members(ctx, login, entities, endpoints)
    if not yes:
        emit(ctx, {"dry_run": True, "group": group, "deviceGroupId": gid,
                   "operation": operation, "memberDeviceIds": member_ids,
                   "hint": "re-run with --yes to execute"})
        return
    emit(ctx, session_core.run_async(
        groups_core.update_group(login, gid, member_ids, operation)))


@groups.command("add")
@click.argument("group")
@click.option("--entity", "entities", multiple=True, help="HA entity id (repeatable)")
@click.option("--endpoint", "endpoints", multiple=True,
              help="Alexa endpoint id (repeatable)")
@click.option("--yes", is_flag=True, default=False, help="Required to execute")
@click.pass_context
def groups_add(ctx, group, entities, endpoints, yes):
    """Add members to a group (by name or id)."""
    _groups_member_update(ctx, group, entities, endpoints, "ADD", yes)


@groups.command("remove")
@click.argument("group")
@click.option("--entity", "entities", multiple=True, help="HA entity id (repeatable)")
@click.option("--endpoint", "endpoints", multiple=True,
              help="Alexa endpoint id (repeatable)")
@click.option("--yes", is_flag=True, default=False, help="Required to execute")
@click.pass_context
def groups_remove(ctx, group, entities, endpoints, yes):
    """Remove members from a group (by name or id)."""
    _groups_member_update(ctx, group, entities, endpoints, "REMOVE", yes)


@groups.command("set")
@click.argument("group")
@click.option("--entity", "entities", multiple=True, help="HA entity id (repeatable)")
@click.option("--endpoint", "endpoints", multiple=True,
              help="Alexa endpoint id (repeatable)")
@click.option("--yes", is_flag=True, default=False, help="Required to execute")
@click.pass_context
def groups_set(ctx, group, entities, endpoints, yes):
    """Replace a group's entire member set (by name or id)."""
    _groups_member_update(ctx, group, entities, endpoints, "REPLACE", yes)


@groups.command("delete")
@click.argument("group")
@click.option("--yes", is_flag=True, default=False,
              help="Required to actually delete (guards live mutation)")
@click.pass_context
def groups_delete(ctx, group, yes):
    """Delete a device-group (by name or id; dry-run unless --yes)."""
    login = _login(ctx)
    g = _find_group_or_abort(ctx, login, group)
    gid = g.get("id")
    name = (((g.get("friendlyName") or {}).get("value") or {}).get("text"))
    if not yes:
        emit(ctx, {"dry_run": True, "would_delete": name or group,
                   "deviceGroupId": gid,
                   "hint": "re-run with --yes to execute"})
        return
    emit(ctx, session_core.run_async(groups_core.delete_group(login, gid)))


# ──────────────────────────────────────────────────────── routines

@cli.group()
def routines():
    """Alexa routines (behaviors) — list / run."""


@routines.command("list")
@click.pass_context
def routines_list(ctx):
    login = _login(ctx)
    emit(ctx, session_core.run_async(routines_core.list_routines(login)))


@routines.command("run")
@click.argument("name_or_id")
@click.option("--yes", is_flag=True, default=False,
              help="Required to actually trigger (guards live mutation)")
@click.pass_context
def routines_run(ctx, name_or_id, yes):
    """Trigger a routine by name or id (via behaviors/preview)."""
    login = _login(ctx)
    if not yes:
        emit(ctx, {"dry_run": True, "would_run": name_or_id,
                   "hint": "re-run with --yes to execute"})
        return
    try:
        emit(ctx, session_core.run_async(routines_core.run_routine(login, name_or_id)))
    except ValueError as exc:
        _abort(str(exc))


# ──────────────────────────────────────────────────────── notifications

@cli.group()
def notifications():
    """Alarms / timers / reminders — list / add / delete."""


@notifications.command("list")
@click.pass_context
def notifications_list(ctx):
    login = _login(ctx)
    emit(ctx, session_core.run_async(notifications_core.list_notifications(login)))


@notifications.command("add-reminder")
@click.argument("label")
@click.option("--device", required=True, help="Echo accountName or serial")
@click.option("--in", "in_seconds", type=float, default=None,
              help="Seconds from now")
@click.option("--at", "at_epoch_ms", type=int, default=None,
              help="Absolute epoch milliseconds")
@click.option("--yes", is_flag=True, default=False, help="Required to execute")
@click.pass_context
def notifications_add_reminder(ctx, label, device, in_seconds, at_epoch_ms, yes):
    """Create a reminder on a device."""
    login = _login(ctx)
    raw = session_core.run_async(devices_meta_core.fetch_devices(login))
    d = devices_meta_core.find_device(raw, device)
    if not d:
        _abort(f"no device matching {device!r}")
    when = notifications_core._epoch_ms(in_seconds, at_epoch_ms)
    payload = notifications_core.build_reminder(
        label, d["serialNumber"], d["deviceType"], when
    )
    if not yes:
        emit(ctx, {"dry_run": True, "payload": payload,
                   "hint": "re-run with --yes to execute"})
        return
    emit(ctx, session_core.run_async(
        notifications_core.create_notification(login, payload)))


@notifications.command("add-alarm")
@click.option("--device", required=True)
@click.option("--in", "in_seconds", type=float, default=None)
@click.option("--at", "at_epoch_ms", type=int, default=None)
@click.option("--label", default="")
@click.option("--yes", is_flag=True, default=False, help="Required to execute")
@click.pass_context
def notifications_add_alarm(ctx, device, in_seconds, at_epoch_ms, label, yes):
    """Create an alarm on a device."""
    login = _login(ctx)
    raw = session_core.run_async(devices_meta_core.fetch_devices(login))
    d = devices_meta_core.find_device(raw, device)
    if not d:
        _abort(f"no device matching {device!r}")
    when = notifications_core._epoch_ms(in_seconds, at_epoch_ms)
    payload = notifications_core.build_alarm(
        d["serialNumber"], d["deviceType"], when, label=label
    )
    if not yes:
        emit(ctx, {"dry_run": True, "payload": payload,
                   "hint": "re-run with --yes to execute"})
        return
    emit(ctx, session_core.run_async(
        notifications_core.create_notification(login, payload)))


@notifications.command("add-timer")
@click.option("--device", required=True)
@click.option("--duration", "duration_seconds", type=float, required=True,
              help="Timer duration in seconds")
@click.option("--label", default="")
@click.option("--yes", is_flag=True, default=False, help="Required to execute")
@click.pass_context
def notifications_add_timer(ctx, device, duration_seconds, label, yes):
    """Create a timer on a device."""
    login = _login(ctx)
    raw = session_core.run_async(devices_meta_core.fetch_devices(login))
    d = devices_meta_core.find_device(raw, device)
    if not d:
        _abort(f"no device matching {device!r}")
    payload = notifications_core.build_timer(
        d["serialNumber"], d["deviceType"], int(duration_seconds * 1000), label=label
    )
    if not yes:
        emit(ctx, {"dry_run": True, "payload": payload,
                   "hint": "re-run with --yes to execute"})
        return
    emit(ctx, session_core.run_async(
        notifications_core.create_notification(login, payload)))


@notifications.command("delete")
@click.argument("notification_id")
@click.option("--yes", is_flag=True, default=False, help="Required to execute")
@click.pass_context
def notifications_delete(ctx, notification_id, yes):
    """Delete a notification by id."""
    login = _login(ctx)
    if not yes:
        emit(ctx, {"dry_run": True, "would_delete": notification_id,
                   "hint": "re-run with --yes to execute"})
        return
    emit(ctx, session_core.run_async(
        notifications_core.delete_notification(login, notification_id)))


# ──────────────────────────────────────────────────────── announce / dnd

@cli.command("announce")
@click.argument("text")
@click.option("--device", default=None,
              help="Echo accountName/serial (default: all devices)")
@click.option("--yes", is_flag=True, default=False, help="Required to execute")
@click.pass_context
def announce_cmd(ctx, text, device, yes):
    """Speak an announcement on all devices (or one named target)."""
    login = _login(ctx)
    if not yes:
        emit(ctx, {"dry_run": True, "would_announce": text,
                   "device": device or "all",
                   "hint": "re-run with --yes to execute"})
        return
    try:
        emit(ctx, session_core.run_async(control_core.announce(login, text, device)))
    except ValueError as exc:
        _abort(str(exc))


@cli.command("dnd")
@click.argument("device")
@click.argument("state", type=click.Choice(["on", "off"]))
@click.option("--yes", is_flag=True, default=False, help="Required to execute")
@click.pass_context
def dnd_cmd(ctx, device, state, yes):
    """Toggle Do-Not-Disturb for a device."""
    login = _login(ctx)
    if not yes:
        emit(ctx, {"dry_run": True, "device": device, "dnd": state,
                   "hint": "re-run with --yes to execute"})
        return
    try:
        emit(ctx, session_core.run_async(
            control_core.set_dnd(login, device, state == "on")))
    except ValueError as exc:
        _abort(str(exc))


# ──────────────────────────────────────────────────────── REPL

@cli.command()
@click.pass_context
def repl(ctx):
    """Start an interactive shell."""
    try:
        from cli_anything.alexa.utils.repl_skin import ReplSkin
    except ImportError:
        click.echo("REPL requires prompt-toolkit. pip install prompt-toolkit", err=True)
        return
    skin = ReplSkin("alexa", version=__version__)
    skin.print_banner()
    pt_session = skin.create_prompt_session()
    while True:
        try:
            line = skin.get_input(pt_session)
        except (EOFError, KeyboardInterrupt):
            skin.print_goodbye()
            break
        line = (line or "").strip()
        if not line:
            continue
        if line in ("exit", "quit"):
            skin.print_goodbye()
            break
        if line == "help":
            skin.help({k: (v.help or "") for k, v in cli.commands.items()})
            continue
        argv = shlex.split(line)
        try:
            cli.main(args=argv, standalone_mode=False, prog_name="(alexa)", obj=ctx.obj)
        except SystemExit:
            pass
        except Exception as exc:  # noqa: BLE001
            skin.error(str(exc))


def main():
    cli(obj={})


if __name__ == "__main__":
    main()
