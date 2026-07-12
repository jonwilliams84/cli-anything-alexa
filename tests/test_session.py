"""Unit tests for the cookie/config-dir resolution + stale-auth retry.

Pure logic only — no alexapy, no live account. Covers:

  * ``resolve_config_dir`` precedence: --cookie-dir > env > valid $HOME >
    /tmp fallback, plus the unset-$HOME case (write-dir == read-dir).
  * the --cookie-dir -> alexapy ``outputpath`` mapping (HA layout:
    ``<dir>/.storage/alexa_media.<email>.pickle``).
  * the HA-rotation auto-recovery in ``load_session`` / ``test_loggedin``:
    False-then-True succeeds after a re-load; False-throughout raises the
    friendly error AND never re-calls ``login()`` past the cap.
"""

import asyncio

import pytest

from cli_anything.alexa.core import session


# ── config-dir resolution precedence ─────────────────────────────────────

def test_resolve_cookie_dir_flag_wins(monkeypatch):
    monkeypatch.setenv("CLI_ALEXA_COOKIE_DIR", "/env/dir")
    monkeypatch.setenv("HOME", "/home/someone")
    assert session.resolve_config_dir("/flag/dir") == session.Path("/flag/dir")


def test_resolve_cookie_dir_env_beats_home(monkeypatch, tmp_path):
    monkeypatch.setenv("CLI_ALEXA_COOKIE_DIR", str(tmp_path / "envdir"))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert session.resolve_config_dir(None) == (tmp_path / "envdir")


def test_resolve_cookie_dir_valid_home(monkeypatch, tmp_path):
    monkeypatch.delenv("CLI_ALEXA_COOKIE_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # tmp_path is a real dir
    assert session.resolve_config_dir(None) == (
        tmp_path / ".config" / "cli-anything-alexa"
    )


def test_resolve_cookie_dir_unset_home_falls_back(monkeypatch):
    """Unset $HOME -> deterministic /tmp fallback (write-dir == read-dir)."""
    monkeypatch.delenv("CLI_ALEXA_COOKIE_DIR", raising=False)
    monkeypatch.delenv("HOME", raising=False)
    write_dir = session.resolve_config_dir(None)
    read_dir = session.resolve_config_dir(None)
    assert write_dir == session.FALLBACK_CONFIG_DIR == session.Path(
        "/tmp/cli-anything-alexa")
    # Two independent resolutions agree (the in-pod write/read disagreement bug).
    assert write_dir == read_dir


def test_resolve_cookie_dir_root_home_falls_back(monkeypatch):
    """HOME='/' is treated as unusable -> fallback (the container quirk)."""
    monkeypatch.delenv("CLI_ALEXA_COOKIE_DIR", raising=False)
    monkeypatch.setenv("HOME", "/")
    assert session.resolve_config_dir(None) == session.FALLBACK_CONFIG_DIR


def test_resolve_cookie_dir_expanduser(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert session.resolve_config_dir("~/sub") == (tmp_path / "sub")


# ── --cookie-dir -> alexapy outputpath / cookie path mapping ─────────────

def test_cookie_path_in_dir_ha_layout():
    p = session.cookie_path_in_dir("/config", "you@example.com")
    assert str(p) == "/config/.storage/alexa_media.you@example.com.pickle"


def test_make_outputpath_matches_alexapy_first_cookiefile(tmp_path):
    """outputpath(...) must produce alexapy's _cookiefile[0] HA-layout path."""
    op = session.make_outputpath(tmp_path, create=False)
    email = "you@example.com"
    # exactly how alexapy builds _cookiefile[0]
    got = op(f".storage/alexa_media.{email}.pickle")
    assert got == str(tmp_path / ".storage" / f"alexa_media.{email}.pickle")
    assert got == str(session.cookie_path_in_dir(tmp_path, email))


def test_make_outputpath_no_create_does_not_mkdir(tmp_path):
    """Read-in-place must not create the (foreign) cookie dir."""
    target = tmp_path / "foreign_config"
    session.make_outputpath(target, create=False)
    assert not target.exists()


def test_make_outputpath_create_makes_dir(tmp_path):
    target = tmp_path / "owned_config"
    session.make_outputpath(target, create=True)
    assert target.is_dir()


# ── stale-auth retry decision ────────────────────────────────────────────

class _FakeLogin:
    """Minimal AlexaLogin stand-in recording login()/test_loggedin() calls.

    ``test_results`` is consumed one per ``test_loggedin``; once exhausted the
    last value repeats. ``load_cookie`` always returns a truthy cookie.
    """

    def __init__(self, test_results):
        self._results = list(test_results)
        self.login_calls = 0
        self.test_calls = 0
        self.load_calls = 0
        self.closed = False

    async def load_cookie(self, *a, **k):
        self.load_calls += 1
        return {"session-id": "x"}

    async def login(self, *a, **k):
        self.login_calls += 1

    async def test_loggedin(self, *a, **k):
        self.test_calls += 1
        if self._results:
            return self._results.pop(0)
        return False

    async def close(self):
        self.closed = True


def _patch_build_login(monkeypatch, fake):
    monkeypatch.setattr(session, "build_login", lambda *a, **k: fake)


def test_load_session_recovers_after_reload(monkeypatch):
    """False-then-True -> succeeds; login() called exactly once (no re-login)."""
    fake = _FakeLogin([False, True])
    _patch_build_login(monkeypatch, fake)
    out = asyncio.run(session.load_session(
        "you@example.com", reload_attempts=3, reload_sleep=0))
    assert out is fake
    assert fake.login_calls == 1          # only ONE login(), never re-login
    assert fake.test_calls == 2           # re-tested after a re-load
    assert fake.load_calls == 2           # initial load + one reload


def test_load_session_gives_up_after_cap(monkeypatch):
    """False throughout -> friendly error after the bounded attempts."""
    fake = _FakeLogin([False, False, False, False, False])
    _patch_build_login(monkeypatch, fake)
    with pytest.raises(session.AlexaSessionError) as exc:
        asyncio.run(session.load_session(
            "you@example.com", reload_attempts=3, reload_sleep=0))
    assert "no longer valid" in str(exc.value)
    assert fake.login_calls == 1          # NEVER re-login in a tight loop
    assert fake.test_calls == 3           # exactly the cap
    assert fake.closed                    # session closed on the friendly abort


def test_test_loggedin_recovers_after_reload(monkeypatch):
    fake = _FakeLogin([False, True])
    _patch_build_login(monkeypatch, fake)
    assert asyncio.run(session.test_loggedin(
        "you@example.com", reload_attempts=3, reload_sleep=0)) is True
    assert fake.login_calls == 1
    assert fake.test_calls == 2


def test_test_loggedin_false_throughout(monkeypatch):
    fake = _FakeLogin([False, False, False, False])
    _patch_build_login(monkeypatch, fake)
    assert asyncio.run(session.test_loggedin(
        "you@example.com", reload_attempts=3, reload_sleep=0)) is False
    assert fake.login_calls == 1          # capped, no re-login storm
    assert fake.test_calls == 3


# ── Amazon region host allow-list (SSRF / credential-redirect guard) ─────

def test_validate_region_accepts_known_hosts():
    """All allow-listed Amazon domains are accepted and normalized to bare form."""
    for host in ("amazon.co.uk", "amazon.com", "amazon.de", "amazon.com.au"):
        assert session.validate_region(host) == host


def test_validate_region_accepts_alexa_prefix_and_scheme():
    """``alexa.amazon.co.uk`` and ``https://alexa.amazon.fr`` normalize correctly."""
    assert session.validate_region("alexa.amazon.co.uk") == "amazon.co.uk"
    assert session.validate_region("https://alexa.amazon.fr") == "amazon.fr"
    assert session.validate_region("http://alexa.amazon.it") == "amazon.it"


def test_validate_region_case_insensitive():
    assert session.validate_region("AMAZON.COM") == "amazon.com"
    assert session.validate_region("Amazon.Co.UK") == "amazon.co.uk"


def test_validate_region_strips_trailing_slash_and_dot():
    assert session.validate_region("amazon.de/") == "amazon.de"
    assert session.validate_region("amazon.de.") == "amazon.de"
    assert session.validate_region("amazon.de/path") == "amazon.de"


def test_validate_region_rejects_unknown_host():
    """An attacker-controlled / typo host must be rejected (SSRF guard)."""
    with pytest.raises(session.AlexaSessionError) as exc:
        session.validate_region("evil.com")
    assert "unsupported Amazon region host" in str(exc.value)


def test_validate_region_rejects_alexa_prefixed_unknown():
    with pytest.raises(session.AlexaSessionError):
        session.validate_region("alexa.attacker.com")


def test_validate_region_rejects_empty_and_none():
    with pytest.raises(session.AlexaSessionError):
        session.validate_region("")
    with pytest.raises(session.AlexaSessionError):
        session.validate_region(None)


def test_base_url_validates_host():
    """base_url must reject unknown hosts (not just build any URL)."""
    assert session.base_url("amazon.co.uk") == "https://alexa.amazon.co.uk"
    with pytest.raises(session.AlexaSessionError):
        session.base_url("evil.com")


def test_build_login_validates_url(monkeypatch):
    """build_login must reject an unknown region before constructing AlexaLogin."""
    class _FakeAlexaLogin:
        def __init__(self, *a, **k):
            raise AssertionError("AlexaLogin should not be constructed for bad url")
    monkeypatch.setattr(session, "_import_alexapy",
                        lambda: (_FakeAlexaLogin, object()))
    with pytest.raises(session.AlexaSessionError):
        session.build_login("you@example.com", url="evil.com")


def test_load_session_validates_url(monkeypatch):
    """load_session must reject an unknown region before any network call."""
    class _FakeLogin:
        async def load_cookie(self, *a, **k):
            raise AssertionError("should not reach load_cookie for bad url")
        async def login(self, *a, **k):
            raise AssertionError("should not reach login for bad url")
        async def test_loggedin(self, *a, **k):
            raise AssertionError("should not reach test_loggedin for bad url")
        async def close(self):
            pass
    monkeypatch.setattr(session, "build_login",
                        lambda *a, **k: _FakeLogin())
    with pytest.raises(session.AlexaSessionError):
        asyncio.run(session.load_session("you@example.com", url="evil.com",
                                          reload_attempts=1, reload_sleep=0))


def test_test_loggedin_validates_url(monkeypatch):
    """test_loggedin must reject an unknown region before any network call."""
    class _FakeLogin:
        async def load_cookie(self, *a, **k):
            raise AssertionError("should not reach load_cookie for bad url")
        async def login(self, *a, **k):
            raise AssertionError("should not reach login for bad url")
        async def test_loggedin(self, *a, **k):
            raise AssertionError("should not reach test_loggedin for bad url")
        async def close(self):
            pass
    monkeypatch.setattr(session, "build_login",
                        lambda *a, **k: _FakeLogin())
    # test_loggedin swallows exceptions and returns False, but validate_region
    # raises AlexaSessionError BEFORE build_login is called, so it propagates.
    with pytest.raises(session.AlexaSessionError):
        asyncio.run(session.test_loggedin("you@example.com", url="evil.com",
                                           reload_attempts=1, reload_sleep=0))


def test_fresh_login_validates_url(monkeypatch):
    """fresh_login must reject an unknown region before constructing AlexaLogin."""
    class _FakeAlexaLogin:
        def __init__(self, *a, **k):
            raise AssertionError("AlexaLogin should not be constructed for bad url")
    monkeypatch.setattr(session, "_import_alexapy",
                        lambda: (_FakeAlexaLogin, object()))
    with pytest.raises(session.AlexaSessionError):
        asyncio.run(session.fresh_login("you@example.com", "pass",
                                        url="evil.com"))


def test_proxy_login_validates_url(monkeypatch):
    """proxy_login must reject an unknown region before constructing AlexaLogin."""
    class _FakeAlexaLogin:
        def __init__(self, *a, **k):
            raise AssertionError("AlexaLogin should not be constructed for bad url")
    monkeypatch.setattr(session, "_import_alexapy",
                        lambda: (_FakeAlexaLogin, object()))
    with pytest.raises(session.AlexaSessionError):
        asyncio.run(session.proxy_login("you@example.com", url="evil.com"))
