"""Behaviour tests for uncovered session.py logic.

Targets:
  * run_async reuses a single event loop across calls (the bug it fixes).
  * _sanitize_email_for_filename edge cases (whitespace-only, dots).
  * import_pickle error path (source file missing).
  * proxy_access_url host normalisation (0.0.0.0 -> 127.0.0.1).
  * csrf_header graceful degradation (no session, no csrf cookie).
  * test_loggedin returns False when load_cookie returns nothing.
  * test_loggedin swallows exceptions and returns False.
  * load_session raises friendly error when no cookie present.
"""

import asyncio
import os
from pathlib import Path

import pytest

from cli_anything.alexa.core import session


# ── run_async: single persistent loop ───────────────────────────────

def test_run_async_reuses_loop_across_calls():
    """run_async must keep ONE loop alive so aiohttp sessions survive."""
    session._LOOP = None  # reset

    async def _get_loop():
        return asyncio.get_event_loop()

    loop1 = session.run_async(_get_loop())
    loop2 = session.run_async(_get_loop())
    assert loop1 is loop2, "run_async must reuse the same event loop"
    assert not loop1.is_closed()


def test_run_async_recreates_after_close():
    """If the loop was closed, run_async creates a fresh one."""
    session._LOOP = None
    async def _noop():
        pass
    session.run_async(_noop())
    session._LOOP.close()
    assert session._LOOP.is_closed()
    # Should create a new loop
    async def _get_loop():
        return asyncio.get_event_loop()
    new_loop = session.run_async(_get_loop())
    assert not new_loop.is_closed()


# ── _sanitize_email_for_filename edge cases ─────────────────────────

def test_sanitize_email_whitespace_only_raises():
    """A whitespace-only email must raise, not produce a bare underscore."""
    with pytest.raises(session.AlexaSessionError):
        session._sanitize_email_for_filename("   ")


def test_sanitize_email_strips_surrounding_whitespace():
    assert session._sanitize_email_for_filename("  user@example.com  ") == "user@example.com"


def test_sanitize_email_replaces_path_separators():
    result = session._sanitize_email_for_filename("a/b\\c")
    assert "/" not in result
    assert "\\" not in result


def test_sanitize_email_collapses_dotdot():
    result = session._sanitize_email_for_filename("a..b")
    assert ".." not in result


def test_sanitize_email_none_raises():
    with pytest.raises(session.AlexaSessionError):
        session._sanitize_email_for_filename(None)


def test_sanitize_email_non_string_raises():
    with pytest.raises(session.AlexaSessionError):
        session._sanitize_email_for_filename(123)


def test_sanitize_email_only_dots_raises():
    """An email that sanitises to just '.' must raise."""
    with pytest.raises(session.AlexaSessionError):
        session._sanitize_email_for_filename(".")


# ── import_pickle error path ────────────────────────────────────────

def test_import_pickle_missing_source_raises(tmp_path):
    """import_pickle must raise when the source file does not exist."""
    config = tmp_path / "config"
    config.mkdir()
    with pytest.raises(session.AlexaSessionError) as exc:
        session.import_pickle(tmp_path / "nonexistent.pickle",
                              "you@example.com", config_dir=config)
    assert "pickle not found" in str(exc.value)


# ── proxy_access_url host normalisation ─────────────────────────────

def test_proxy_access_url_bind_all_becomes_localhost():
    """0.0.0.0 is not browsable; the URL must show 127.0.0.1."""
    url = session.proxy_access_url("0.0.0.0", 8765)
    assert "127.0.0.1" in url
    assert "8765" in url
    assert "0.0.0.0" not in url


def test_proxy_access_url_empty_host_becomes_localhost():
    url = session.proxy_access_url("", 8765)
    assert "127.0.0.1" in url


def test_proxy_access_url_specific_host_preserved():
    url = session.proxy_access_url("192.168.1.10", 8765)
    assert "192.168.1.10" in url
    assert "127.0.0.1" not in url


def test_proxy_access_url_port_as_string_coerced_to_int():
    url = session.proxy_access_url("0.0.0.0", "8765")
    assert ":8765" in url


# ── csrf_header graceful degradation ────────────────────────────────

class _FakeCookieJar:
    def __init__(self, cookies):
        self._cookies = cookies

    def __iter__(self):
        return iter(self._cookies)


class _FakeCookie:
    def __init__(self, key, value):
        self.key = key
        self.value = value


class _FakeSession:
    def __init__(self, cookies):
        self.cookie_jar = _FakeCookieJar(cookies)


class _FakeLoginWithSession:
    def __init__(self, cookies):
        self.session = _FakeSession(cookies)


def test_csrf_header_finds_csrf_cookie():
    login = _FakeLoginWithSession([_FakeCookie("csrf", "tok123"),
                                    _FakeCookie("other", "x")])
    result = session.csrf_header(login)
    assert result == {"csrf": "tok123"}


def test_csrf_header_no_csrf_cookie_returns_empty():
    login = _FakeLoginWithSession([_FakeCookie("other", "x")])
    result = session.csrf_header(login)
    assert result == {}


def test_csrf_header_no_cookies_returns_empty():
    login = _FakeLoginWithSession([])
    result = session.csrf_header(login)
    assert result == {}


def test_csrf_header_no_session_attr_returns_empty():
    """If login has no session attribute, csrf_header must not crash."""
    class _BareLogin:
        pass
    result = session.csrf_header(_BareLogin())
    assert result == {}


# ── test_loggedin: no cookie -> False ───────────────────────────────

class _NoCookieLogin:
    async def load_cookie(self, *a, **k):
        return None
    async def login(self, *a, **k):
        pass
    async def test_loggedin(self, *a, **k):
        raise AssertionError("should not test when no cookie")
    async def close(self):
        pass


def test_test_loggedin_no_cookie_returns_false(monkeypatch):
    # Patch _import_alexapy so AlexaLogin() returns fake_login
    def _import():
        class _FakeAlexaLogin:
            def __init__(self, *a, **k):
                pass
            def __new__(cls, *a, **k):
                return fake_login
        return _FakeAlexaLogin, None
    monkeypatch.setattr(session, "_import_alexapy", _import)

    # Also mock _import_alexapy since proxy_login calls it first
    monkeypatch.setattr(session, "build_login", lambda *a, **k: _NoCookieLogin())
    result = asyncio.run(session.test_loggedin("you@example.com",
                                                reload_attempts=1, reload_sleep=0))
    assert result is False


# ── test_loggedin: swallows exceptions -> False ──────────────────────

class _ExplodingLogin:
    async def load_cookie(self, *a, **k):
        return {"session-id": "x"}
    async def login(self, *a, **k):
        pass
    async def test_loggedin(self, *a, **k):
        raise RuntimeError("network exploded")
    async def close(self):
        pass


def test_test_loggedin_swallows_exception_returns_false(monkeypatch):
    """test_loggedin must never raise — it returns False on any error."""
    monkeypatch.setattr(session, "build_login", lambda *a, **k: _ExplodingLogin())
    result = asyncio.run(session.test_loggedin("you@example.com",
                                                reload_attempts=1, reload_sleep=0))
    assert result is False


# ── load_session: no cookie -> friendly error ───────────────────────

class _NoCookieLoginForLoad:
    async def load_cookie(self, *a, **k):
        return None
    async def login(self, *a, **k):
        raise AssertionError("login() should not be called when no cookie")
    async def test_loggedin(self, *a, **k):
        raise AssertionError("should not test when no cookie")
    async def close(self):
        pass


def test_load_session_no_cookie_raises_friendly_error(monkeypatch):
    monkeypatch.setattr(session, "build_login", lambda *a, **k: _NoCookieLoginForLoad())
    with pytest.raises(session.AlexaSessionError) as exc:
        asyncio.run(session.load_session("you@example.com",
                                          reload_attempts=1, reload_sleep=0))
    assert "no saved cookie" in str(exc.value)


# ── load_session: closes session on error ────────────────────────────

class _CloseTrackingLogin:
    def __init__(self):
        self.closed = False
    async def load_cookie(self, *a, **k):
        return {"session-id": "x"}
    async def login(self, *a, **k):
        pass
    async def test_loggedin(self, *a, **k):
        return False
    async def close(self):
        self.closed = True


def test_load_session_closes_session_on_stale_cookie(monkeypatch):
    """The half-open session must be closed when the cookie is stale."""
    fake = _CloseTrackingLogin()
    monkeypatch.setattr(session, "build_login", lambda *a, **k: fake)
    with pytest.raises(session.AlexaSessionError):
        asyncio.run(session.load_session("you@example.com",
                                          reload_attempts=1, reload_sleep=0))
    assert fake.closed is True


# ── test_loggedin: recovers after reload (False then True) ──────────

class _RecoveryLogin:
    def __init__(self):
        self._results = [False, True]
        self.close_called = False
    async def load_cookie(self, *a, **k):
        return {"session-id": "x"}
    async def login(self, *a, **k):
        pass
    async def test_loggedin(self, *a, **k):
        return self._results.pop(0) if self._results else False
    async def close(self):
        self.close_called = True


def test_test_loggedin_recovers_after_reload(monkeypatch):
    fake = _RecoveryLogin()
    monkeypatch.setattr(session, "build_login", lambda *a, **k: fake)
    result = asyncio.run(session.test_loggedin("you@example.com",
                                                reload_attempts=3, reload_sleep=0))
    assert result is True
    assert fake.close_called is True



# ── fresh_login: captcha / 2FA / error / success branches ──────────

class _FreshFakeLogin:
    """Stand-in for the (url, email, password, output_path) AlexaLogin ctor."""

    def __init__(self, *a, url="", email="", password="", output_path="", **kwargs):
        self._statuses: list[dict] = []
        self.status: dict = {}
        self.set_totp_calls = 0
        self.login_calls = 0

    def set_totp(self, secret: str) -> None:
        self.set_totp_calls += 1

    async def login(self, *a, data=None, **k):
        self.login_calls += 1
        self.status = self._statuses.pop(0) if self._statuses else {}


def _patch_fresh_login(monkeypatch, fake):
    """Patch _import_alexapy so it returns a class whose instance IS fake."""
    def _import():
        # Return a class whose __new__ always returns the same fake instance.
        # This simulates what real alexapy does: constructs and returns an object.
        class _FakeAlexaLogin(type(fake)):
            def __new__(cls, *a, **k):
                return fake
        return _FakeAlexaLogin, None
    monkeypatch.setattr(session, "_import_alexapy", _import)
    monkeypatch.setattr(session, "make_outputpath", lambda *a, **k: lambda *x: str(a[0]))


def test_fresh_login_captcha_raises_AlexaSessionError(monkeypatch):
    """When Amazon returns a captcha, fresh_login must raise with guidance."""
    fake = _FreshFakeLogin()
    fake._statuses = [{"captcha_required": True}]
    _patch_fresh_login(monkeypatch, fake)
    with pytest.raises(session.AlexaSessionError, match="Amazon returned a captcha"):
        asyncio.run(session.fresh_login("you@example.com", "pass"))


def test_fresh_login_2fa_no_secret_no_callback_raises(monkeypatch):
    """When 2FA is required but no secret or callback is available, raise."""
    fake = _FreshFakeLogin()
    fake._statuses = [{"securitycode_required": True}]
    _patch_fresh_login(monkeypatch, fake)
    with pytest.raises(session.AlexaSessionError, match="no code available"):
        asyncio.run(session.fresh_login("you@example.com", "pass"))


def test_fresh_login_2fa_callback_provides_code(monkeypatch):
    """When 2FA is required and a callback is given, the code is injected."""
    fake = _FreshFakeLogin()
    fake._statuses = [
        {"securitycode_required": True},
        {"login_successful": True},
    ]
    _patch_fresh_login(monkeypatch, fake)

    def get_code():
        return "123456"

    asyncio.run(session.fresh_login("you@example.com", "pass", otp_callback=get_code))
    assert fake.login_calls == 2


def test_fresh_login_error_message_becomes_AlexaSessionError(monkeypatch):
    """An error_message in the status dict must become an AlexaSessionError."""
    fake = _FreshFakeLogin()
    fake._statuses = [{"error_message": "Too many attempts."}]
    _patch_fresh_login(monkeypatch, fake)
    with pytest.raises(session.AlexaSessionError, match="Too many attempts"):
        asyncio.run(session.fresh_login("you@example.com", "pass"))


def test_fresh_login_incomplete_raises(monkeypatch):
    """When status never reaches login_successful, raise with diagnostic."""
    fake = _FreshFakeLogin()
    fake._statuses = [{"some": "unrecognised status"}] * 5
    _patch_fresh_login(monkeypatch, fake)
    with pytest.raises(session.AlexaSessionError, match="scripted login did not complete"):
        asyncio.run(session.fresh_login("you@example.com", "pass"))


def test_fresh_login_first_status_successful_returns_login(monkeypatch):
    """If the first status already has login_successful, return without retry."""
    fake = _FreshFakeLogin()
    fake._statuses = [{"login_successful": True}]
    _patch_fresh_login(monkeypatch, fake)
    result = asyncio.run(session.fresh_login("you@example.com", "pass"))
    assert result is fake
    assert fake.login_calls == 1


def test_fresh_login_totp_secret_passed_to_set_totp(monkeypatch):
    """When otp_secret is given, it is registered before the first login call."""
    fake = _FreshFakeLogin()
    fake._statuses = [{"login_successful": True}]
    _patch_fresh_login(monkeypatch, fake)
    asyncio.run(session.fresh_login("you@example.com", "pass", otp_secret="SECRET"))
    assert fake.set_totp_calls == 1


# ── import_pickle: source is a directory → raises ───────────────────

def test_import_pickle_source_is_directory_raises(tmp_path):
    """If the source path is a directory, import_pickle must raise."""
    src = tmp_path / "somedir"
    src.mkdir()
    config = tmp_path / "config"
    config.mkdir()
    with pytest.raises(session.AlexaSessionError):
        session.import_pickle(str(src), "you@example.com", config_dir=config)


# ── proxy_login: timeout and exception-in-poll paths ──────────────

class _ProxyFakeLogin:
    def __init__(self, test_loggedin_results: list[bool]):
        self._results = list(test_loggedin_results)
        self.test_calls = 0
        self.closed = False

    async def test_loggedin(self, *a, **k):
        self.test_calls += 1
        return self._results.pop(0) if self._results else False

    async def finalize_login(self, *a, **k):
        pass

    async def close(self):
        self.closed = True

    @property
    def url(self):
        return "amazon.co.uk"

    session = property(lambda self: object())


class _ProxyFakeProxy:
    def __init__(self, login, access_url_str: str):
        self._login = login
        self._access_url_str = access_url_str

    def access_url(self):
        return self._access_url_str

    def change_login(self, *a):
        pass

    async def start_proxy(self, host=""):
        pass

    async def stop_proxy(self):
        pass


def test_proxy_login_timeout_raises_AlexaSessionError(monkeypatch):
    """When polling times out, proxy_login must raise with a clear message."""
    fake_login = _ProxyFakeLogin([False] * 200)
    fake_proxy = _ProxyFakeProxy(fake_login, "http://127.0.0.1:3001")

    # Patch _import_alexapy so AlexaLogin() returns fake_login
    def _import():
        class _FakeAlexaLogin:
            def __init__(self, *a, **k):
                pass
            def __new__(cls, *a, **k):
                return fake_login
        return _FakeAlexaLogin, None
    monkeypatch.setattr(session, "_import_alexapy", _import)

    # Patch alexapy.AlexaProxy at the session module level where it's imported
    import types
    fake_alexapy = types.ModuleType("alexapy")
    fake_alexapy.AlexaProxy = lambda *a, **k: fake_proxy
    # Replace sys.modules entry AND stash original for cleanup
    import sys
    _orig_alexapy = sys.modules.get("alexapy")
    monkeypatch.setitem(sys.modules, "alexapy", fake_alexapy)
    # monkeypatch cleanup handles sys.modules["alexapy"] restoration

    with pytest.raises(session.AlexaSessionError, match="timed out"):
        asyncio.run(session.proxy_login(
            "you@example.com", timeout=0.01, poll_interval=0.001))


def test_proxy_login_test_loggedin_exception_keeps_polling(monkeypatch):
    """If test_loggedin raises, proxy_login must continue polling, not fail."""
    call_count = [0]

    class _RaiseOnceLogin:
        def __init__(self):
            self.closed = False

        async def test_loggedin(self, *a, **k):
            if call_count[0] == 0:
                call_count[0] += 1
                raise RuntimeError("transient network glitch")
            return True  # succeeds on second call

        async def finalize_login(self, *a, **k):
            pass

        async def close(self):
            self.closed = True

        @property
        def url(self):
            return "amazon.co.uk"

        session = property(lambda self: object())

    fake_login = _RaiseOnceLogin()
    fake_proxy = _ProxyFakeProxy(fake_login, "http://127.0.0.1:3001")

    async def mock_build_login(*a, **k):
        return fake_login

    # proxy_login calls _import_alexapy first, so we must mock it
    def _mock_import_alexapy():
        class _FakeAlexaLogin:
            def __new__(cls, *a, **k):
                return fake_login
        return _FakeAlexaLogin, None
    monkeypatch.setattr(session, "_import_alexapy", _mock_import_alexapy)
    monkeypatch.setattr(session, "build_login", mock_build_login)
    import sys
    import types
    fake_alexapy = types.ModuleType("alexapy")
    fake_alexapy.AlexaProxy = lambda *a, **k: fake_proxy
    monkeypatch.setitem(sys.modules, "alexapy", fake_alexapy)

    asyncio.run(session.proxy_login(
        "you@example.com", timeout=1.0, poll_interval=0.01))
    assert call_count[0] == 1  # first raised, second succeeded

def test__default_config_dir_no_home(monkeypatch):
    from cli_anything.alexa.core.session import _default_config_dir, FALLBACK_CONFIG_DIR
    monkeypatch.setenv("HOME", "")
    res = _default_config_dir()
    assert res == FALLBACK_CONFIG_DIR

