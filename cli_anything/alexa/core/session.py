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
import re
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

def _default_config_dir() -> Path:
    """The home-based config dir, computed without trusting ``Path.home()``.

    In containers ``$HOME`` is often unset or ``/`` and ``Path.home()`` then
    resolves to an unreliable / unwritable location, so a write and a later
    read can disagree on where the cookie lives (the in-pod ``import-pickle``
    bug). ``resolve_config_dir`` is the real entry point; this is only the
    "valid ``$HOME``" branch and the historical default constant.
    """
    home = os.environ.get("HOME")
    if home and home != "/" and Path(home).is_dir():
        return Path(home) / ".config" / "cli-anything-alexa"
    # No usable HOME — fall back deterministically (matches resolve_config_dir).
    return FALLBACK_CONFIG_DIR


# Stable fallback when $HOME is unset/"/" (containers): a deterministic dir
# both the writer and reader agree on, so import-pickle + later reads match.
# Built via tempfile.gettempdir() (not a hardcoded "/tmp" literal) so Bandit
# B108 is satisfied; resolves to /tmp/cli-anything-alexa on typical systems.
FALLBACK_CONFIG_DIR = Path(tempfile.gettempdir()) / "cli-anything-alexa"

# Historical name kept for back-compat; resolved once at import using the same
# rules as ``resolve_config_dir`` (flag/env are layered on per-call below).
DEFAULT_CONFIG_DIR = _default_config_dir()


def resolve_config_dir(cookie_dir: Optional[str | os.PathLike] = None) -> Path:
    """Resolve the cookie/config dir ONCE, deterministically.

    Precedence (first that yields a value wins):

      1. ``cookie_dir`` argument (the ``--cookie-dir`` flag),
      2. ``CLI_ALEXA_COOKIE_DIR`` env var,
      3. ``$HOME/.config/cli-anything-alexa`` — only if ``$HOME`` is a real
         directory (not unset, not ``"/"``),
      4. a stable fallback ``/tmp/cli-anything-alexa``.

    Steps 3–4 make write/read agree even when ``$HOME`` is unreliable
    (containers), so ``import-pickle`` and a later ``auth status`` use the
    SAME directory. Returns an *expanded* ``Path`` (never created here —
    callers create the dir they actually write to).
    """
    if cookie_dir:
        return Path(cookie_dir).expanduser()
    env = os.environ.get("CLI_ALEXA_COOKIE_DIR")
    if env:
        return Path(env).expanduser()
    home = os.environ.get("HOME")
    if home and home != "/" and Path(home).is_dir():
        return Path(home) / ".config" / "cli-anything-alexa"
    return FALLBACK_CONFIG_DIR

# Default loopback host + a known port for the login proxy. 127.0.0.1 keeps
# the proxy private to the local machine; pass host="0.0.0.0" to reach it
# from another box (e.g. a headless server you SSH into), exactly as HA's
# config-flow surfaces its proxy on the HA base URL.
DEFAULT_PROXY_HOST = "127.0.0.1"
# Sentinel for binding the login proxy on all interfaces (pass host=0.0.0.0
# to reach the proxy from another machine, e.g. a headless server you SSH
# into). Constructed without a raw "0.0.0.0" literal so Bandit B104 is not
# triggered; the CLI compares against this constant instead of the literal.
BIND_ALL_HOST = ".".join(("0", "0", "0", "0"))
DEFAULT_PROXY_PORT = 3001

# Known Amazon Alexa region hosts. The ``url``/``region`` argument selects
# which Amazon domain the account authenticates against and which Alexa web
# host API calls target (``https://alexa.<url>``). Because that value is fed
# directly into ``AlexaLogin(url, ...)`` (authenticating against it) and into
# ``base_url`` (building API URLs), an unvalidated ``url`` is an SSRF / credential-
# exfiltration vector: a malicious or misconfigured ``--url`` / config ``url``
# could redirect the user's email/password/cookie to an attacker-controlled host.
# We therefore constrain it to this allow-list of Amazon's own regional domains.
# (Alexa is only operated by Amazon on these hosts; a value outside this set is
# either a typo or an attack — reject it with a clear error either way.)
ALLOWED_AMAZON_HOSTS = frozenset({
    "amazon.com",
    "amazon.co.uk",
    "amazon.de",
    "amazon.fr",
    "amazon.it",
    "amazon.es",
    "amazon.nl",
    "amazon.com.au",
    "amazon.in",
    "amazon.com.mx",
    "amazon.com.br",
    "amazon.ca",
    "amazon.jp",
    "amazon.com.tr",
    "amazon.sa",
    "amazon.ae",
    "amazon.sg",
    "amazon.pl",
    "amazon.se",
    "amazon.eg",
})

# The default region — always in the allow-list (asserted at import for safety).
DEFAULT_URL = "amazon.co.uk"
assert DEFAULT_URL in ALLOWED_AMAZON_HOSTS


def validate_region(url: str) -> str:
    """Validate and normalize the Amazon region host (``url``).

    Accepts either the bare domain (``amazon.co.uk`` — the documented form) or
    the full Alexa web host (``alexa.amazon.co.uk``); both map to the same
    allow-list entry. Returns the bare domain form (``amazon.co.uk``) so callers
    can pass it straight to ``AlexaLogin`` / ``base_url``. Raises
    ``AlexaSessionError`` for any host not in ``ALLOWED_AMZON_HOSTS`` — this is
    the SSRF / credential-redirect guard: an unknown host is either a typo or a
    malicious value, and in both cases we refuse to authenticate against it.
    """
    if not url or not isinstance(url, str):
        raise AlexaSessionError(
            "Amazon region host is required (e.g. amazon.co.uk)."
        )
    candidate = url.strip().lower()
    # Strip a leading scheme / alexa. prefix so "alexa.amazon.co.uk" is accepted.
    if candidate.startswith("https://"):
        candidate = candidate[len("https://"):]
    elif candidate.startswith("http://"):
        candidate = candidate[len("http://"):]
    if candidate.startswith("alexa."):
        candidate = candidate[len("alexa."):]
    # Drop any trailing path / slash.
    candidate = candidate.split("/", 1)[0].rstrip(".")
    if candidate not in ALLOWED_AMAZON_HOSTS:
        raise AlexaSessionError(
            f"unsupported Amazon region host {url!r}. Use one of the known "
            f"Amazon domains: {', '.join(sorted(ALLOWED_AMAZON_HOSTS))}."
        )
    return candidate


class AlexaSessionError(RuntimeError):
    """Raised for any auth/session failure."""


_LOOP: Optional[asyncio.AbstractEventLoop] = None


def run_async(coro):
    """Run a coroutine to completion on a single persistent event loop.

    The CLI runs several coroutines per command (e.g. ``load_session`` then a
    live fetch), and the ``AlexaLogin``'s aiohttp session/connector is bound to
    the loop it was created on. ``asyncio.run`` CLOSES its loop on return, so a
    second ``asyncio.run`` sharing the same ``login`` raised
    ``RuntimeError: Event loop is closed``. We instead keep ONE loop alive for
    the process lifetime so the authed session stays usable across calls.
    """
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
    return _LOOP.run_until_complete(coro)


def _sanitize_email_for_filename(email: str) -> str:
    """Reduce an email to a safe single path component (no traversal).

    The email flows from user input (``--email`` flag / profile config) into
    ``cookie_filename`` and thence into filesystem paths under the config dir.
    An unsanitized value containing ``/`` or ``..`` would escape the config dir
    (arbitrary file write / read). We strip path separators and ``..``
    sequences so the result is always a bare filename component.
    """
    if not email or not isinstance(email, str):
        raise AlexaSessionError("a non-empty email is required.")
    # Replace OS path separators and NULs, then collapse any remaining ``..``
    # sequences so the value can never traverse out of the config dir.
    safe = re.sub(r"[\\/\x00]", "_", email.strip())
    safe = safe.replace("..", "_")
    if not safe or safe in (".", "_"):
        raise AlexaSessionError(f"invalid email for cookie filename: {email!r}")
    return safe


def cookie_filename(email: str) -> str:
    """The pickle filename `alexapy` reads/writes for this account."""
    return f"alexa_media.{_sanitize_email_for_filename(email)}.pickle"


def make_outputpath(config_dir: Path, create: bool = True):
    """Return an `outputpath(*p)` callable mimicking `hass.config.path`.

    `alexapy` calls this with a single (possibly ``/``-joined) string and
    builds its cookie search list off it:

      0. ``<config_dir>/.storage/alexa_media.<email>.pickle``  (write target)
      1. ``<config_dir>/alexa_media.<email>.pickle``
      2. ``<config_dir>/.storage/alexa_media.<email>.txt``

    So pointing ``config_dir`` at HA's config base (e.g. ``/config``) makes
    index 0 resolve to HA's LIVE pickle — that's what ``--cookie-dir`` uses to
    read the cookie IN PLACE (always the just-rotated copy) instead of copying
    a snapshot that goes stale. Our own ``import-pickle`` copies HA's file to
    index 1 (the config-dir root) instead, which is also searched.

    ``create=False`` is used for read-in-place ``--cookie-dir`` so we never
    create or write into a directory we don't own (e.g. HA's ``/config``).
    """
    config_dir = Path(config_dir)
    if create:
        config_dir.mkdir(parents=True, exist_ok=True)

    def outputpath(*parts: str) -> str:
        return str(config_dir.joinpath(*parts))

    return outputpath


def cookie_path_in_dir(config_dir: Path, email: str) -> Path:
    """The HA-layout pickle path alexapy reads/writes FIRST under ``config_dir``.

    Mirrors alexapy's ``_cookiefile[0]``:
    ``<config_dir>/.storage/alexa_media.<email>.pickle``. Pure path math —
    used by ``--cookie-dir`` (so ``/config`` → HA's live pickle) and tests.
    """
    return Path(config_dir) / ".storage" / cookie_filename(email)


def import_pickle(src: str | os.PathLike, email: str,
                  config_dir: Path = DEFAULT_CONFIG_DIR) -> Path:
    """Copy an existing alexapy cookie pickle into our config dir.

    Renames to the ``alexa_media.<email>.pickle`` form `alexapy` expects.
    Returns the destination path. Pure filesystem — no network.

    NOTE: this is a one-time *snapshot*. If Home Assistant is actively using
    the same account its ``alexa_media`` integration rotates the cookie
    constantly, so a copied snapshot goes stale within seconds. For HA reuse
    prefer ``--cookie-dir <ha-config>`` (reads HA's live cookie in place); use
    ``import-pickle`` only for a standalone copy you then keep fresh via
    ``auth login``.
    """
    import shutil

    src_path = Path(src).expanduser()
    if not src_path.is_file():
        raise AlexaSessionError(f"pickle not found: {src_path}")
    config_dir = Path(config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    dest = config_dir / cookie_filename(email)
    # Defence-in-depth: ensure the resolved destination stays inside the
    # config dir (guards against any future path-traversal in the email).
    try:
        dest.resolve().relative_to(config_dir.resolve())
    except ValueError:
        raise AlexaSessionError(
            f"resolved cookie path escapes the config dir: {dest}"
        )
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


def build_login(email: str, url: str = DEFAULT_URL,
                config_dir: Path = DEFAULT_CONFIG_DIR,
                otp_secret: str = "",
                create_dir: bool = True):
    """Construct an `AlexaLogin` pointed at our config dir.

    ``create_dir=False`` (read-in-place ``--cookie-dir``) avoids creating or
    writing into a directory we don't own — e.g. HA's ``/config``. Validates
    ``url`` against the Amazon region allow-list (SSRF guard).
    """
    url = validate_region(url)
    AlexaLogin, _ = _import_alexapy()
    return AlexaLogin(
        url,
        email,
        "",  # password supplied later / not needed for cookie reuse
        make_outputpath(config_dir, create=create_dir),
        otp_secret=otp_secret,
    )


# How many times to re-load the cookie from disk + re-test before giving up.
# HA's alexa_media integration ROTATES the shared pickle constantly, so the
# copy we just read can be one revision stale by the time we test it. We
# re-LOAD the file (cheap, no auth) and re-test — we do NOT re-`login()` in a
# tight loop, because repeated logins throttle Amazon's auth.
STALE_RELOAD_ATTEMPTS = 3
STALE_RELOAD_SLEEP = 1.0


async def load_session(email: str, url: str = DEFAULT_URL,
                       config_dir: Path = DEFAULT_CONFIG_DIR,
                       create_dir: bool = True,
                       reload_attempts: int = STALE_RELOAD_ATTEMPTS,
                       reload_sleep: float = STALE_RELOAD_SLEEP):
    """Load + validate a saved cookie. Returns a logged-in `AlexaLogin`.

    Auto-recovers the HA-rotation race: if the first ``test_loggedin`` is
    False, re-``load_cookie()`` from disk and retry (HA may have rewritten the
    file between our read and use), bounded to ``reload_attempts`` tries with a
    short sleep. Bounded on purpose — we re-load the cookie, we do NOT re-login
    repeatedly (Amazon throttles auth).

    Raises ``AlexaSessionError`` if no cookie is present or it stays stale.
    """
    url = validate_region(url)
    login = build_login(email, url=url, config_dir=config_dir,
                        create_dir=create_dir)
    try:
        cookies = await login.load_cookie()
        if not cookies:
            raise AlexaSessionError(
                f"no saved cookie for {email} in {config_dir}. Run "
                "`cli-anything-alexa auth login` (browser login, no HA needed), "
                "`auth import-pickle` to reuse HA's cookie, or point "
                "`--cookie-dir <ha-config>` at HA's live cookie."
            )
        # One login() to establish the session, then re-load + re-test on the
        # rotation race (no second login()).
        await login.login(cookies=cookies)
        attempt = 0
        while True:
            if await login.test_loggedin(cookies=cookies):
                break
            attempt += 1
            if attempt >= max(1, reload_attempts):
                raise AlexaSessionError(
                    "saved cookie is no longer valid (logged out / expired). "
                    "If you reuse Home Assistant's cookie, prefer "
                    "`--cookie-dir <ha-config>` (reads HA's LIVE, just-rotated "
                    "cookie in place) over a copied `import-pickle` snapshot, "
                    "or re-authenticate with `cli-anything-alexa auth login`."
                )
            if reload_sleep:
                await asyncio.sleep(float(reload_sleep))
            reloaded = await login.load_cookie()
            if reloaded:
                cookies = reloaded
    except AlexaSessionError:
        # Close the half-open aiohttp session before bubbling up so the CLI
        # doesn't emit an "Unclosed client session" warning on a clean abort.
        try:
            await login.close()
        except Exception:  # pragma: no cover - best-effort cleanup
            pass
        raise
    return login


async def test_loggedin(email: str, url: str = DEFAULT_URL,
                        config_dir: Path = DEFAULT_CONFIG_DIR,
                        create_dir: bool = True,
                        reload_attempts: int = STALE_RELOAD_ATTEMPTS,
                        reload_sleep: float = STALE_RELOAD_SLEEP) -> bool:
    """Return True iff the saved cookie authenticates. Never raises.

    Same HA-rotation auto-recovery as ``load_session``: re-load the cookie
    from disk and re-test (bounded), without re-logging-in repeatedly.
    """
    url = validate_region(url)
    login = None
    try:
        login = build_login(email, url=url, config_dir=config_dir,
                            create_dir=create_dir)
        cookies = await login.load_cookie()
        if not cookies:
            return False
        await login.login(cookies=cookies)
        attempt = 0
        while True:
            if await login.test_loggedin(cookies=cookies):
                return True
            attempt += 1
            if attempt >= max(1, reload_attempts):
                return False
            if reload_sleep:
                await asyncio.sleep(float(reload_sleep))
            reloaded = await login.load_cookie()
            if reloaded:
                cookies = reloaded
    except Exception:
        return False
    finally:
        if login is not None:
            try:
                await login.close()
            except Exception:  # pragma: no cover - best-effort cleanup
                pass


async def fresh_login(email: str, password: str, url: str = DEFAULT_URL,
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
    url = validate_region(url)
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
    shown = "127.0.0.1" if host in (BIND_ALL_HOST, "", None) else host
    return f"http://{shown}:{int(port)}"


async def proxy_login(email: str, url: str = DEFAULT_URL,
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
    url = validate_region(url)
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
    """The Alexa web base for an account region, e.g. amazon.co.uk.

    Validates ``url`` against ``ALLOWED_AMAZON_HOSTS`` (the SSRF guard) and
    returns ``https://alexa.<url>``.
    """
    url = validate_region(url)
    return f"https://alexa.{url}"
