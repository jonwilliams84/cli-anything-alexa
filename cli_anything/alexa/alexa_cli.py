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
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path

import click

from cli_anything.alexa.core import activity as activity_core
from cli_anything.alexa.core import appliances as appliances_pure
from cli_anything.alexa.core import control as control_core
from cli_anything.alexa.core import devices as devices_core
from cli_anything.alexa.core import devices_meta as devices_meta_core
from cli_anything.alexa.core import endpoints as endpoints_core
from cli_anything.alexa.core import groups as groups_core
from cli_anything.alexa.core import media as media_core
from cli_anything.alexa.core import notifications as notifications_core
from cli_anything.alexa.core import project
from cli_anything.alexa.core import routines as routines_core
from cli_anything.alexa.core import sequences as sequences_core
from cli_anything.alexa.core import session as session_core
from cli_anything.alexa.core import smarthome as smarthome_core
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
                email,
                url=ctx.obj.get("url", "amazon.co.uk"),
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
@click.option(
    "--config",
    "config_path",
    default=None,
    type=click.Path(),
    help="Profile path (default ~/.config/cli-anything-alexa/config.json)",
)
@click.option(
    "--cookie-dir",
    "cookie_dir",
    default=None,
    envvar="CLI_ALEXA_COOKIE_DIR",
    help="Read/write the cookie at this dir IN PLACE (HA layout: "
    "<dir>/.storage/alexa_media.<email>.pickle). Point it at HA's "
    "config base (e.g. /config) to reuse HA's LIVE rotating "
    "cookie. Env: CLI_ALEXA_COOKIE_DIR.",
)
@click.option(
    "--json", "as_json", is_flag=True, default=False, help="Emit machine-readable JSON output"
)
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
    safe = {k: v for k, v in ctx.obj.items() if k not in ("config_path", "as_json")}
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
@click.option("--email", default=None, help="Override the account email (else uses the profile's)")
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
@click.option(
    "--url",
    "region",
    default=None,
    help="Account region host, e.g. amazon.co.uk / amazon.com / amazon.de",
)
@click.option(
    "--password", default=None, help="Password — switches to the SCRIPTED (headless/CI) login"
)
@click.option(
    "--otp-secret", default=None, help="Base32 TOTP secret for the scripted login's 2FA (headless)"
)
@click.option(
    "--host",
    default=None,
    help=f"Proxy bind host (default {session_core.DEFAULT_PROXY_HOST}; "
    "use 0.0.0.0 to log in from another machine)",
)
@click.option(
    "--port", type=int, default=None, help=f"Proxy port (default {session_core.DEFAULT_PROXY_PORT})"
)
@click.option(
    "--timeout",
    type=float,
    default=600.0,
    help="Seconds to wait for the browser login (proxy flow)",
)
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
            _run(
                ctx,
                session_core.fresh_login(
                    em,
                    password,
                    url=region,
                    config_dir=ctx.obj.get("cookie_dir", session_core.DEFAULT_CONFIG_DIR),
                    otp_secret=otp_secret or "",
                    otp_callback=None if otp_secret else otp_cb,
                ),
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
        click.echo("  3. When it says you can close the window, you are done.")
        if phost == session_core.BIND_ALL_HOST:
            click.echo("")
            click.echo(
                f"  (bound to 0.0.0.0 — from another machine open http://<this-host>:{pport} )"
            )
        click.echo("")
        click.echo("Waiting for login to complete... (Ctrl-C to cancel)")

    try:
        session_core.run_async(
            session_core.proxy_login(
                em,
                url=region,
                config_dir=ctx.obj.get("cookie_dir", session_core.DEFAULT_CONFIG_DIR),
                host=phost,
                port=pport,
                timeout=timeout,
                on_url=on_url,
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
        click.echo(
            f"Logged in as {em} ({region}). You're all set — try `cli-anything-alexa devices list`."
        )


@auth.command("status")
@click.pass_context
def auth_status(ctx):
    """Validate the saved cookie (test_loggedin)."""
    email = _require_email(ctx)
    ok = session_core.run_async(
        session_core.test_loggedin(
            email,
            url=ctx.obj.get("url", "amazon.co.uk"),
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
            click.echo(
                json.dumps(
                    {"error": "ambiguous", "target": what, "matches": cands},
                    indent=2,
                    default=str,
                    sort_keys=True,
                ),
                err=True,
            )
        else:
            click.echo(
                f"error: {what!r} matches {len(matches)} devices — "
                "disambiguate by applianceId or endpoint id:",
                err=True,
            )
            click.echo(render_table(cands), err=True)
        sys.exit(1)
    return matches[0]


@devices.command("list")
@click.option("--ha-only", is_flag=True, help="Only Home-Assistant-sourced appliances")
@click.option("--native-only", is_flag=True, help="Only native (non-HA) appliances")
@click.option(
    "--manufacturer", default=None, help="Filter by manufacturer (case-insensitive substring)"
)
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
    rows = endpoints_core.device_rows(records, native_only=native_only, manufacturer=manufacturer)
    emit(ctx, rows)


def _emit_bulk_rename_preview(ctx, planned, mode):
    """Dry-run preview for a bulk rename plan (pattern or map)."""
    if ctx.obj.get("as_json"):
        emit(
            ctx,
            {
                "dry_run": True,
                "mode": mode,
                "count": len(planned),
                "renames": planned,
                "hint": "re-run with --yes to execute",
            },
        )
        return
    if not planned:
        click.echo(f"no devices matched {mode} — nothing to rename")
        return
    click.echo(f"Dry-run: {len(planned)} rename(s) planned ({mode}).")
    click.echo(
        render_table([{"old": p["old"], "new": p["new"], "source": p["source"]} for p in planned])
    )
    warned = [p for p in planned if p.get("warning")]
    if warned:
        click.echo(
            "\nDACS warnings (Amazon may reject these non-speakable names; "
            "re-run with --speakable to auto-fix):"
        )
        for p in warned:
            click.echo(f"  - {p['warning']}")
    click.echo("\nRe-run with --yes to execute.")


@devices.command("rename")
@click.argument("target", required=False)
@click.argument("new_name", required=False)
@click.option(
    "--pattern",
    "pattern",
    default=None,
    help="Bulk rename via a sed-style s/REGEX/REPL/[ig] applied to "
    "EVERY device name (capture groups with \\1).",
)
@click.option(
    "--map",
    "map_file",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Bulk rename from a file of 'current name => new name' "
    "(or 'endpointId => new name') lines; # comments allowed.",
)
@click.option(
    "--speakable",
    is_flag=True,
    default=False,
    help="Auto-transform each new name into a DACS-speakable form "
    "(hyphens->spaces, strip control chars).",
)
@click.option(
    "--yes", is_flag=True, default=False, help="Required to actually rename (guards live mutation)"
)
@click.pass_context
def devices_rename(ctx, target, new_name, pattern, map_file, speakable, yes):
    """Rename device(s). Single TARGET NEW_NAME, or bulk --pattern / --map.

    \b
    Single: TARGET resolves by applianceId -> endpoint id -> exact name ->
    normalized name (ambiguous match aborts and lists candidates).
    Bulk --pattern 's/REGEX/REPL/': applies the sed substitution to every
    device's current name; changed names form the rename set.
    Bulk --map <file>: 'current name => new name' lines.

    All modes are DRY-RUN by default (preview table); pass --yes to execute.
    Amazon's rename API rejects non-speakable names (e.g. hyphens) via DACS —
    --speakable auto-fixes them; otherwise non-speakable names are warned about.
    """
    if sum(bool(x) for x in (pattern, map_file)) > 1:
        _abort("--pattern and --map are mutually exclusive")
    if (pattern or map_file) and (target or new_name):
        _abort("bulk --pattern / --map take no TARGET / NEW_NAME arguments")

    login = _login(ctx)
    records = _run(ctx, endpoints_core.fetch_endpoint_records(login))

    # ── bulk: --pattern ──
    if pattern:
        try:
            planned = endpoints_core.plan_pattern_renames(records, pattern, speakable=speakable)
        except endpoints_core.PatternError as exc:
            _abort(str(exc))
        if not yes:
            _emit_bulk_rename_preview(ctx, planned, f"pattern {pattern!r}")
            return
        emit(ctx, _run(ctx, endpoints_core.apply_renames(login, planned)))
        return

    # ── bulk: --map ──
    if map_file:
        try:
            pairs = endpoints_core.parse_rename_map(Path(map_file).read_text())
        except ValueError as exc:
            _abort(str(exc))
        planned, problems = endpoints_core.plan_map_renames(records, pairs, speakable=speakable)
        if problems:
            if ctx.obj.get("as_json"):
                click.echo(
                    json.dumps(
                        {"error": "unresolved map entries", "problems": problems},
                        indent=2,
                        default=str,
                        sort_keys=True,
                    ),
                    err=True,
                )
            else:
                click.echo(
                    "error: these --map targets did not resolve to exactly one device:", err=True
                )
                click.echo(render_table(problems), err=True)
            sys.exit(1)
        if not yes:
            _emit_bulk_rename_preview(ctx, planned, "map")
            return
        emit(ctx, _run(ctx, endpoints_core.apply_renames(login, planned)))
        return

    # ── single ──
    if not target or not new_name:
        _abort("give TARGET NEW_NAME, or a bulk --pattern / --map")
    final_name = endpoints_core.speakable_name(new_name) if speakable else new_name
    matches = endpoints_core.resolve_target(records, target)
    rec = _resolve_one_or_abort(ctx, records, matches, target)
    eid = rec.get("endpointId")
    if not eid:
        _abort(f"resolved device for {target!r} has no endpoint id (cannot rename)")
    if not yes:
        out = {
            "dry_run": True,
            "would_rename": rec.get("name"),
            "to": final_name,
            "endpointId": eid,
            "applianceId": rec.get("applianceId"),
            "hint": "re-run with --yes to execute",
        }
        warn = endpoints_core.speakable_warning(final_name)
        if warn:
            out["warning"] = warn
        emit(ctx, out)
        return
    emit(ctx, _run(ctx, endpoints_core.rename_endpoint(login, eid, final_name)))


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


def _resolve_targets_or_abort(ctx, records, targets):
    """Resolve every positional target to exactly one record (abort otherwise)."""
    resolved = []
    for target in targets:
        matches = endpoints_core.resolve_target(records, target)
        resolved.append(_resolve_one_or_abort(ctx, records, matches, target))
    return resolved


def _select_records(ctx, records, targets, all_devices):
    """The record set a state/control command should act on, or abort."""
    if all_devices and targets:
        _abort("--all cannot be combined with explicit device targets")
    if all_devices:
        return list(records)
    if not targets:
        _abort("name at least one device, or pass --all")
    return _resolve_targets_or_abort(ctx, records, targets)


@devices.command("state")
@click.argument("targets", nargs=-1)
@click.option("--all", "all_devices", is_flag=True, default=False, help="Read every device")
@click.pass_context
def devices_state(ctx, targets, all_devices):
    """Read live capability state (power/brightness/colour/temperature).

    TARGET is anything `devices list` shows — display name, applianceId or
    endpoint id — and may be repeated. Read-only, so no --yes.

    \b
    Devices Alexa could not reach come back under `errors` rather than silently
    missing, and devices with no phoenix entityId are reported as `skipped`.
    """
    login = _login(ctx)
    records = _run(ctx, endpoints_core.fetch_endpoint_records(login))
    selected = _select_records(ctx, records, targets, all_devices)
    result = _run(ctx, smarthome_core.read_states(login, selected))
    if ctx.obj.get("as_json"):
        emit(ctx, result)
        return
    emit(ctx, result["states"])
    for err in result["errors"]:
        click.echo(f"error: {err.get('entityId')}: {err.get('code')}", err=True)
    for name in result["skipped"]:
        click.echo(f"warning: {name!r} has no phoenix entityId — state unavailable", err=True)


def _power_command(ctx, targets, all_devices, on, yes):
    """Shared dry-run/execute path for `devices on` / `devices off`."""
    login = _login(ctx)
    records = _run(ctx, endpoints_core.fetch_endpoint_records(login))
    selected = _select_records(ctx, records, targets, all_devices)
    action = "turnOn" if on else "turnOff"
    if not yes:
        emit(
            ctx,
            {
                "dry_run": True,
                "action": action,
                "count": len(selected),
                "devices": [r.get("name") for r in selected],
                "hint": "re-run with --yes to execute",
            },
        )
        return
    results = []
    for rec in selected:
        entity_id = _run(ctx, _as_coro(smarthome_core.entity_ref, rec))
        results.append(
            {
                "name": rec.get("name"),
                **_run(ctx, smarthome_core.set_power(login, entity_id, on)),
            }
        )
    emit(ctx, results)


async def _as_coro(fn, *args, **kwargs):
    """Await-able wrapper so a pure validator can reuse ``_run``'s error mapping.

    ``entity_ref`` raises ``ValueError`` with the caller-facing message the CLI
    already knows how to print; routing it through ``_run`` keeps that in one
    place instead of duplicating the try/except at each call site.
    """
    return fn(*args, **kwargs)


@devices.command("on")
@click.argument("targets", nargs=-1)
@click.option("--all", "all_devices", is_flag=True, default=False, help="Every device (careful)")
@click.option("--yes", is_flag=True, default=False, help="Required to execute")
@click.pass_context
def devices_on(ctx, targets, all_devices, yes):
    """Turn device(s) on (plugs, switches, lights)."""
    _power_command(ctx, targets, all_devices, True, yes)


@devices.command("off")
@click.argument("targets", nargs=-1)
@click.option("--all", "all_devices", is_flag=True, default=False, help="Every device (careful)")
@click.option("--yes", is_flag=True, default=False, help="Required to execute")
@click.pass_context
def devices_off(ctx, targets, all_devices, yes):
    """Turn device(s) off (plugs, switches, lights)."""
    _power_command(ctx, targets, all_devices, False, yes)


@devices.command("light")
@click.argument("target")
@click.option("--on/--off", "power", default=None, help="Power the light on or off")
@click.option("--brightness", default=None, help="Brightness percentage, 0-100")
@click.option(
    "--color", default=None, help=f"Colour name ({', '.join(smarthome_core.COLOR_NAMES[:6])}…)"
)
@click.option(
    "--color-temp",
    "color_temperature",
    default=None,
    help=f"Colour temperature ({', '.join(smarthome_core.COLOR_TEMPERATURE_NAMES)})",
)
@click.option("--yes", is_flag=True, default=False, help="Required to execute")
@click.pass_context
def devices_light(ctx, target, power, brightness, color, color_temperature, yes):
    """Set a light's power / brightness / colour.

    \b
    Brightness-only or colour-only changes also send `turnOn` — that is what
    "set the brightness" means for a lamp that is off. --color and --color-temp
    are mutually exclusive (Alexa would apply both, last one winning).
    """
    # Validate before touching the network so a bad value fails fast and
    # identically in dry-run and executed mode.
    try:
        plan = smarthome_core.plan_light_change(
            power=power,
            brightness=brightness,
            color=color,
            color_temperature=color_temperature,
        )
    except ValueError as exc:
        _abort(str(exc))
    login = _login(ctx)
    records = _run(ctx, endpoints_core.fetch_endpoint_records(login))
    rec = _resolve_one_or_abort(
        ctx, records, endpoints_core.resolve_target(records, target), target
    )
    if not yes:
        emit(
            ctx,
            {
                "dry_run": True,
                "device": rec.get("name"),
                "actions": plan["actions"],
                "hint": "re-run with --yes to execute",
            },
        )
        return
    entity_id = _run(ctx, _as_coro(smarthome_core.entity_ref, rec))
    emit(
        ctx,
        {
            "name": rec.get("name"),
            **_run(
                ctx,
                smarthome_core.set_light_state(
                    login,
                    entity_id,
                    power=power,
                    brightness=brightness,
                    color=color,
                    color_temperature=color_temperature,
                ),
            ),
        },
    )


@devices.command("prune")
@click.option(
    "--whitelist",
    "whitelist_file",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="File of allowed HA entity ids (one per line)",
)
@click.option(
    "--dry-run/--no-dry-run",
    default=True,
    help="Preview only (default). --no-dry-run + --yes to execute.",
)
@click.option(
    "--yes", is_flag=True, default=False, help="Required to actually DELETE (guards live mutation)"
)
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
        res = _run(ctx, devices_core.delete_appliance(login, row["applianceId"]))
        results.append(res)
    emit(ctx, {**summary, "results": results})


@devices.command("delete")
@click.argument("appliance_ids", nargs=-1)
@click.option(
    "--entity",
    "entity",
    default=None,
    help="Resolve the appliance to delete by HA entity id (ha.entity_id)",
)
@click.option(
    "--name", "name", default=None, help="Resolve the appliance to delete by Alexa display name"
)
@click.option(
    "--verify",
    is_flag=True,
    default=False,
    help="After deleting, re-discover + re-query and report which "
    "devices re-synced/re-appeared (native devices re-sync from "
    "their source bridge/skill).",
)
@click.option(
    "--yes", is_flag=True, default=False, help="Required to actually delete (guards live mutation)"
)
@click.pass_context
def devices_delete(ctx, appliance_ids, entity, name, verify, yes):
    """Delete appliances by applianceId, --entity <ha.id>, or --name "<display>".

    Positional applianceId(s) still work. --entity / --name resolve via the
    endpoints query to the applianceId; if a name matches more than one device
    (native + HA twin) the command aborts and lists the matches.

    \b
    Native (non-HA) devices re-sync from their cloud skill / bridge, so deleting
    them in Alexa alone may not stick — you'll be warned, and --verify will
    re-discover and report which re-appeared.
    """
    login = _login(ctx)
    records = _run(ctx, endpoints_core.fetch_endpoint_records(login))
    # index records by applianceId so we can attach native-source warnings.
    by_appliance = {r.get("applianceId"): r for r in records if r.get("applianceId")}
    targets = list(appliance_ids)
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

    # native-source warnings (deleting native devices may not stick)
    warnings = []
    for aid in targets:
        rec = by_appliance.get(aid)
        if rec is not None:
            w = endpoints_core.native_delete_warning(rec)
            if w:
                warnings.append(w)
    if warnings and not ctx.obj.get("as_json"):
        for w in warnings:
            click.echo(f"warning: {w}", err=True)

    if not yes:
        emit(
            ctx,
            {
                "dry_run": True,
                "would_delete": targets,
                "native_warnings": warnings,
                "hint": "re-run with --yes to execute"
                + (" (--verify to re-check after)" if not verify else ""),
            },
        )
        return
    results = [_run(ctx, devices_core.delete_appliance(login, aid)) for aid in targets]
    if not verify:
        emit(ctx, results)
        return
    # build the "deleted" rows (applianceId + name) for the reappear diff
    deleted_rows = [
        {"applianceId": aid, "name": (by_appliance.get(aid) or {}).get("name")} for aid in targets
    ]
    verification = _run(ctx, devices_core.verify_deletes(login, deleted_rows))
    emit(ctx, {"results": results, "verify": verification, "native_warnings": warnings})


@cli.command("discover")
@click.option(
    "--yes",
    is_flag=True,
    default=False,
    help="Required to actually trigger discovery (guards live mutation)",
)
@click.pass_context
def discover_cmd(ctx, yes):
    """Trigger Alexa smart-home device discovery (POST /api/phoenix/discovery)."""
    login = _login(ctx)
    if not yes:
        emit(
            ctx,
            {"dry_run": True, "would_trigger": "discovery", "hint": "re-run with --yes to execute"},
        )
        return
    emit(ctx, _run(ctx, devices_core.trigger_discovery(login)))


# ──────────────────────────────────────────────────────── guard


@cli.group()
def guard():
    """Alexa Guard — read / set the home's away-vs-home arm state."""


def _guard_or_abort(ctx, login):
    """The Guard panel record, or a clean abort when the account has none."""
    records = _run(ctx, endpoints_core.fetch_endpoint_records(login))
    panel = smarthome_core.find_guard(records)
    if panel is None:
        _abort(
            "no Alexa Guard panel on this account — Guard is region-limited and "
            "must be set up in the Alexa app first"
        )
    return panel


@guard.command("status")
@click.pass_context
def guard_status(ctx):
    """Show whether Guard is armed away or standing down (read-only)."""
    login = _login(ctx)
    panel = _guard_or_abort(ctx, login)
    emit(
        ctx,
        _run(
            ctx,
            smarthome_core.fetch_guard_state(
                login, panel.get("applianceId"), name=panel.get("name")
            ),
        ),
    )


@guard.command("set")
@click.argument("state", type=click.Choice(["away", "home"]))
@click.option("--yes", is_flag=True, default=False, help="Required to execute")
@click.pass_context
def guard_set(ctx, state, yes):
    """Arm Guard (`away`) or stand it down (`home`).

    There is no separate "disarmed" state — `home` (ARMED_STAY) *is* how Guard
    is stood down.
    """
    login = _login(ctx)
    panel = _guard_or_abort(ctx, login)
    if not yes:
        emit(
            ctx,
            {
                "dry_run": True,
                "name": panel.get("name"),
                "would_set": smarthome_core.normalize_guard_state(state),
                "hint": "re-run with --yes to execute",
            },
        )
        return
    entity_id = _run(ctx, _as_coro(smarthome_core.entity_ref, panel))
    emit(
        ctx,
        _run(ctx, smarthome_core.set_guard_state(login, entity_id, state, name=panel.get("name"))),
    )


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


@echos.command("bluetooth")
@click.pass_context
def echos_bluetooth(ctx):
    """Show bluetooth devices paired to each Echo."""
    login = _login(ctx)
    emit(ctx, _run(ctx, devices_meta_core.fetch_bluetooth(login)))


@echos.command("wake-words")
@click.pass_context
def echos_wake_words(ctx):
    """Show the configured wake word for each Echo."""
    login = _login(ctx)
    emit(ctx, _run(ctx, devices_meta_core.fetch_wake_words(login)))


@echos.command("dnd")
@click.pass_context
def echos_dnd(ctx):
    """Show the current Do-Not-Disturb state of each Echo (read-only)."""
    login = _login(ctx)
    emit(ctx, _run(ctx, devices_meta_core.fetch_dnd_states(login)))


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
    member_ids, unresolved = groups_core.resolve_members(list(entities), list(endpoints), ent_map)
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
    return member_ids


def _resolve_child_groups(ctx, login, child_groups):
    """Resolve --child-group names/ids to group ids; abort on any unresolved."""
    if not child_groups:
        return []
    raw = _run(ctx, groups_core.fetch_groups(login))
    child_ids, unresolved = groups_core.resolve_child_groups(raw, list(child_groups))
    if unresolved:
        _abort("could not resolve these child groups (no such group?): " + ", ".join(unresolved))
    return child_ids


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
@click.option(
    "--entity", "entities", multiple=True, help="HA entity id to add as a member (repeatable)"
)
@click.option(
    "--endpoint",
    "endpoints",
    multiple=True,
    help="Alexa endpoint id (amzn1.alexa.endpoint.*) to add (repeatable)",
)
@click.option(
    "--child-group",
    "child_groups",
    multiple=True,
    help="Nest another group (by name or id) as a child — the rollup "
    "pattern, e.g. 'Downstairs' of room groups (repeatable)",
)
@click.option(
    "--yes", is_flag=True, default=False, help="Required to actually create (guards live mutation)"
)
@click.pass_context
def groups_create(ctx, name, entities, endpoints, child_groups, yes):
    """Create a device-group with members and/or child groups (dry-run unless --yes)."""
    login = _login(ctx)
    member_ids = _resolve_group_members(ctx, login, entities, endpoints)
    child_ids = _resolve_child_groups(ctx, login, child_groups)
    if not member_ids and not child_ids:
        _abort("no members given — pass at least one --entity / --endpoint / --child-group")
    if not yes:
        emit(
            ctx,
            {
                "dry_run": True,
                "would_create": name,
                "memberDeviceIds": member_ids,
                "childDeviceGroupIds": child_ids,
                "hint": "re-run with --yes to execute",
            },
        )
        return
    emit(ctx, _run(ctx, groups_core.create_group(login, name, member_ids, child_ids)))


def _groups_member_update(
    ctx, group, entities, endpoints, operation, yes, devices=(), child_groups=()
):
    """Shared add/remove/set body: resolve members + child groups + updateDeviceGroup."""
    login = _login(ctx)
    g = _find_group_or_abort(ctx, login, group)
    gid = g.get("id")
    member_ids = _resolve_group_members(ctx, login, entities, endpoints, devices)
    child_ids = _resolve_child_groups(ctx, login, child_groups)
    if not member_ids and not child_ids:
        _abort(
            "nothing to change — pass at least one --entity / --endpoint / --device / --child-group"
        )
    if not yes:
        emit(
            ctx,
            {
                "dry_run": True,
                "group": group,
                "deviceGroupId": gid,
                "operation": operation,
                "memberDeviceIds": member_ids,
                "childDeviceGroupIds": child_ids,
                "hint": "re-run with --yes to execute",
            },
        )
        return
    emit(ctx, _run(ctx, groups_core.update_group(login, gid, member_ids, operation, child_ids)))


@groups.command("add")
@click.argument("group")
@click.option("--entity", "entities", multiple=True, help="HA entity id (repeatable)")
@click.option("--endpoint", "endpoints", multiple=True, help="Alexa endpoint id (repeatable)")
@click.option(
    "--device",
    "devices_",
    multiple=True,
    help="Alexa display name — targets native/non-HA devices (repeatable)",
)
@click.option(
    "--child-group",
    "child_groups",
    multiple=True,
    help="Nest another group (by name or id) as a child (repeatable)",
)
@click.option("--yes", is_flag=True, default=False, help="Required to execute")
@click.pass_context
def groups_add(ctx, group, entities, endpoints, devices_, child_groups, yes):
    """Add members and/or child groups to a group (by name or id)."""
    _groups_member_update(ctx, group, entities, endpoints, "ADD", yes, devices_, child_groups)


@groups.command("remove")
@click.argument("group")
@click.option("--entity", "entities", multiple=True, help="HA entity id (repeatable)")
@click.option("--endpoint", "endpoints", multiple=True, help="Alexa endpoint id (repeatable)")
@click.option(
    "--device",
    "devices_",
    multiple=True,
    help="Alexa display name — targets native/non-HA devices (repeatable)",
)
@click.option(
    "--child-group",
    "child_groups",
    multiple=True,
    help="Remove a nested child group (by name or id) (repeatable)",
)
@click.option("--yes", is_flag=True, default=False, help="Required to execute")
@click.pass_context
def groups_remove(ctx, group, entities, endpoints, devices_, child_groups, yes):
    """Remove members and/or child groups from a group (by name or id)."""
    _groups_member_update(ctx, group, entities, endpoints, "REMOVE", yes, devices_, child_groups)


@groups.command("set")
@click.argument("group")
@click.option("--entity", "entities", multiple=True, help="HA entity id (repeatable)")
@click.option("--endpoint", "endpoints", multiple=True, help="Alexa endpoint id (repeatable)")
@click.option(
    "--device",
    "devices_",
    multiple=True,
    help="Alexa display name — targets native/non-HA devices (repeatable)",
)
@click.option(
    "--child-group",
    "child_groups",
    multiple=True,
    help="Replace the child-group set with these (by name or id) (repeatable)",
)
@click.option("--yes", is_flag=True, default=False, help="Required to execute")
@click.pass_context
def groups_set(ctx, group, entities, endpoints, devices_, child_groups, yes):
    """Replace a group's entire member (and child-group) set (by name or id)."""
    _groups_member_update(ctx, group, entities, endpoints, "REPLACE", yes, devices_, child_groups)


@groups.command("delete")
@click.argument("group")
@click.option(
    "--yes", is_flag=True, default=False, help="Required to actually delete (guards live mutation)"
)
@click.pass_context
def groups_delete(ctx, group, yes):
    """Delete a device-group (by name or id; dry-run unless --yes)."""
    login = _login(ctx)
    g = _find_group_or_abort(ctx, login, group)
    gid = g.get("id")
    name = ((g.get("friendlyName") or {}).get("value") or {}).get("text")
    if not yes:
        emit(
            ctx,
            {
                "dry_run": True,
                "would_delete": name or group,
                "deviceGroupId": gid,
                "hint": "re-run with --yes to execute",
            },
        )
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
@click.option(
    "--yes", is_flag=True, default=False, help="Required to actually trigger (guards live mutation)"
)
@click.pass_context
def routines_run(ctx, name_or_id, yes):
    """Trigger a routine by name or id (via behaviors/preview)."""
    login = _login(ctx)
    if not yes:
        emit(
            ctx, {"dry_run": True, "would_run": name_or_id, "hint": "re-run with --yes to execute"}
        )
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
@click.option("--in", "in_seconds", type=float, default=None, help="Seconds from now")
@click.option("--at", "at_epoch_ms", type=int, default=None, help="Absolute epoch milliseconds")
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
    payload = notifications_core.build_reminder(label, d["serialNumber"], d["deviceType"], when)
    if not yes:
        emit(ctx, {"dry_run": True, "payload": payload, "hint": "re-run with --yes to execute"})
        return
    emit(ctx, _run(ctx, notifications_core.create_notification(login, payload)))


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
    payload = notifications_core.build_alarm(d["serialNumber"], d["deviceType"], when, label=label)
    if not yes:
        emit(ctx, {"dry_run": True, "payload": payload, "hint": "re-run with --yes to execute"})
        return
    emit(ctx, _run(ctx, notifications_core.create_notification(login, payload)))


@notifications.command("add-timer")
@click.option("--device", required=True)
@click.option(
    "--duration", "duration_seconds", type=float, required=True, help="Timer duration in seconds"
)
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
        emit(ctx, {"dry_run": True, "payload": payload, "hint": "re-run with --yes to execute"})
        return
    emit(ctx, _run(ctx, notifications_core.create_notification(login, payload)))


@notifications.command("delete")
@click.argument("notification_id")
@click.option("--yes", is_flag=True, default=False, help="Required to execute")
@click.pass_context
def notifications_delete(ctx, notification_id, yes):
    """Delete a notification by id."""
    login = _login(ctx)
    if not yes:
        emit(
            ctx,
            {
                "dry_run": True,
                "would_delete": notification_id,
                "hint": "re-run with --yes to execute",
            },
        )
        return
    emit(ctx, _run(ctx, notifications_core.delete_notification(login, notification_id)))


# ──────────────────────────────────────────────────────── media


@cli.group()
def media():
    """Media transport + player state on physical Echo devices.

    DEVICE is an Echo accountName or serialNumber (see `echos list`). Omit it
    and the first online Echo is used, matching `announce`/`routines run`.
    """


def _media_action(ctx, device, action, yes):
    """Shared dry-run/execute path for the zero-argument transport verbs.

    Every transport verb is a mutation of what a speaker is doing, so it obeys
    the harness-wide rule: preview by default, act only on --yes.
    """
    login = _login(ctx)
    if not yes:
        emit(
            ctx,
            {
                "dry_run": True,
                "device": device or "first online",
                "action": action,
                "hint": "re-run with --yes to execute",
            },
        )
        return
    emit(ctx, _run(ctx, media_core.transport(login, device, action)))


@media.command("status")
@click.argument("device", required=False)
@click.pass_context
def media_status(ctx, device):
    """Show what an Echo is currently playing."""
    login = _login(ctx)
    emit(ctx, _run(ctx, media_core.player_status(login, device)))


@media.command("play")
@click.argument("device", required=False)
@click.option("--yes", is_flag=True, default=False, help="Required to execute")
@click.pass_context
def media_play(ctx, device, yes):
    """Resume playback."""
    _media_action(ctx, device, "play", yes)


@media.command("pause")
@click.argument("device", required=False)
@click.option("--yes", is_flag=True, default=False, help="Required to execute")
@click.pass_context
def media_pause(ctx, device, yes):
    """Pause playback."""
    _media_action(ctx, device, "pause", yes)


@media.command("next")
@click.argument("device", required=False)
@click.option("--yes", is_flag=True, default=False, help="Required to execute")
@click.pass_context
def media_next(ctx, device, yes):
    """Skip to the next track."""
    _media_action(ctx, device, "next", yes)


@media.command("previous")
@click.argument("device", required=False)
@click.option("--yes", is_flag=True, default=False, help="Required to execute")
@click.pass_context
def media_previous(ctx, device, yes):
    """Go back to the previous track."""
    _media_action(ctx, device, "previous", yes)


@media.command("forward")
@click.argument("device", required=False)
@click.option("--yes", is_flag=True, default=False, help="Required to execute")
@click.pass_context
def media_forward(ctx, device, yes):
    """Fast-forward the current track."""
    _media_action(ctx, device, "forward", yes)


@media.command("rewind")
@click.argument("device", required=False)
@click.option("--yes", is_flag=True, default=False, help="Required to execute")
@click.pass_context
def media_rewind(ctx, device, yes):
    """Rewind the current track."""
    _media_action(ctx, device, "rewind", yes)


@media.command("stop")
@click.argument("device", required=False)
@click.option(
    "--all",
    "all_devices",
    is_flag=True,
    default=False,
    help="Stop playback on EVERY device instead of one",
)
@click.option("--yes", is_flag=True, default=False, help="Required to execute")
@click.pass_context
def media_stop(ctx, device, all_devices, yes):
    """Stop playback on one Echo (or all of them with --all)."""
    login = _login(ctx)
    if not yes:
        emit(
            ctx,
            {
                "dry_run": True,
                "device": "all" if all_devices else (device or "first online"),
                "action": "stop",
                "hint": "re-run with --yes to execute",
            },
        )
        return
    emit(ctx, _run(ctx, media_core.stop(login, device, all_devices=all_devices)))


@media.command("volume")
@click.argument("device", required=False)
@click.option("--level", type=float, required=True, help="Volume percentage, 0-100")
@click.option("--yes", is_flag=True, default=False, help="Required to execute")
@click.pass_context
def media_volume(ctx, device, level, yes):
    """Set an Echo's volume (0-100)."""
    # Validate before touching the network so a bad number fails fast and
    # identically in dry-run and executed mode.
    try:
        media_core.normalize_volume(level)
    except ValueError as exc:
        _abort(str(exc))
    login = _login(ctx)
    if not yes:
        emit(
            ctx,
            {
                "dry_run": True,
                "device": device or "first online",
                "volume": level,
                "hint": "re-run with --yes to execute",
            },
        )
        return
    emit(ctx, _run(ctx, media_core.set_volume(login, device, level)))


@media.command("shuffle")
@click.argument("device", required=False)
@click.option(
    "--state", type=click.Choice(["on", "off"]), required=True, help="Turn shuffle on or off"
)
@click.option("--yes", is_flag=True, default=False, help="Required to execute")
@click.pass_context
def media_shuffle(ctx, device, state, yes):
    """Turn shuffle on or off."""
    login = _login(ctx)
    if not yes:
        emit(
            ctx,
            {
                "dry_run": True,
                "device": device or "first online",
                "shuffle": state,
                "hint": "re-run with --yes to execute",
            },
        )
        return
    emit(ctx, _run(ctx, media_core.set_shuffle(login, device, state == "on")))


@media.command("repeat")
@click.argument("device", required=False)
@click.option(
    "--state", type=click.Choice(["on", "off"]), required=True, help="Turn repeat on or off"
)
@click.option("--yes", is_flag=True, default=False, help="Required to execute")
@click.pass_context
def media_repeat(ctx, device, state, yes):
    """Turn repeat on or off."""
    login = _login(ctx)
    if not yes:
        emit(
            ctx,
            {
                "dry_run": True,
                "device": device or "first online",
                "repeat": state,
                "hint": "re-run with --yes to execute",
            },
        )
        return
    emit(ctx, _run(ctx, media_core.set_repeat(login, device, state == "on")))


@media.command("play-music")
@click.argument("search_phrase")
@click.option("--device", default=None, help="Echo accountName/serial (default: first online)")
@click.option(
    "--provider",
    default=media_core.DEFAULT_MUSIC_PROVIDER,
    help=(
        "Music provider id, e.g. "
        + ", ".join(media_core.KNOWN_MUSIC_PROVIDERS)
        + " (sent verbatim, so newer provider ids also work)"
    ),
)
@click.option("--yes", is_flag=True, default=False, help="Required to execute")
@click.pass_context
def media_play_music(ctx, search_phrase, device, provider, yes):
    """Play music matching SEARCH_PHRASE from a provider."""
    provider_id = media_core.normalize_provider(provider)
    login = _login(ctx)
    if not yes:
        emit(
            ctx,
            {
                "dry_run": True,
                "device": device or "first online",
                "provider": provider_id,
                "search": search_phrase,
                "hint": "re-run with --yes to execute",
            },
        )
        return
    emit(ctx, _run(ctx, media_core.play_music(login, device, search_phrase, provider_id)))


# ──────────────────────────────────────────────────────── announce / dnd


@cli.command("announce")
@click.argument("text")
@click.option("--device", default=None, help="Echo accountName/serial (default: all devices)")
@click.option("--yes", is_flag=True, default=False, help="Required to execute")
@click.pass_context
def announce_cmd(ctx, text, device, yes):
    """Speak an announcement on all devices (or one named target)."""
    login = _login(ctx)
    if not yes:
        emit(
            ctx,
            {
                "dry_run": True,
                "would_announce": text,
                "device": device or "all",
                "hint": "re-run with --yes to execute",
            },
        )
        return
    try:
        emit(ctx, _run(ctx, control_core.announce(login, text, device)))
    except ValueError as exc:
        _abort(str(exc))


@cli.command("speak")
@click.argument("text")
@click.option("--device", default=None, help="Echo accountName/serial (default: first online)")
@click.option("--yes", is_flag=True, default=False, help="Required to execute")
@click.pass_context
def speak_cmd(ctx, text, device, yes):
    """Speak TEXT on one Echo via TTS — no announcement chime.

    `announce` fans out to every device and plays Alexa's announcement tone
    first; `speak` is the plain say-something path on a single speaker.
    """
    login = _login(ctx)
    if not yes:
        emit(
            ctx,
            {
                "dry_run": True,
                "would_speak": text,
                "device": device or "first online",
                "hint": "re-run with --yes to execute",
            },
        )
        return
    try:
        emit(ctx, _run(ctx, control_core.speak(login, text, device)))
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
        emit(
            ctx,
            {
                "dry_run": True,
                "device": device,
                "dnd": state,
                "hint": "re-run with --yes to execute",
            },
        )
        return
    try:
        emit(ctx, _run(ctx, control_core.set_dnd(login, device, state == "on")))
    except ValueError as exc:
        _abort(str(exc))


# ──────────────────────────────────────────────────────── run (behaviours)


@cli.group("run")
def run_group():
    """Make an Echo do anything you could *say* to it.

    `run command` sends literal text through Alexa's own parser, so it reaches
    every skill and device on the account — including ones this CLI has no
    typed command for. `run sequence`/`run sound`/`run skill` trigger the
    built-in behaviours, the soundbank and a skill by id.

    Alexa answers out loud; there is no response payload. Read back what
    happened with `activity history`.
    """


def _queue_delay_or_abort(queue_delay):
    """Validate --queue-delay before any network call (see `media volume`)."""
    try:
        return sequences_core.normalize_queue_delay(queue_delay)
    except ValueError as exc:
        _abort(str(exc))


def _run_behavior(ctx, coro_factory, preview, queue_delay, yes):
    """Shared dry-run/execute path for the four behaviour verbs.

    Each one makes a speaker do something, so it obeys the harness-wide rule:
    preview by default, act only on --yes. Values are normalised *before*
    `_login` so bad input fails identically with and without --yes.
    """
    delay = _queue_delay_or_abort(queue_delay)
    login = _login(ctx)
    if not yes:
        emit(ctx, {"dry_run": True, **preview, "hint": "re-run with --yes to execute"})
        return
    emit(ctx, _run(ctx, coro_factory(login, delay)))


@run_group.command("catalog")
@click.option(
    "--kind",
    type=click.Choice(["all", "sequences", "sounds"]),
    default="all",
    help="Show only sequences or only sounds",
)
@click.pass_context
def run_catalog(ctx, kind):
    """List the built-in sequences and sound aliases (no account needed)."""
    data = sequences_core.catalog(kind)
    if ctx.obj.get("as_json"):
        emit(ctx, data)
        return
    for title, rows in data.items():
        click.echo(f"{title}:")
        click.echo(render_table(rows))


@run_group.command("command")
@click.argument("text")
@click.option("--device", default=None, help="Echo accountName/serial (default: first online)")
@click.option("--queue-delay", default=None, help="Seconds to batch queued commands (default: 0)")
@click.option("--yes", is_flag=True, default=False, help="Required to execute")
@click.pass_context
def run_command_cmd(ctx, text, device, queue_delay, yes):
    """Run TEXT as if it had been spoken to Alexa.

    e.g. `run command "turn off the kitchen lights" --yes`
    """
    try:
        utterance = sequences_core.normalize_command_text(text)
    except ValueError as exc:
        _abort(str(exc))
    _run_behavior(
        ctx,
        lambda login, delay: sequences_core.run_command(login, device, utterance, delay),
        {"device": device or "first online", "command": utterance},
        queue_delay,
        yes,
    )


@run_group.command("sequence")
@click.argument("name")
@click.option("--device", default=None, help="Echo accountName/serial (default: first online)")
@click.option("--queue-delay", default=None, help="Seconds to batch queued commands")
@click.option("--yes", is_flag=True, default=False, help="Required to execute")
@click.pass_context
def run_sequence_cmd(ctx, name, device, queue_delay, yes):
    """Run a built-in behaviour (weather, joke, good-night… — see `run catalog`)."""
    try:
        sequence = sequences_core.normalize_sequence(name)
    except ValueError as exc:
        _abort(str(exc))
    _run_behavior(
        ctx,
        lambda login, delay: sequences_core.run_sequence(login, device, sequence, delay),
        {"device": device or "first online", "sequence": sequence},
        queue_delay,
        yes,
    )


@run_group.command("sound")
@click.argument("sound")
@click.option("--device", default=None, help="Echo accountName/serial (default: first online)")
@click.option("--queue-delay", default=None, help="Seconds to batch queued commands")
@click.option("--yes", is_flag=True, default=False, help="Required to execute")
@click.pass_context
def run_sound_cmd(ctx, sound, device, queue_delay, yes):
    """Play a soundbank sound (alias or raw id — see `run catalog --kind sounds`)."""
    try:
        sound_id = sequences_core.normalize_sound(sound)
    except ValueError as exc:
        _abort(str(exc))
    _run_behavior(
        ctx,
        lambda login, delay: sequences_core.play_sound(login, device, sound_id, delay),
        {"device": device or "first online", "sound": sound_id},
        queue_delay,
        yes,
    )


@run_group.command("skill")
@click.argument("skill_id")
@click.option("--device", default=None, help="Echo accountName/serial (default: first online)")
@click.option("--queue-delay", default=None, help="Seconds to batch queued commands (default: 0)")
@click.option("--yes", is_flag=True, default=False, help="Required to execute")
@click.pass_context
def run_skill_cmd(ctx, skill_id, device, queue_delay, yes):
    """Launch a skill by id (amzn1.ask.skill.<uuid>)."""
    try:
        skill = sequences_core.normalize_skill_id(skill_id)
    except ValueError as exc:
        _abort(str(exc))
    _run_behavior(
        ctx,
        lambda login, delay: sequences_core.run_skill(login, device, skill, delay),
        {"device": device or "first online", "skill": skill},
        queue_delay,
        yes,
    )


# ──────────────────────────────────────────────────────── activity


@cli.group("activity")
def activity():
    """Voice history — what was said to Alexa and what she said back."""


@activity.command("history")
@click.option(
    "--limit",
    default=None,
    help=f"Records to fetch (default {activity_core.DEFAULT_HISTORY_LIMIT})",
)
@click.option(
    "--hours",
    default=None,
    help=f"How far back to look (default {activity_core.DEFAULT_HISTORY_HOURS})",
)
@click.option("--device", default=None, help="Only turns heard by this Echo")
@click.option("--contains", default=None, help="Only turns whose text matches")
@click.option(
    "--include-noise",
    is_flag=True,
    default=False,
    help="Keep DEVICE_ARBITRATION rows (multi-Echo wake-word races)",
)
@click.pass_context
def activity_history(ctx, limit, hours, device, contains, include_noise):
    """Show recent voice turns (transcript + Alexa's reply)."""
    try:
        count = activity_core.normalize_limit(limit)
        span = hours if hours is not None else activity_core.DEFAULT_HISTORY_HOURS
        activity_core.history_window(span)
    except ValueError as exc:
        _abort(str(exc))
    login = _login(ctx)
    emit(
        ctx,
        _run(
            ctx,
            activity_core.voice_history(
                login,
                limit=count,
                hours=span,
                device=device,
                contains=contains,
                include_noise=include_noise,
            ),
        ),
    )


@activity.command("records")
@click.option("--limit", default=None, help="Activities to fetch")
@click.pass_context
def activity_records(ctx, limit):
    """Show the legacy activity feed (carries per-activity ids and status)."""
    try:
        count = activity_core.normalize_limit(limit)
    except ValueError as exc:
        _abort(str(exc))
    login = _login(ctx)
    emit(ctx, _run(ctx, activity_core.activity_records(login, limit=count)))


@activity.command("last")
@click.option("--limit", default=None, help="How many records to search")
@click.pass_context
def activity_last(ctx, limit):
    """Show the last Echo that answered, and what it was asked."""
    try:
        count = activity_core.normalize_limit(limit)
    except ValueError as exc:
        _abort(str(exc))
    login = _login(ctx)
    emit(ctx, _run(ctx, activity_core.last_command(login, limit=count)))


@activity.command("clear")
@click.option("--items", default=None, help="How many recent recordings to delete (default 50)")
@click.option("--yes", is_flag=True, default=False, help="Required to execute")
@click.pass_context
def activity_clear(ctx, items, yes):
    """Delete recent voice recordings — irreversible."""
    try:
        count = activity_core.normalize_limit(items, default=50)
    except ValueError as exc:
        _abort(str(exc))
    login = _login(ctx)
    if not yes:
        emit(
            ctx,
            {
                "dry_run": True,
                "would_delete": count,
                "irreversible": True,
                "hint": "re-run with --yes to execute",
            },
        )
        return
    emit(ctx, _run(ctx, activity_core.clear_history(login, items=count)))


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
