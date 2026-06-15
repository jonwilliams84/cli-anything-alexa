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
from cli_anything.alexa.core import endpoints as endpoints_core
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
        return "0.2.0+unknown"


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
                config_dir=ctx.obj.get("cookie_dir", session_core.DEFAULT_CONFIG_DIR),
                create_dir=not ctx.obj.get("read_in_place", False),
            )
        )
    except session_core.AlexaSessionError as exc:
        _abort(str(exc))
    except Exception as exc:  # noqa: BLE001 - never leak a raw traceback
        _abort(
            f"could not establish an Alexa session ({type(exc).__name__}: {exc}). "
            "Run `cli-anything-alexa auth login` to (re)authenticate."
        )


def _run(ctx, coro):
    """Run a live-call coroutine, turning network/API errors into a friendly
    abort instead of a raw traceback. ``ValueError`` (caller-facing messages
    raised by the core modules) is surfaced verbatim."""
    try:
        return session_core.run_async(coro)
    except session_core.AlexaSessionError as exc:
        _abort(str(exc))
    except ValueError as exc:
        _abort(str(exc))
    except Exception as exc:  # noqa: BLE001 - friendly, never a traceback
        _abort(
            f"the Alexa request failed ({type(exc).__name__}: {exc}). "
            "If this persists, re-authenticate with `auth login` — the saved "
            "session may have expired."
        )


# ──────────────────────────────────────────────────────── root

@click.group(context_settings=CONTEXT_SETTINGS, invoke_without_command=True)
@click.option("--email", default=None, help="Amazon account email")
@click.option("--url", default=None, help="Account region host (default amazon.co.uk)")
@click.option("--config", "config_path", default=None, type=click.Path(),
              help="Profile path (default ~/.config/cli-anything-alexa/config.json)")
@click.option("--cookie-dir", "cookie_dir", default=None, envvar="CLI_ALEXA_COOKIE_DIR",
              help="Read/write the cookie at this dir IN PLACE (HA layout: "
                   "<dir>/.storage/alexa_media.<email>.pickle). Point it at HA's "
                   "config base (e.g. /config) to reuse HA's LIVE rotating "
                   "cookie. Env: CLI_ALEXA_COOKIE_DIR.")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit machine-readable JSON output")
@click.version_option(version=__version__, prog_name="cli-anything-alexa")
@click.pass_context
def cli(ctx, email, url, config_path, cookie_dir, as_json):
    """cli-anything-alexa — Amazon Alexa management over the unofficial web API."""
    ctx.ensure_object(dict)
    cfg_path_obj = Path(config_path).expanduser() if config_path else None
    cfg = project.load_config(cfg_path_obj)
    cfg = project.merge_cli_overrides(cfg, email=email, url=url)
    ctx.obj.update(cfg)
    ctx.obj["as_json"] = as_json
    ctx.obj["config_path"] = cfg_path_obj
    # Resolve the cookie/config dir ONCE (flag > env > $HOME > /tmp fallback)
    # so write (import-pickle) and read (status / live calls) always agree.
    # read_in_place: when --cookie-dir / env is set we read HA's live cookie at
    # that location and never create/copy into it.
    ctx.obj["read_in_place"] = bool(cookie_dir)
    ctx.obj["cookie_dir"] = session_core.resolve_config_dir(cookie_dir)
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

    Copies the cookie into the resolved config dir (``--cookie-dir`` > env >
    $HOME/.config/cli-anything-alexa > /tmp fallback) under the name alexapy
    expects, so later commands reuse the session with no MFA.

    \b
    HEADS-UP: this is a one-time SNAPSHOT. If Home Assistant is actively using
    the same account it rotates the cookie constantly, so the copy goes stale
    within seconds (auth flips logged_in true->false mid-session). For HA reuse
    prefer reading HA's LIVE cookie in place:
      cli-anything-alexa --cookie-dir /config auth status
    Use import-pickle for a standalone copy you keep fresh via `auth login`.
    """
    em = email or ctx.obj.get("email")
    if not em:
        _abort("need --email or a configured email to name the cookie")
    if ctx.obj.get("read_in_place"):
        _abort(
            "--cookie-dir reads the cookie IN PLACE — copying with "
            "import-pickle would be pointless (and goes stale). Just run "
            f"`--cookie-dir {ctx.obj.get('cookie_dir')} auth status` to use "
            "the live cookie there directly."
        )
    config_dir = ctx.obj.get("cookie_dir", session_core.DEFAULT_CONFIG_DIR)
    try:
        dest = session_core.import_pickle(pickle_path, em, config_dir=config_dir)
    except session_core.AlexaSessionError as exc:
        _abort(str(exc))
    # persist the email into the profile for convenience
    cfg = dict(ctx.obj)
    cfg["email"] = em
    project.save_config(cfg, ctx.obj.get("config_path"))
    ok = session_core.run_async(
        session_core.test_loggedin(
            em, url=ctx.obj.get("url", "amazon.co.uk"), config_dir=config_dir
        )
    )
    emit(ctx, {"imported": str(dest), "email": em, "logged_in": ok})


@auth.command("login")
@click.option("--email", default=None, help="Amazon account email (prompted if omitted)")
@click.option("--url", "region", default=None,
              help="Account region host, e.g. amazon.co.uk / amazon.com / amazon.de")
@click.option("--password", default=None,
              help="Password — switches to the SCRIPTED (headless/CI) login")
@click.option("--otp-secret", default=None,
              help="Base32 TOTP secret for the scripted login's 2FA (headless)")
@click.option("--host", default=None,
              help=f"Proxy bind host (default {session_core.DEFAULT_PROXY_HOST}; "
                   "use 0.0.0.0 to log in from another machine)")
@click.option("--port", type=int, default=None,
              help=f"Proxy port (default {session_core.DEFAULT_PROXY_PORT})")
@click.option("--timeout", type=float, default=600.0,
              help="Seconds to wait for the browser login (proxy flow)")
@click.pass_context
def auth_login(ctx, email, region, password, otp_secret, host, port, timeout):
    """Log in to Amazon. Guided browser-proxy login by default (recommended).

    \b
    The default flow needs NO Home Assistant and handles captcha / 2FA
    natively because you complete Amazon's own login pages in a browser:
      1. it starts a tiny local web proxy and prints a URL,
      2. you open that URL and log in to Amazon as normal,
      3. on success the session cookie is saved locally — done.

    For headless / CI, pass --password (and --otp-secret for 2FA) to use the
    scripted login instead. Existing HA users can also `auth import-pickle`.
    """
    em = email or ctx.obj.get("email")
    as_json = ctx.obj.get("as_json")
    if not em:
        if as_json:
            _abort("--email is required with --json")
        em = click.prompt("Amazon account email")

    region = region or ctx.obj.get("url") or "amazon.co.uk"
    if not as_json and region == "amazon.co.uk" and not (ctx.obj.get("url")):
        region = click.prompt("Account region host", default="amazon.co.uk")

    def _persist():
        cfg = dict(ctx.obj)
        cfg["email"] = em
        cfg["url"] = region
        project.save_config(cfg, ctx.obj.get("config_path"))

    # ── Scripted (headless/CI) login: only when a password is supplied ──
    if password is not None:
        def otp_cb():
            return click.prompt("OTP / 2FA code")

        try:
            _run(ctx,
                session_core.fresh_login(
                    em, password, url=region,
                    config_dir=ctx.obj.get(
                        "cookie_dir", session_core.DEFAULT_CONFIG_DIR),
                    otp_secret=otp_secret or "",
                    otp_callback=None if otp_secret else otp_cb,
                )
            )
        except session_core.AlexaSessionError as exc:
            _abort(str(exc))
        _persist()
        emit(ctx, {"logged_in": True, "email": em, "method": "scripted"})
        return

    # ── Guided proxy browser login (default, recommended) ──
    pport = port if port is not None else session_core.DEFAULT_PROXY_PORT
    phost = host if host is not None else session_core.DEFAULT_PROXY_HOST

    def on_url(access_url):
        if as_json:
            return
        click.echo("")
        click.echo("Browser login — three steps:")
        click.echo(f"  1. Open this URL in a browser:  {access_url}")
        click.echo("  2. Sign in to Amazon as you normally would (captcha / 2FA")
        click.echo("     are handled by Amazon's own pages).")
        click.echo('  3. When it says you can close the window, you are done.')
        if phost == "0.0.0.0":
            click.echo("")
            click.echo(f"  (bound to 0.0.0.0 — from another machine open "
                       f"http://<this-host>:{pport} )")
        click.echo("")
        click.echo("Waiting for login to complete... (Ctrl-C to cancel)")

    try:
        login = session_core.run_async(
            session_core.proxy_login(
                em, url=region,
                config_dir=ctx.obj.get(
                    "cookie_dir", session_core.DEFAULT_CONFIG_DIR),
                host=phost, port=pport,
                timeout=timeout, on_url=on_url,
            )
        )
    except KeyboardInterrupt:
        _abort("login cancelled.")
    except session_core.AlexaSessionError as exc:
        _abort(str(exc))
    except OSError as exc:
        _abort(
            f"could not start the login proxy on {phost}:{pport} ({exc}). "
            "Try a different --port, or --host 0.0.0.0 for a remote box."
        )
    _persist()
    if as_json:
        emit(ctx, {"logged_in": True, "email": em, "method": "proxy"})
    else:
        click.echo(f"Logged in as {em} ({region}). You're all set — try "
                   "`cli-anything-alexa devices list`.")


@auth.command("status")
@click.pass_context
def auth_status(ctx):
    """Validate the saved cookie (test_loggedin)."""
    email = _require_email(ctx)
    ok = session_core.run_async(
        session_core.test_loggedin(
            email, url=ctx.obj.get("url", "amazon.co.uk"),
            config_dir=ctx.obj.get("cookie_dir", session_core.DEFAULT_CONFIG_DIR),
            create_dir=not ctx.obj.get("read_in_place", False),
        )
    )
    emit(ctx, {"email": email, "logged_in": ok})
    if not ok:
        sys.exit(1)


# ──────────────────────────────────────────────────────── devices (appliances)

@cli.group()
def devices():
    """Smart-home appliances — list / prune / delete / rename / duplicates."""


def _resolve_one_or_abort(ctx, records, matches, what):
    """Return the single matched record, or abort.

    0 matches -> "no device matching"; >1 -> ambiguity abort listing the
    candidates so the user can disambiguate (a native + HA twin can share a
    name). ``records`` is unused but kept for signature symmetry.
    """
    if not matches:
        _abort(f"no device matching {what!r}")
    if len(matches) > 1:
        cands = endpoints_core.ambiguous_matches(matches)
        if ctx.obj.get("as_json"):
            click.echo(json.dumps(
                {"error": "ambiguous", "target": what, "matches": cands},
                indent=2, default=str, sort_keys=True), err=True)
        else:
            click.echo(f"error: {what!r} matches {len(matches)} devices — "
                       "disambiguate by applianceId or endpoint id:", err=True)
            click.echo(render_table(cands), err=True)
        sys.exit(1)
    return matches[0]


@devices.command("list")
@click.option("--ha-only", is_flag=True, help="Only Home-Assistant-sourced appliances")
@click.option("--native-only", is_flag=True, help="Only native (non-HA) appliances")
@click.option("--manufacturer", default=None,
              help="Filter by manufacturer (case-insensitive substring)")
@click.pass_context
def devices_list(ctx, ha_only, native_only, manufacturer):
    """List every smart-home device Alexa knows about.

    Shows the manufacturer and a native-vs-HA ``source`` marker. ``enabled`` is
    the endpoint's enablement state (a true online/reachability column is not
    exposed cleanly by the API, so it is omitted). Filter with --ha-only /
    --native-only / --manufacturer.
    """
    if ha_only and native_only:
        _abort("--ha-only and --native-only are mutually exclusive")
    login = _login(ctx)
    records = _run(ctx, endpoints_core.fetch_endpoint_records(login))
    if ha_only:
        records = [r for r in records if r.get("ha_sourced")]
    rows = endpoints_core.device_rows(
        records, native_only=native_only, manufacturer=manufacturer
    )
    emit(ctx, rows)


@devices.command("rename")
@click.argument("target")
@click.argument("new_name")
@click.option("--yes", is_flag=True, default=False,
              help="Required to actually rename (guards live mutation)")
@click.pass_context
def devices_rename(ctx, target, new_name, yes):
    """Rename a device. TARGET resolves by applianceId, endpoint id, or name.

    Resolution precedence: exact applianceId -> exact endpoint id -> exact
    display name -> normalized/case-insensitive display name. If TARGET matches
    more than one device (a native + HA twin can share a name) the command
    aborts and lists the matches so you can disambiguate. Dry-run unless --yes.
    """
    login = _login(ctx)
    records = _run(ctx, endpoints_core.fetch_endpoint_records(login))
    matches = endpoints_core.resolve_target(records, target)
    rec = _resolve_one_or_abort(ctx, records, matches, target)
    eid = rec.get("endpointId")
    if not eid:
        _abort(f"resolved device for {target!r} has no endpoint id (cannot rename)")
    if not yes:
        emit(ctx, {"dry_run": True, "would_rename": rec.get("name"),
                   "to": new_name, "endpointId": eid,
                   "applianceId": rec.get("applianceId"),
                   "hint": "re-run with --yes to execute"})
        return
    emit(ctx, _run(ctx, endpoints_core.rename_endpoint(login, eid, new_name)))


@devices.command("duplicates")
@click.pass_context
def devices_duplicates(ctx):
    """Detect devices exposed twice (native + HA twin, or any shared name).

    Lists each display name shared by more than one endpoint, flagging the
    classic native+HA twin. Nothing is deleted — it's for a human to decide
    which copy to drop (then `devices delete`).
    """
    login = _login(ctx)
    records = _run(ctx, endpoints_core.fetch_endpoint_records(login))
    dups = endpoints_core.find_duplicates(records)
    if ctx.obj.get("as_json"):
        emit(ctx, dups)
        return
    if not dups:
        click.echo("no duplicate device names found")
        return
    for d in dups:
        tag = " [native+HA twin]" if d.get("native_plus_ha") else ""
        click.echo(f"\n{d['name']}  (x{d['count']}){tag}")
        click.echo(render_table(d["endpoints"]))


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
    raw = _run(ctx, devices_core.fetch_appliances(login))
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
        res = _run(ctx, 
            devices_core.delete_appliance(login, row["applianceId"])
        )
        results.append(res)
    emit(ctx, {**summary, "results": results})


@devices.command("delete")
@click.argument("appliance_ids", nargs=-1)
@click.option("--entity", "entity", default=None,
              help="Resolve the appliance to delete by HA entity id (ha.entity_id)")
@click.option("--name", "name", default=None,
              help="Resolve the appliance to delete by Alexa display name")
@click.option("--yes", is_flag=True, default=False,
              help="Required to actually delete (guards live mutation)")
@click.pass_context
def devices_delete(ctx, appliance_ids, entity, name, yes):
    """Delete appliances by applianceId, --entity <ha.id>, or --name "<display>".

    Positional applianceId(s) still work. --entity / --name resolve via the
    endpoints query to the applianceId; if a name matches more than one device
    (native + HA twin) the command aborts and lists the matches.
    """
    login = _login(ctx)
    targets = list(appliance_ids)
    if entity or name:
        records = _run(ctx, endpoints_core.fetch_endpoint_records(login))
        if entity:
            matches = endpoints_core.resolve_by_entity(records, entity)
            rec = _resolve_one_or_abort(ctx, records, matches, entity)
            targets.append(rec.get("applianceId"))
        if name:
            matches = endpoints_core.resolve_by_name(records, name)
            rec = _resolve_one_or_abort(ctx, records, matches, name)
            targets.append(rec.get("applianceId"))
    targets = [t for t in targets if t]
    if not targets:
        _abort("nothing to delete — pass an applianceId, --entity, or --name")
    if not yes:
        emit(ctx, {
            "dry_run": True,
            "would_delete": targets,
            "hint": "re-run with --yes to execute",
        })
        return
    results = [
        _run(ctx, devices_core.delete_appliance(login, aid))
        for aid in targets
    ]
    emit(ctx, results)


@cli.command("discover")
@click.option("--yes", is_flag=True, default=False,
              help="Required to actually trigger discovery (guards live mutation)")
@click.pass_context
def discover_cmd(ctx, yes):
    """Trigger Alexa smart-home device discovery (POST /api/phoenix/discovery)."""
    login = _login(ctx)
    if not yes:
        emit(ctx, {"dry_run": True, "would_trigger": "discovery",
                   "hint": "re-run with --yes to execute"})
        return
    emit(ctx, _run(ctx, devices_core.trigger_discovery(login)))


# ──────────────────────────────────────────────────────── echo devices

@cli.group("echos")
def echos():
    """Physical Echo devices (announce/dnd/routine targets)."""


@echos.command("list")
@click.pass_context
def echos_list(ctx):
    """List the Echo speakers on the account."""
    login = _login(ctx)
    raw = _run(ctx, devices_meta_core.fetch_devices(login))
    emit(ctx, devices_meta_core.device_rows(raw))


# ──────────────────────────────────────────────────────── groups

@cli.group()
def groups():
    """Smart-home device-groups / rooms — list / create / add / remove / set / delete."""


def _resolve_group_members(ctx, login, entities, endpoints, devices=()):
    """Resolve --entity + --endpoint + --device to endpoint ids; abort on errors.

    --device resolves a device by Alexa **display name** (normalized) to its
    endpoint id — this is how native / non-HA devices (e.g. Tasmota-Wemo plugs)
    that have no HA entity are targeted. A name matching more than one device
    aborts and lists the matches.
    """
    ent_map = {}
    if entities:
        ent_map = _run(ctx, groups_core.fetch_endpoint_map(login))
    member_ids, unresolved = groups_core.resolve_members(
        list(entities), list(endpoints), ent_map
    )
    if unresolved:
        _abort(
            "could not resolve these entities to Alexa endpoints "
            f"(not exposed to Alexa?): {', '.join(unresolved)}"
        )
    if devices:
        records = _run(ctx, endpoints_core.fetch_endpoint_records(login))
        for name in devices:
            matches = endpoints_core.resolve_by_name(records, name)
            rec = _resolve_one_or_abort(ctx, records, matches, name)
            eid = rec.get("endpointId")
            if eid and eid not in member_ids:
                member_ids.append(eid)
    if not member_ids:
        _abort("no members given — pass at least one --entity / --endpoint / --device")
    return member_ids


def _find_group_or_abort(ctx, login, name_or_id):
    """Fetch groups and resolve a name/id to a raw group record, or abort."""
    raw = _run(ctx, groups_core.fetch_groups(login))
    g = groups_core.find_group(raw, name_or_id)
    if not g:
        _abort(f"no group matching {name_or_id!r}")
    return g


@groups.command("list")
@click.pass_context
def groups_list(ctx):
    """List Alexa smart-home device-groups (name, id, member count/names)."""
    login = _login(ctx)
    emit(ctx, _run(ctx, groups_core.list_groups(login)))


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
    emit(ctx, _run(ctx, 
        groups_core.create_group(login, name, member_ids)))


def _groups_member_update(ctx, group, entities, endpoints, operation, yes, devices=()):
    """Shared add/remove/set body: resolve members + updateDeviceGroup."""
    login = _login(ctx)
    g = _find_group_or_abort(ctx, login, group)
    gid = g.get("id")
    member_ids = _resolve_group_members(ctx, login, entities, endpoints, devices)
    if not yes:
        emit(ctx, {"dry_run": True, "group": group, "deviceGroupId": gid,
                   "operation": operation, "memberDeviceIds": member_ids,
                   "hint": "re-run with --yes to execute"})
        return
    emit(ctx, _run(ctx, 
        groups_core.update_group(login, gid, member_ids, operation)))


@groups.command("add")
@click.argument("group")
@click.option("--entity", "entities", multiple=True, help="HA entity id (repeatable)")
@click.option("--endpoint", "endpoints", multiple=True,
              help="Alexa endpoint id (repeatable)")
@click.option("--device", "devices_", multiple=True,
              help="Alexa display name — targets native/non-HA devices (repeatable)")
@click.option("--yes", is_flag=True, default=False, help="Required to execute")
@click.pass_context
def groups_add(ctx, group, entities, endpoints, devices_, yes):
    """Add members to a group (by name or id)."""
    _groups_member_update(ctx, group, entities, endpoints, "ADD", yes, devices_)


@groups.command("remove")
@click.argument("group")
@click.option("--entity", "entities", multiple=True, help="HA entity id (repeatable)")
@click.option("--endpoint", "endpoints", multiple=True,
              help="Alexa endpoint id (repeatable)")
@click.option("--device", "devices_", multiple=True,
              help="Alexa display name — targets native/non-HA devices (repeatable)")
@click.option("--yes", is_flag=True, default=False, help="Required to execute")
@click.pass_context
def groups_remove(ctx, group, entities, endpoints, devices_, yes):
    """Remove members from a group (by name or id)."""
    _groups_member_update(ctx, group, entities, endpoints, "REMOVE", yes, devices_)


@groups.command("set")
@click.argument("group")
@click.option("--entity", "entities", multiple=True, help="HA entity id (repeatable)")
@click.option("--endpoint", "endpoints", multiple=True,
              help="Alexa endpoint id (repeatable)")
@click.option("--device", "devices_", multiple=True,
              help="Alexa display name — targets native/non-HA devices (repeatable)")
@click.option("--yes", is_flag=True, default=False, help="Required to execute")
@click.pass_context
def groups_set(ctx, group, entities, endpoints, devices_, yes):
    """Replace a group's entire member set (by name or id)."""
    _groups_member_update(ctx, group, entities, endpoints, "REPLACE", yes, devices_)


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
    emit(ctx, _run(ctx, groups_core.delete_group(login, gid)))


# ──────────────────────────────────────────────────────── routines

@cli.group()
def routines():
    """Alexa routines (behaviors) — list / run."""


@routines.command("list")
@click.pass_context
def routines_list(ctx):
    login = _login(ctx)
    emit(ctx, _run(ctx, routines_core.list_routines(login)))


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
        emit(ctx, _run(ctx, routines_core.run_routine(login, name_or_id)))
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
    emit(ctx, _run(ctx, notifications_core.list_notifications(login)))


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
    raw = _run(ctx, devices_meta_core.fetch_devices(login))
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
    emit(ctx, _run(ctx, 
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
    raw = _run(ctx, devices_meta_core.fetch_devices(login))
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
    emit(ctx, _run(ctx, 
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
    raw = _run(ctx, devices_meta_core.fetch_devices(login))
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
    emit(ctx, _run(ctx, 
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
    emit(ctx, _run(ctx, 
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
        emit(ctx, _run(ctx, control_core.announce(login, text, device)))
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
        emit(ctx, _run(ctx, 
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
