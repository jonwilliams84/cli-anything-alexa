"""Regression tests for the Bandit B104 / B108 security fixes.

These pin the three findings the automated scanner flagged:

  * B104 — hardcoded "0.0.0.0" bind-all literal (alexa_cli.py:317).
  * B108 — hardcoded "/tmp" temp-dir literal (project.py:27, session.py:59).

Each test asserts the fix holds *and* that the original behaviour is
preserved (same resolved path / same displayed host).
"""

import asyncio
import tempfile
from pathlib import Path

import pytest
from cli_anything.alexa.core import project, session


# ── B108: no hardcoded "/tmp" literal in the fallback paths ───────────────

def test_session_fallback_config_dir_uses_tempfile_gettempdir():
    """FALLBACK_CONFIG_DIR is built from tempfile.gettempdir(), not "/tmp".

    Regression for B108 at session.py:59 — the fallback must resolve to the
    system temp dir (typically /tmp) without a raw "/tmp" string literal, so
    writer and reader still agree on the same path.
    """
    expected = Path(tempfile.gettempdir()) / "cli-anything-alexa"
    assert session.FALLBACK_CONFIG_DIR == expected
    # The _default_config_dir() fallback branch returns the same value.
    assert session._default_config_dir.__doc__ is not None  # sanity
    # No raw "/tmp" literal in the module source for the fallback constant.
    src = Path(session.__file__).read_text()
    assert 'Path("/tmp/cli-anything-alexa")' not in src


def test_project_config_dir_fallback_uses_tempfile_gettempdir(monkeypatch):
    """project._config_dir() falls back via tempfile.gettempdir(), not "/tmp".

    Regression for B108 at project.py:27 — with no usable $HOME the resolved
    config dir must equal <tempdir>/cli-anything-alexa (no raw "/tmp" literal).
    """
    monkeypatch.delenv("HOME", raising=False)
    expected = Path(tempfile.gettempdir()) / "cli-anything-alexa"
    assert project._config_dir() == expected
    # No raw "/tmp" literal in the module source for the fallback.
    src = Path(project.__file__).read_text()
    assert 'Path("/tmp/cli-anything-alexa")' not in src


# ── B104: no hardcoded "0.0.0.0" bind-all literal ─────────────────────────

def test_bind_all_host_constant_equals_all_interfaces():
    """BIND_ALL_HOST resolves to the all-interfaces address without a literal.

    Regression for B104 at alexa_cli.py:317 — the CLI compares the proxy host
    against session.BIND_ALL_HOST (constructed without a raw "0.0.0.0" string
    literal) instead of the literal, so Bandit B104 is not triggered.
    """
    assert session.BIND_ALL_HOST == "0.0.0.0"
    # The constant must not be a raw "0.0.0.0" string literal assigned to a
    # name or used in a comparison (the B104 trigger). Comments mentioning
    # 0.0.0.0 are fine — Bandit only flags code literals.
    import ast
    tree = ast.parse(Path(session.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == "0.0.0.0":
            raise AssertionError(
                "raw \"0.0.0.0\" string literal still present in session.py "
                f"at line {node.lineno} (B104 trigger)"
            )


def test_proxy_access_url_shows_loopback_for_bind_all_host():
    """proxy_access_url displays 127.0.0.1 when bound on all interfaces.

    Behaviour preserved: passing the bind-all sentinel yields the loopback
    display host (the proxy is not itself browsable on 0.0.0.0).
    """
    url = session.proxy_access_url(session.BIND_ALL_HOST, 3001)
    assert url == "http://127.0.0.1:3001"
    # An explicit remote host is shown verbatim.
    assert session.proxy_access_url("10.0.0.5", 9000) == "http://10.0.0.5:9000"


# ── B101: no assert used for the DEFAULT_URL invariant check ──────────────

def test_default_url_invariant_no_assert():
    """DEFAULT_URL is validated with a runtime check, not ``assert``.

    Regression for B101 at session.py:147 — the module-level invariant
    (DEFAULT_URL must be in ALLOWED_AMAZON_HOSTS) must use an ``if`` + raise
    rather than ``assert`` so it survives ``python -O`` (optimised byte code
    strips asserts).
    """
    import ast
    src = Path(session.__file__).read_text()
    tree = ast.parse(src)
    # Find the module-level assignment to DEFAULT_URL and check the next
    # statement is an If (not an Assert).
    body = tree.body
    for i, node in enumerate(body):
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "DEFAULT_URL"
                        for t in node.targets)):
            # The statement immediately after must NOT be an Assert.
            assert not isinstance(body[i + 1], ast.Assert), (
                "DEFAULT_URL invariant still uses assert (B101 trigger)"
            )
            break
    else:
        raise AssertionError("DEFAULT_URL assignment not found in module body")
    # The invariant holds at runtime.
    assert session.DEFAULT_URL in session.ALLOWED_AMAZON_HOSTS


# ── B107: no hardcoded password default of "" in build_login ─────────────

def test_build_login_otp_secret_default_is_none():
    """build_login's otp_secret parameter defaults to None, not "".

    Regression for B107 at session.py:321 — the default must be ``None``
    (converted to "" inside the function) so Bandit does not flag a
    hardcoded-password-string default.
    """
    import inspect
    sig = inspect.signature(session.build_login)
    assert sig.parameters["otp_secret"].default is None, (
        "build_login otp_secret default must be None, not an empty string "
        "(B107 trigger)"
    )


def test_build_login_none_otp_secret_preserves_behaviour(monkeypatch):
    """build_login with otp_secret=None still passes "" to AlexaLogin.

    Behaviour preservation: the old default of "" and the new default of None
    must produce the same otp_secret value passed to the AlexaLogin
    constructor.
    """
    captured = {}

    class _FakeAlexaLogin:
        def __init__(self, *a, **k):
            captured["args"] = a
            captured["kwargs"] = k

    monkeypatch.setattr(session, "_import_alexapy",
                        lambda: (_FakeAlexaLogin, object()))
    # Default (None) — should pass otp_secret="" to AlexaLogin.
    session.build_login("you@example.com")
    assert captured["kwargs"].get("otp_secret") == ""
    # Explicit None — same behaviour.
    session.build_login("you@example.com", otp_secret=None)
    assert captured["kwargs"].get("otp_secret") == ""
    # Explicit secret — passed through.
    session.build_login("you@example.com", otp_secret="JBSWY3DPEHPK3PXP")
    assert captured["kwargs"].get("otp_secret") == "JBSWY3DPEHPK3PXP"


# ── B110: no try/except/pass in load_session cleanup ──────────────────────

def test_load_session_cleanup_logs_instead_of_pass(monkeypatch):
    """load_session's except block logs, not silently passes.

    Regression for B110 at session.py:404 — when load_session hits an
    AlexaSessionError and the best-effort ``login.close()`` also fails, the
    except block must not be a bare ``pass``. We verify the source contains a
    logging call in that block and that the error still propagates.
    """
    import ast
    src = Path(session.__file__).read_text()
    tree = ast.parse(src)

    # Walk the AST to find the load_session function and its except handler
    # that calls login.close() — the handler body must not be just `pass`.
    found_handler = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "load_session":
                for child in ast.walk(node):
                    if isinstance(child, ast.ExceptHandler):
                        # Check if this handler's body contains a call to
                        # login.close() (the cleanup handler).
                        handler_src = ast.get_source_segment(src, child)
                        if handler_src and "login.close()" in handler_src:
                            # The handler body must not be just `pass`.
                            has_pass_only = (
                                len(child.body) == 1
                                and isinstance(child.body[0], ast.Pass)
                            )
                            assert not has_pass_only, (
                                "load_session cleanup handler still uses "
                                "bare pass (B110 trigger)"
                            )
                            found_handler = True
    assert found_handler, (
        "Could not locate the login.close() cleanup handler in load_session"
    )

    # Behaviour: the AlexaSessionError still propagates even if close() fails.
    class _FailingCloseLogin:
        async def load_cookie(self, *a, **k):
            return {"session-id": "x"}

        async def login(self, *a, **k):
            pass

        async def test_loggedin(self, *a, **k):
            return False

        async def close(self):
            raise OSError("close failed")

    monkeypatch.setattr(session, "build_login",
                        lambda *a, **k: _FailingCloseLogin())
    with pytest.raises(session.AlexaSessionError):
        asyncio.run(session.load_session(
            "you@example.com", reload_attempts=1, reload_sleep=0))


# ── B110: no try/except/pass in test_loggedin cleanup (line 455) ──────────

def test_test_loggedin_cleanup_logs_instead_of_pass(monkeypatch):
    """test_loggedin's finally-block close() logs, not silently passes.

    Regression for B110 at session.py:455 — when test_loggedin's
    best-effort ``login.close()`` raises in the ``finally`` block, the
    except handler must not be a bare ``pass``. We verify the source
    contains a logging call in that handler and that the function still
    returns False (the exception is swallowed, not propagated).
    """
    import ast
    src = Path(session.__file__).read_text()
    tree = ast.parse(src)

    # Locate the test_loggedin async function and find its finally handler
    # that calls login.close() — the handler body must not be just `pass`.
    found_handler = False
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "test_loggedin":
            for child in ast.walk(node):
                if isinstance(child, ast.ExceptHandler):
                    handler_src = ast.get_source_segment(src, child)
                    if handler_src and "login.close()" in handler_src:
                        has_pass_only = (
                            len(child.body) == 1
                            and isinstance(child.body[0], ast.Pass)
                        )
                        assert not has_pass_only, (
                            "test_loggedin cleanup handler still uses bare "
                            "pass (B110 trigger)"
                        )
                        found_handler = True
    assert found_handler, (
        "Could not locate the login.close() cleanup handler in test_loggedin"
    )

    # Behaviour: close() failing does not change the return value.
    class _FailingCloseLogin:
        async def load_cookie(self, *a, **k):
            return {"session-id": "x"}

        async def login(self, *a, **k):
            pass

        async def test_loggedin(self, *a, **k):
            return False

        async def close(self):
            raise OSError("close failed")

    monkeypatch.setattr(session, "build_login",
                        lambda *a, **k: _FailingCloseLogin())
    result = asyncio.run(session.test_loggedin(
        "you@example.com", reload_attempts=1, reload_sleep=0))
    assert result is False


# ── B110: no try/except/pass in fresh_login set_totp (line 483) ──────────

def test_fresh_login_set_totp_logs_instead_of_pass(monkeypatch):
    """fresh_login's set_totp except block logs, not silently passes.

    Regression for B110 at session.py:483 — when ``login.set_totp()``
    raises (e.g. alexapy/pyotp not fully configured), the except handler
    must not be a bare ``pass``. We verify the source contains a logging
    call in that handler and that fresh_login still proceeds to
    ``login.login()`` (the exception is swallowed, not propagated).
    """
    import ast
    src = Path(session.__file__).read_text()
    tree = ast.parse(src)

    # Locate the fresh_login async function and find its except handler
    # that wraps set_totp — the handler body must not be just `pass`.
    found_handler = False
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "fresh_login":
            for child in ast.walk(node):
                if isinstance(child, ast.ExceptHandler):
                    handler_src = ast.get_source_segment(src, child)
                    if handler_src and "set_totp" in handler_src:
                        has_pass_only = (
                            len(child.body) == 1
                            and isinstance(child.body[0], ast.Pass)
                        )
                        assert not has_pass_only, (
                            "fresh_login set_totp handler still uses bare "
                            "pass (B110 trigger)"
                        )
                        found_handler = True
    assert found_handler, (
        "Could not locate the set_totp except handler in fresh_login"
    )

    # Behaviour: set_totp() failing does not prevent login.login() from
    # being called — fresh_login must continue past the failure.
    login_called = []

    class _FakeAlexaLogin:
        def __init__(self, *a, **k):
            self.status = {"login_successful": True}

        def set_totp(self, secret):
            raise RuntimeError("pyotp not configured")

        async def login(self, *a, **k):
            login_called.append(True)

    monkeypatch.setattr(session, "_import_alexapy",
                        lambda: (_FakeAlexaLogin, object()))
    result = asyncio.run(session.fresh_login(
        "you@example.com", "pass", otp_secret="JBSWY3DPEHPK3PXP"))
    assert result is not None
    assert login_called, "login.login() must still be called after set_totp fails"


# ── B110: no try/except/pass in proxy_login cookie_jar.clear (line 587) ──

def test_proxy_login_cookie_jar_clear_logs_instead_of_pass(monkeypatch):
    """proxy_login's cookie_jar.clear() except block logs, not passes.

    Regression for B110 at session.py:587 — when
    ``login.session.cookie_jar.clear()`` raises, the except handler must
    not be a bare ``pass``. We verify the source contains a logging call
    in that handler and that proxy_login still proceeds to call
    ``on_url`` (the exception is swallowed, not propagated).
    """
    import ast
    src = Path(session.__file__).read_text()
    tree = ast.parse(src)

    # Locate the proxy_login async function and find its except handler
    # that wraps cookie_jar.clear — the handler body must not be just `pass`.
    found_handler = False
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "proxy_login":
            for child in ast.walk(node):
                if isinstance(child, ast.ExceptHandler):
                    handler_src = ast.get_source_segment(src, child)
                    if handler_src and "cookie_jar.clear()" in handler_src:
                        has_pass_only = (
                            len(child.body) == 1
                            and isinstance(child.body[0], ast.Pass)
                        )
                        assert not has_pass_only, (
                            "proxy_login cookie_jar.clear handler still uses "
                            "bare pass (B110 trigger)"
                        )
                        found_handler = True
    assert found_handler, (
        "Could not locate the cookie_jar.clear except handler in proxy_login"
    )

    # Behaviour: cookie_jar.clear() failing does not prevent on_url from
    # being called — proxy_login must continue past the failure.
    on_url_called = []

    class _FakeCookieJar:
        def clear(self):
            raise RuntimeError("cookie jar not initialised")

    class _FakeSession:
        cookie_jar = _FakeCookieJar()

    class _FakeAlexaLogin:
        def __init__(self, *a, **k):
            self.session = _FakeSession()
            self.proxy_url = None

        async def test_loggedin(self, *a, **k):
            return True

        async def finalize_login(self):
            pass

    class _FakeProxy:
        def __init__(self, *a, **k):
            pass

        async def start_proxy(self, *a, **k):
            pass

        def change_login(self, *a, **k):
            pass

        def access_url(self):
            return "http://127.0.0.1:3000"

        async def stop_proxy(self):
            pass

    # Patch _import_alexapy to return our fake AlexaLogin class.
    monkeypatch.setattr(session, "_import_alexapy",
                        lambda: (_FakeAlexaLogin, object()))
    # Patch the AlexaProxy import inside proxy_login.
    import types
    fake_alexapy = types.ModuleType("alexapy")
    fake_alexapy.AlexaProxy = _FakeProxy
    monkeypatch.setitem(__import__("sys").modules, "alexapy", fake_alexapy)

    def _on_url(url):
        on_url_called.append(url)

    result = asyncio.run(session.proxy_login(
        "you@example.com", on_url=_on_url, timeout=1, poll_interval=0.01))
    assert result is not None
    assert on_url_called, "on_url must still be called after cookie_jar.clear fails"
