"""Alexa session/auth backend — wraps `alexapy.AlexaLogin`.

Three ways in (all avoid re-doing MFA on every call):

1. **Proxy browser login (recommended)** — ``auth login`` starts a small
   local web proxy (alexapy's ``AlexaProxy``, the same mechanism the Home
   Assistant ``alexa_media`` config-flow uses) and prints a URL. You open
   that URL in a browser and complete Amazon's *own* login pages — password,
   captcha, 2FA/OTP — natively. When Amazon redirects to its success page,
   the proxy captures the session and we persist the cookie. No Home
   Assistant required; captcha and MFA "just work" because you are using
   Amazon's real login UI. See ``proxy_login``.

2. **Scripted login (headless/CI fallback)** — ``auth login --password ...
   [--otp-secret <TOTP base32>]`` drives alexapy's non-interactive
   email/password/OTP flow. Amazon frequently gates this behind a captcha;
   when it does, fall back to the proxy flow. See ``fresh_login``.

3. **Reuse an existing alexapy cookie** — e.g. the Home Assistant
   ``alexa_media`` integration's pickle at
   ``/config/.storage/alexa_media.<email>.pickle``. Import it with
   ``auth import-pickle``. A convenience for existing HA users, not the
   default.

`alexapy` is async; the CLI wraps each call in ``asyncio.run`` via
``run_async`` below. The authed aiohttp session lives on ``login.session``;
for mutating raw calls (phoenix delete, behaviors/preview) add the header
``csrf=<value of the 'csrf' cookie>`` (see ``csrf_header``).

Python-version note: a fresh proxy/scripted login persisted by alexapy on
*your* Python loads back fine on that same Python — 3.10+ is enough. The
``partitioned`` cookie-attribute incompatibility (a ``CookieError`` /
``KeyError`` on unpickle) only bites when *importing* a pickle written by a
**newer** Python — e.g. Home Assistant's 3.14 pickle read on a 3.13/3.12
host. So 3.14 is needed only for ``import-pickle`` from a 3.14 source.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Optional

DEFAULT_CONFIG_DIR = Path.home() / ".config" / "cli-anything-alexa"

# Default loopback host + a known port for the login proxy. 127.0.0.1 keeps
# the proxy private to the local machine; pass host="0.0.0.0" to reach it
# from another box (e.g. a headless server you SSH into), exactly as HA's
# config-flow surfaces its proxy on the HA base URL.
DEFAULT_PROXY_HOST = "127.0.0.1"
DEFAULT_PROXY_PORT = 3001


class AlexaSessionError(RuntimeError):
    """Raised for any auth/session failure."""


def run_async(coro):
    """Run a coroutine to completion, returning its result.

    A fresh event loop per invocation keeps the stateless CLI simple.
    """
    return asyncio.run(coro)


def cookie_filename(email: str) -> str:
    """The pickle filename `alexapy` reads/writes for this account."""
    return f"alexa_media.{email}.pickle"


def make_outputpath(config_dir: Path):
    """Return an `outputpath(*p)` callable mimicking `hass.config.path`.

    `alexapy` joins paths onto this; the cookie pickle lands at
    ``<config_dir>/<cookie_filename>``. We deliberately do NOT add a
    `.storage` segment (HA does) — our cookie sits directly in the
    config dir, and `import-pickle` copies HA's file to match.
    """
    config_dir = Path(config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)

    def outputpath(*parts: str) -> str:
        return str(config_dir.joinpath(*parts))

    return outputpath


def import_pickle(src: str | os.PathLike, email: str,
                  config_dir: Path = DEFAULT_CONFIG_DIR) -> Path:
    """Copy an existing alexapy cookie pickle into our config dir.

    Renames to the ``alexa_media.<email>.pickle`` form `alexapy` expects.
    Returns the destination path. Pure filesystem — no network.
    """
    import shutil

    src_path = Path(src).expanduser()
    if not src_path.is_file():
        raise AlexaSessionError(f"pickle not found: {src_path}")
    config_dir = Path(config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    dest = config_dir / cookie_filename(email)
    shutil.copy2(src_path, dest)
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass
    return dest


def _import_alexapy():
    try:
        from alexapy import AlexaLogin, AlexaAPI  # noqa: F401

        return AlexaLogin, AlexaAPI
    except ImportError as exc:  # pragma: no cover - exercised only without dep
        raise AlexaSessionError(
            "alexapy is not installed. Install it with `pip install alexapy` "
            "(it's also bundled in the Home Assistant venv). cli-anything-alexa "
            "needs it for every live command."
        ) from exc


def build_login(email: str, url: str = "amazon.co.uk",
                config_dir: Path = DEFAULT_CONFIG_DIR,
                otp_secret: str = ""):
    """Construct an `AlexaLogin` pointed at our config dir."""
    AlexaLogin, _ = _import_alexapy()
    return AlexaLogin(
        url,
        email,
        "",  # password supplied later / not needed for cookie reuse
        make_outputpath(config_dir),
        otp_secret=otp_secret,
    )


async def load_session(email: str, url: str = "amazon.co.uk",
                       config_dir: Path = DEFAULT_CONFIG_DIR):
    """Load + validate a saved cookie. Returns a logged-in `AlexaLogin`.

    Raises ``AlexaSessionError`` if no cookie is present or it's stale.
    """
    login = build_login(email, url=url, config_dir=config_dir)
    try:
        cookies = await login.load_cookie()
        if not cookies:
            raise AlexaSessionError(
                f"no saved cookie for {email} in {config_dir}. Run "
                "`cli-anything-alexa auth login` (browser login, no HA needed) "
                "or `auth import-pickle` to reuse HA's cookie."
            )
        await login.login(cookies=cookies)
        if not await login.test_loggedin(cookies=cookies):
            raise AlexaSessionError(
                "saved cookie is no longer valid (logged out / expired). "
                "Re-authenticate with `cli-anything-alexa auth login`."
            )
    except AlexaSessionError:
        # Close the half-open aiohttp session before bubbling up so the CLI
        # doesn't emit an "Unclosed client session" warning on a clean abort.
        try:
            await login.close()
        except Exception:  # pragma: no cover - best-effort cleanup
            pass
        raise
    return login


async def test_loggedin(email: str, url: str = "amazon.co.uk",
                        config_dir: Path = DEFAULT_CONFIG_DIR) -> bool:
    """Return True iff the saved cookie authenticates. Never raises."""
    login = None
    try:
        login = build_login(email, url=url, config_dir=config_dir)
        cookies = await login.load_cookie()
        if not cookies:
            return False
        await login.login(cookies=cookies)
        return bool(await login.test_loggedin(cookies=cookies))
    except Exception:
        return False
    finally:
        if login is not None:
            try:
                await login.close()
            except Exception:  # pragma: no cover - best-effort cleanup
                pass


async def fresh_login(email: str, password: str, url: str = "amazon.co.uk",
                      config_dir: Path = DEFAULT_CONFIG_DIR,
                      otp_secret: str = "",
                      otp_callback=None):
    """Drive alexapy's *scripted* login (password + optional OTP).

    This is the non-interactive fallback for headless/CI use. Prefer the
    proxy flow (``proxy_login``) for desktops — Amazon often captcha-blocks
    scripted logins, and the proxy handles captcha/MFA natively.

    ``otp_secret`` is a base32 TOTP shared secret: when present, alexapy
    auto-generates the 2FA code each attempt (no prompt needed). Otherwise
    ``otp_callback`` (a no-arg callable returning the code) is used when
    alexapy reports a code is required. Returns the logged-in `AlexaLogin`.
    """
    AlexaLogin, _ = _import_alexapy()
    login = AlexaLogin(
        url, email, password, make_outputpath(config_dir), otp_secret=otp_secret
    )
    # Register the TOTP secret so alexapy can fill the 2FA code itself.
    if otp_secret:
        try:
            login.set_totp(otp_secret)
        except Exception:  # pragma: no cover - alexapy/pyotp specifics
            pass
    await login.login()
    # alexapy surfaces required next-steps in login.status
    for _ in range(5):
        status = login.status or {}
        if status.get("login_successful"):
            break
        data: dict[str, Any] = {}
        if status.get("captcha_required"):
            raise AlexaSessionError(
                "Amazon returned a captcha for this scripted login. Captcha "
                "cannot be solved headlessly — use the proxy browser login "
                "instead:  `cli-anything-alexa auth login` (no --password). "
                "It opens Amazon's own pages where captcha/2FA work normally."
            )
        if status.get("securitycode_required") or status.get("login_failed") == "2fa":
            # If a TOTP secret was given, alexapy already injected the code;
            # re-issuing without one would loop. Only prompt when we have no
            # secret and a callback is available.
            if otp_secret:
                # let alexapy retry with its generated code
                pass
            elif otp_callback:
                data["securitycode"] = otp_callback()
            else:
                raise AlexaSessionError(
                    "2FA/OTP required but no code available. Pass "
                    "--otp-secret <base32 TOTP secret> for headless login, "
                    "or use the proxy flow (`auth login` with no --password)."
                )
        elif status.get("error_message"):
            raise AlexaSessionError(str(status["error_message"]))
        else:
            break
        await login.login(data=data)
    if not (login.status or {}).get("login_successful"):
        raise AlexaSessionError(
            f"scripted login did not complete: {login.status!r}. Use the "
            "proxy browser login: `cli-anything-alexa auth login` (no "
            "--password)."
        )
    return login


def proxy_access_url(host: str, port: int) -> str:
    """Build the local proxy base URL a user opens in the browser (pure).

    ``host`` 0.0.0.0 binds all interfaces but is not itself browsable; we
    surface 127.0.0.1 in that case so the printed URL is clickable on the
    same machine (matching how HA advertises its proxy on a reachable host).
    """
    shown = "127.0.0.1" if host in ("0.0.0.0", "", None) else host
    return f"http://{shown}:{int(port)}"


async def proxy_login(email: str, url: str = "amazon.co.uk",
                      config_dir: Path = DEFAULT_CONFIG_DIR,
                      host: str = DEFAULT_PROXY_HOST,
                      port: int = DEFAULT_PROXY_PORT,
                      timeout: float = 600.0,
                      poll_interval: float = 3.0,
                      on_url=None):
    """Robust browser-based login via alexapy's ``AlexaProxy``.

    Starts a local web proxy, hands the caller (via ``on_url`` and the return
    value) the access URL to open in a browser, then polls
    ``login.test_loggedin()`` until success or ``timeout`` seconds elapse.
    On success the cookie is persisted (alexapy's ``finalize_login`` writes
    it into the config dir under the ``alexa_media.<email>.pickle`` name the
    rest of the CLI expects) and ``chmod 0600``. The proxy is ALWAYS stopped,
    including on timeout / cancellation.

    Mirrors the HA ``alexa_media`` config-flow: ``AlexaProxy(login, base)`` →
    ``change_login`` → ``access_url`` → user completes Amazon's own pages →
    ``test_loggedin`` → ``finalize_login``.

    Returns the logged-in ``AlexaLogin`` on success; raises
    ``AlexaSessionError`` on timeout or proxy failure.
    """
    AlexaLogin, _ = _import_alexapy()
    try:
        from alexapy import AlexaProxy
    except ImportError as exc:  # pragma: no cover - only without dep
        raise AlexaSessionError(
            "alexapy is too old / missing AlexaProxy — proxy login needs "
            "alexapy>=1.27.0. Upgrade with `pip install -U alexapy`."
        ) from exc

    config_dir = Path(config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    login = AlexaLogin(url, email, "", make_outputpath(config_dir))

    base = proxy_access_url(host, port)
    proxy = AlexaProxy(login, base)
    try:
        await proxy.start_proxy(host=host)
        proxy.change_login(login)
        access_url = str(proxy.access_url())
        # Stash so AlexaProxy's success test can redirect back to "/".
        login.proxy_url = proxy.access_url()
        try:
            login.session.cookie_jar.clear()
        except Exception:  # pragma: no cover
            pass
        if on_url:
            on_url(access_url)

        deadline = asyncio.get_event_loop().time() + float(timeout)
        while True:
            await asyncio.sleep(float(poll_interval))
            try:
                if await login.test_loggedin():
                    break
            except Exception:  # transient during the login dance — keep polling
                pass
            if asyncio.get_event_loop().time() >= deadline:
                raise AlexaSessionError(
                    "timed out waiting for the browser login to complete. "
                    f"Re-run `auth login` and open {access_url} promptly "
                    "(use --host 0.0.0.0 if logging in from another machine)."
                )

        await login.finalize_login()
    finally:
        try:
            await proxy.stop_proxy()
        except Exception:  # pragma: no cover - best-effort cleanup
            pass
        # Close alexapy's aiohttp session — the cookie is already on disk; we
        # only used the live session to drive the login. Avoids "Unclosed
        # client session" warnings.
        try:
            await login.close()
        except Exception:  # pragma: no cover - best-effort cleanup
            pass

    # Lock down whatever cookie file alexapy just wrote.
    for name in (
        config_dir / ".storage" / cookie_filename(email),
        config_dir / cookie_filename(email),
    ):
        try:
            if name.exists():
                os.chmod(name, 0o600)
        except OSError:
            pass
    return login


def csrf_header(login) -> dict[str, str]:
    """Build the ``{'csrf': <value>}`` header required for mutating calls.

    Reads the `csrf` cookie off the authed aiohttp session's cookie jar.
    """
    try:
        for cookie in login.session.cookie_jar:
            if cookie.key == "csrf":
                return {"csrf": cookie.value}
    except Exception:
        pass
    return {}


def base_url(url: str = "amazon.co.uk") -> str:
    """The Alexa web base for an account region, e.g. amazon.co.uk."""
    host = url if url.startswith("alexa.") else f"alexa.{url}"
    return f"https://{host}"
