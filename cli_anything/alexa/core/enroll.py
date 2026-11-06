"""Device-credential enrollment — alexapy's ``secureauth`` paste-URL flow.

The **fourth auth path** (``auth enroll``), alongside proxy browser login,
the scripted headless login and cookie reuse. alexapy ≥ 1.34 ships
``alexapy.secureauth`` (the "alexa-auth-redesign"): a PKCE **device
enrollment** flow where

  1. the CLI generates a one-shot enrollment flow (PKCE verifier + device
     serial in memory *only*) and prints Amazon's own login URL,
  2. you open that URL in your browser — same captcha/2FA pages as the
     proxy flow — and Amazon redirects back to a ``/ap/maplanding`` URL,
  3. you paste that redirect URL back, and
  4. the CLI exchanges the embedded authorization code (PKCE S256) for
     **durable device credentials** — a ``refresh_token`` + ``mac_dms``
     — via ``EnrollmentFlow.async_register``.

Nothing here handles or stores the account password or a TOTP seed, and
unlike `auth login` no local proxy is involved, so the two phases can be
run on different machines (save the flow state, finish elsewhere).

After the credential exchange the harness does the rest of the session
work locally: an ``AlexaLogin`` is built over the credentials' oauth
material, ``exchange_token_for_cookies`` mints the session cookie into
the same JSON jar every other command reads (``.storage/alexa_media.
<email>.cookies``), ``test_loggedin`` verifies it, and the cookie +
credentials file (chmod 0600) are persisted. The ``mac_dms`` device-
signing key travels with the credentials, so enrollment directly
enables `events listen` too.

All alexapy imports are lazy (same pattern as ``session.py``), so the CLI
loads without alexapy installed.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

from cli_anything.alexa.core.session import (
    AlexaSessionError,
    _import_alexapy,
    make_outputpath,
    validate_region,
)


def _import_secureauth():
    """Return alexapy's secureauth classes (lazy import, version-guarded)."""
    try:
        from alexapy.secureauth import DeviceCredentials, EnrollmentFlow
    except ImportError as exc:
        raise AlexaEnrollError(
            "device enrollment needs alexapy>=1.34 (the alexa-auth-redesign "
            "secureauth module). Upgrade with `pip install -U alexapy`, or "
            "use `auth login` with the current alexapy."
        ) from exc
    return EnrollmentFlow, DeviceCredentials


#: Fallback when alexapy is unavailable: the same marketplace allow-list
#: alexapy's secureauth ships (subset of session.ALLOWED_AMAZON_HOSTS —
#: not every Alexa region supports device enrollment).
_FALLBACK_DOMAINS = {
    "amazon.ca",
    "amazon.co.jp",
    "amazon.co.uk",
    "amazon.com",
    "amazon.com.au",
    "amazon.com.br",
    "amazon.com.mx",
    "amazon.de",
    "amazon.es",
    "amazon.fr",
    "amazon.in",
    "amazon.it",
}

_ENROLL_STATE_FILE = "enrollment.json"
_CREDENTIALS_FILE = "credentials.json"


class AlexaEnrollError(RuntimeError):
    """Raised for any device-enrollment failure."""


def supported_domains() -> set[str]:
    """The marketplace domains alexapy's enrollment flow supports.

    A narrower allow-list than the session region hosts: device enrollment
    only exists for alexapy's ``ALLOWED_DOMAINS`` (amazon.co.jp yes, but
    amazon.com.mx no — see the session allow-list for the API-side host
    list). alexapy owns the truth; the fallback copy above is only used
    when alexapy is absent.
    """
    try:
        from alexapy.secureauth import ALLOWED_DOMAINS

        return set(ALLOWED_DOMAINS)
    except ImportError:  # pragma: no cover - only without alexapy
        return set(_FALLBACK_DOMAINS)


def validate_enrollment_region(url: str) -> str:
    """Normalize + validate the enrollment host (pure).

    Two gates: the session region allow-list (an unknown host is either a
    typo or an attack — same SSRF guard every command uses) and then the
    enrollment allow-list, because a region that does not support device
    enrollment must fail HERE, not after a browser round-trip.
    """
    try:
        domain = validate_region(url)
    except AlexaSessionError as exc:
        raise AlexaEnrollError(str(exc)) from exc
    if domain not in supported_domains():
        raise AlexaEnrollError(
            f"device enrollment is not available for {domain!r}: alexapy's "
            "enrollment flow supports "
            + ", ".join(sorted(supported_domains()))
            + ". `auth login`/`--cookie-dir` work for every other region."
        )
    return domain


def new_flow(url: str, serial: str | None = None):
    """Start a fresh enrollment flow for a validated region host.

    The PKCE verifier and serial live in the returned object only —
    persist across invocations with ``flow.to_state()`` (via
    ``save_flow_state``) so a flow started here can be finished elsewhere.
    """
    domain = validate_enrollment_region(url)
    EnrollmentFlow, _ = _import_secureauth()
    try:
        return EnrollmentFlow(domain, serial=serial)
    except Exception as exc:  # alexapy: EnrollmentError
        raise AlexaEnrollError(str(exc)) from exc


def restore_flow(state: dict[str, Any]):
    """Rebuild a flow saved by ``save_flow_state`` (no network)."""
    EnrollmentFlow, _ = _import_secureauth()
    try:
        return EnrollmentFlow.from_state(state)
    except Exception as exc:  # noqa: BLE001 - alexapy raises EnrollmentError for bad state
        raise AlexaEnrollError(
            f"the saved enrollment state is unusable: {exc}. Finish the "
            "exchange with the same flow you started (delete it to start over)."
        ) from exc


def code_from_redirect(flow, pasted_url: str) -> str:
    """Extract the PKCE authorization code from the pasted redirect URL.

    Pure string parsing (alexapy's ``parse_redirect_url``): refuses a
    non-https URL, anything off ``/ap/maplanding``, the wrong domain, or
    a URL without an authorization code — each is either a mistake or an
    injection attempt, and the code must never be sent to the wrong host.
    """
    try:
        return flow.parse_redirect_url(pasted_url)
    except Exception as exc:  # alexapy: EnrollmentError
        raise AlexaEnrollError(
            f"{exc} — the URL to paste is the full Amazon page URL that "
            "appears in the browser bar after the sign-in redirect."
        ) from exc


def flow_row(flow) -> dict[str, Any]:
    """What the user needs to start the flow: the URL + its identity (pure).

    The raw PKCE verifier is deliberately NOT echoed — it is secret
    material; it lives in the state file only.
    """
    return {
        "enrollment_url": flow.oauth_url,
        "domain": flow.domain,
        "serial": flow.serial,
        "code_challenge_len": len(flow.code_challenge),
    }


def credentials_row(creds) -> dict[str, Any]:
    """A display row for the exchange result — secrets masked (pure).

    The refresh token is credential material equivalent to a password, so
    it is never echoed: ``has_refresh_token`` says whether a future
    re-exchange is possible.
    """
    mac_dms = getattr(creds, "mac_dms", None) or {}
    return {
        "serial": getattr(creds, "serial", ""),
        "customer_id": getattr(creds, "customer_id", None),
        "domain": getattr(creds, "domain", ""),
        "has_refresh_token": bool(getattr(creds, "refresh_token", None)),
        "has_mac_dms": bool(mac_dms),
        "mac_dms_serial": mac_dms.get("device_serial") if isinstance(mac_dms, dict) else None,
    }


def credentials_path(config_dir) -> Path:
    """Where durable device credentials are saved under the config dir."""
    return Path(config_dir) / _CREDENTIALS_FILE


def enrollment_state_path(config_dir) -> Path:
    """Where a two-phase flow's state is saved under the config dir (chmod 0600)."""
    return Path(config_dir) / _ENROLL_STATE_FILE


def save_credentials(path: Path, creds) -> Path:
    """Persist device credentials to ``path`` (chmod 0600) and return it.

    ``credentials.json`` is the durable enrolment: refresh token + mac_dms
    for the account, written with owner-only permissions. Re-enrollment
    overwrites (the record replaces the previous in full).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        **_credentials_dict(creds),
        "saved_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - filesystems without chmod
        _log.debug("chmod 0600 on %s failed", path, exc_info=True)
    return path


def _credentials_dict(creds) -> dict[str, Any]:
    """The credentials as a plain dict (alexapy's ``as_dict``)."""
    try:
        return creds.as_dict()
    except Exception as exc:  # pragma: no cover - alexapy object, not a fake
        raise AlexaEnrollError(f"could not serialize the exchanged credentials: {exc}") from exc


def load_credentials(path: Path):
    """Load previously saved credentials (``credentials.json``).

    Raises ``AlexaEnrollError`` for a missing or unreadable file so a
    stale/corrupt enrolment is never silently turned into a session.
    """
    path = Path(path)
    if not path.is_file():
        raise AlexaEnrollError(
            f"no saved device credentials at {path}. Run `cli-anything-alexa auth enroll` first."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise AlexaEnrollError(
            f"saved device credentials are unreadable ({exc}). Re-enroll with "
            "`cli-anything-alexa auth enroll`."
        ) from exc
    EnrollmentFlow, DeviceCredentials = _import_secureauth()
    # Only the dataclass's own fields survive load — metadata keys we wrote
    # alongside them (``saved_at``) are ours, not the credentials'.
    fields = set(getattr(DeviceCredentials, "__dataclass_fields__", {}))
    if fields:
        data = {k: v for k, v in data.items() if k in fields}
    try:
        return DeviceCredentials.from_dict(data)
    except (KeyError, TypeError) as exc:
        raise AlexaEnrollError(
            f"saved device credentials have an unknown shape ({exc}). "
            "Re-enroll with `cli-anything-alexa auth enroll`."
        ) from exc


def save_flow_state(path: Path, flow) -> Path:
    """Persist the in-memory flow for a two-phase enrollment (chmod 0600).

    The state file holds the PKCE verifier — secret, short-lived, and
    deliberately stored only under the private config dir.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(flow.to_state(), indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - filesystems without chmod
        _log.debug("chmod 0600 on %s failed", path, exc_info=True)
    return path


def load_flow_state(path: Path):
    """Restore a flow saved by ``save_flow_state``."""
    path = Path(path)
    if not path.is_file():
        raise AlexaEnrollError(
            f"no enrollment in progress at {path}. Start one with "
            "`cli-anything-alexa auth enroll` (or its --start phase)."
        )
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise AlexaEnrollError(
            f"the saved enrollment state is unreadable ({exc}). Start over "
            "with `cli-anything-alexa auth enroll`."
        ) from exc
    return restore_flow(state)


def _registration_session():
    """The aiohttp session the credential exchange rides on (lazy import)."""
    try:
        import aiohttp

        return aiohttp
    except ImportError as exc:  # pragma: no cover - ships with alexapy
        raise AlexaEnrollError("aiohttp is required for device enrollment.") from exc


async def register_code(flow, pasted_url: str, aio_session=None):
    """Exchange the pasted redirect URL's code for ``DeviceCredentials``.

    Opens (and always closes) its own aiohttp session unless the caller
    supplies one. Network/protocol failures surface as
    ``AlexaEnrollError`` so the CLI prints one hint, not a traceback.
    """
    code = code_from_redirect(flow, pasted_url)
    aiohttp = _registration_session()
    owns_session = aio_session is None
    session = aio_session if aio_session is not None else aiohttp.ClientSession()
    try:
        try:
            return await flow.async_register(session, code)
        except AlexaEnrollError:
            raise
        except Exception as exc:  # noqa: BLE001 - alexapy: EnrollmentError / network
            raise AlexaEnrollError(
                f"the credential exchange failed: {type(exc).__name__}: {exc}"
            ) from exc
    finally:
        if owns_session:
            try:
                await session.close()
            except Exception:  # noqa: BLE001 - pragma: no cover - best-effort cleanup
                _log.debug("registration session close failed", exc_info=True)


async def activate_credentials(
    creds,
    email: str,
    url: str,
    config_dir,
) -> dict[str, Any]:
    """Turn durable credentials into a working, saved session cookie.

    Builds an ``AlexaLogin`` over the credentials' oauth material (its
    ``refresh_token`` + ``mac_dms`` — the same fields a proxy/scripted
    login would have minted), mints the session cookies via alexapy's
    ``exchange_token_for_cookies`` (``/ap/exchangetoken/cookies`` against
    the same region host), verifies the session with ``test_loggedin``
    and persists the cookie with ``save_cookiefile`` — into the exact
    JSON jar the rest of the harness loads, so a fresh enrolment needs no
    re-login for every live command.

    Returns a row: ``{"email", "cookie_saved", "logged_in"}``
    (``logged_in`` False means the cookie was saved but Amazon refused
    the follow-up check — a stale/limited session, reported, not hidden).
    """
    domain = validate_region(url)
    email = (email or "").strip()
    if not email:
        raise AlexaEnrollError("an account email is required to name the cookie.")
    AlexaLogin, _ = _import_alexapy()
    login = AlexaLogin(
        domain,
        email,
        "",
        make_outputpath(config_dir),
        oauth={
            "refresh_token": getattr(creds, "refresh_token", None),
            "mac_dms": getattr(creds, "mac_dms", None),
        },
    )
    row: dict[str, Any] = {"email": email, "cookie_saved": False, "logged_in": False}
    try:
        if not await login.exchange_token_for_cookies():
            raise AlexaEnrollError(
                "Amazon refused the token-for-cookie exchange. The enrollment "
                "code may have expired — re run `auth enroll` for a fresh one."
            )
        row["cookie_saved"] = True
        row["logged_in"] = bool(await login.test_loggedin())
        await login.save_cookiefile()
    finally:
        try:
            await login.close()
        except Exception:  # noqa: BLE001 - pragma: no cover - best-effort cleanup
            _log.debug("login.close() failed", exc_info=True)
    return row


def enrollment_summary(
    creds_row: dict[str, Any],
    activation: dict[str, Any],
    credentials_file: Path,
    state_file: Path | None = None,
) -> dict[str, Any]:
    """One row the CLI emits after every enrollment path (pure).

    ``logged_in`` mirrors the verify semantics the rest of the harness uses
    (True / False, never an empty string), and the file locations are the
    two things a re-run or an uninstall needs to know about.
    """
    summary = {
        "method": "enroll",
        "domain": creds_row.get("domain"),
        "serial": creds_row.get("serial"),
        "customer_id": creds_row.get("customer_id"),
        "email": activation.get("email"),
        "cookie_saved": activation.get("cookie_saved", False),
        "logged_in": bool(activation.get("logged_in", False)),
        "credentials_file": str(credentials_file),
    }
    if state_file is not None:
        summary["enrollment_state_file"] = str(state_file)
    return summary
