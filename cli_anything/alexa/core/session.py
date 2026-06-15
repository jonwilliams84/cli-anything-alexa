"""Alexa session/auth backend — wraps `alexapy.AlexaLogin`.

Two ways in (both avoid re-doing MFA on every call):

1. **Reuse an existing alexapy cookie** — e.g. the Home Assistant
   `alexa_media` integration's pickle at
   ``/config/.storage/alexa_media.<email>.pickle``. Import it with
   ``auth import-pickle`` (copies it into our config dir under the same
   ``alexa_media.<email>.pickle`` name `alexapy` expects), then every
   command just loads + validates it.

2. **Fresh login** — ``auth login`` drives `alexapy`'s email/password/OTP
   flow and persists the resulting cookie into our config dir.

`alexapy` is async; the CLI wraps each call in ``asyncio.run`` via
``run_async`` below. The authed aiohttp session lives on ``login.session``;
for mutating raw calls (phoenix delete, behaviors/preview) add the header
``csrf=<value of the 'csrf' cookie>`` (see ``csrf_header``).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Optional

DEFAULT_CONFIG_DIR = Path.home() / ".config" / "cli-anything-alexa"


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
    cookies = await login.load_cookie()
    if not cookies:
        raise AlexaSessionError(
            f"no saved cookie for {email} in {config_dir}. Run "
            "`auth import-pickle` (to reuse HA's cookie) or `auth login`."
        )
    await login.login(cookies=cookies)
    if not await login.test_loggedin(cookies=cookies):
        raise AlexaSessionError(
            "saved cookie is no longer valid (logged out / expired). "
            "Re-import a fresh pickle or run `auth login`."
        )
    return login


async def test_loggedin(email: str, url: str = "amazon.co.uk",
                        config_dir: Path = DEFAULT_CONFIG_DIR) -> bool:
    """Return True iff the saved cookie authenticates. Never raises."""
    try:
        login = build_login(email, url=url, config_dir=config_dir)
        cookies = await login.load_cookie()
        if not cookies:
            return False
        await login.login(cookies=cookies)
        return bool(await login.test_loggedin(cookies=cookies))
    except Exception:
        return False


async def fresh_login(email: str, password: str, url: str = "amazon.co.uk",
                      config_dir: Path = DEFAULT_CONFIG_DIR,
                      otp_secret: str = "",
                      otp_callback=None):
    """Drive alexapy's interactive login (password + optional OTP).

    ``otp_callback`` is a no-arg callable returning the OTP/2FA code when
    `alexapy` reports it needs one. Returns the logged-in `AlexaLogin`.
    """
    AlexaLogin, _ = _import_alexapy()
    login = AlexaLogin(
        url, email, password, make_outputpath(config_dir), otp_secret=otp_secret
    )
    await login.login()
    # alexapy surfaces required next-steps in login.status
    for _ in range(5):
        status = login.status or {}
        if status.get("login_successful"):
            break
        data: dict[str, Any] = {}
        if status.get("captcha_required"):
            raise AlexaSessionError(
                "captcha required — solve via the Alexa app or import HA's "
                "cookie instead (recommended). captcha url: "
                f"{status.get('captcha_image_url')}"
            )
        if status.get("securitycode_required") or status.get("login_failed") == "2fa":
            if not otp_callback:
                raise AlexaSessionError("2FA/OTP required but no code provided")
            data["securitycode"] = otp_callback()
        elif status.get("error_message"):
            raise AlexaSessionError(status["error_message"])
        else:
            break
        await login.login(data=data)
    if not (login.status or {}).get("login_successful"):
        raise AlexaSessionError(
            f"login did not complete: {login.status!r}. Importing HA's cookie "
            "via `auth import-pickle` is the reliable path."
        )
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
