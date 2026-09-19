"""A small site with a real login, for driving the login handler through a real browser.

Two HTTP servers run in threads:

* the **app** (``app_url``) — public pages (``/``, ``/about``), a login page, and protected
  pages under ``/account`` that need a session cookie and otherwise redirect to ``/login``;
* an **identity provider** (``idp_url``) — ``/oauth/authorize`` for SSO, plus a plain
  ``/login`` that is *not* an SSO endpoint (reachable as ``evil_url``, a different hostname).

Behaviour is switched by constructor arguments; every credential submission is counted on the
server side (``login_posts``, ``idp_posts``, ``evil_posts``, ``mfa_posts``) so tests can prove
how many times a password was really sent.
"""

from __future__ import annotations

import http.server
import secrets
import threading
from urllib.parse import parse_qs, quote, urlsplit

USER = "alice@example.com"
PASSWORD = "Zx9-Q7-PASSWORD-UNIQUE"

PROTECTED = ("/account", "/account/orders", "/account/settings")


def _page(title: str, body: str) -> bytes:
    return (
        f"<!doctype html><html><head><meta charset=utf-8><title>{title}</title>"
        f"<style>body{{font:14px sans-serif}}</style></head><body>{body}</body></html>"
    ).encode()


class LoginSite:
    """
    ``mode``: ``"password"`` (a username/password form), ``"sso"`` (a "Sign in with SSO" button
    that goes to the identity provider), or ``"mfa"`` (a correct password leads to a one-time-
    code prompt that can't be completed).

    ``idp_flow``: ``"password"`` or ``"identifier-first"`` (username and password on separate
    screens). ``idp_postback``: return to the app with an auto-submitting POST form instead of
    a redirect. ``expire_after=N``: the Nth authenticated request to a protected page finds the
    session gone (once). ``bounce_to_untrusted``: unauthenticated visits are sent to a login
    page on another host that is not an SSO endpoint.
    """

    def __init__(
        self,
        *,
        mode: str = "password",
        idp_flow: str = "password",
        idp_postback: bool = False,
        expire_after: int | None = None,
        bounce_to_untrusted: bool = False,
    ) -> None:
        self.mode = mode
        self.idp_flow = idp_flow
        self.idp_postback = idp_postback
        self.expire_after = expire_after
        self.bounce_to_untrusted = bounce_to_untrusted

        self.sessions: set[str] = set()
        self.codes: set[str] = set()
        self.login_posts = 0  # password submissions to the app's own login form
        self.idp_posts = 0  # password submissions to the identity provider
        self.evil_posts = 0  # password submissions to the untrusted login page
        self.mfa_posts = 0
        self.protected_ok = 0  # authenticated requests served for protected pages
        self._expired = False

        self._app = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self._app_handler())
        self._idp = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self._idp_handler())
        for server in (self._app, self._idp):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        self.app_url = f"http://127.0.0.1:{self._app.server_address[1]}"
        self.idp_url = f"http://127.0.0.1:{self._idp.server_address[1]}"
        self.evil_url = f"http://localhost:{self._idp.server_address[1]}"

    def close(self) -> None:
        for server in (self._app, self._idp):
            server.shutdown()
            server.server_close()

    # -- shared helpers --------------------------------------------------------------- #

    @staticmethod
    def _send(handler, body: bytes, status: int = 200, headers: dict | None = None) -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            handler.send_header(key, value)
        handler.end_headers()
        handler.wfile.write(body)

    @staticmethod
    def _redirect(handler, location: str, headers: dict | None = None) -> None:
        handler.send_response(302)
        handler.send_header("Location", location)
        handler.send_header("Content-Length", "0")
        for key, value in (headers or {}).items():
            handler.send_header(key, value)
        handler.end_headers()

    @staticmethod
    def _form(handler) -> dict[str, str]:
        length = int(handler.headers.get("Content-Length", 0))
        raw = handler.rfile.read(length).decode()
        return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

    @staticmethod
    def _cookie(handler, name: str) -> str | None:
        for part in handler.headers.get("Cookie", "").split(";"):
            key, _, value = part.strip().partition("=")
            if key == name:
                return value
        return None

    def _open_session(self) -> dict[str, str]:
        token = secrets.token_hex(8)
        self.sessions.add(token)
        return {"Set-Cookie": f"app_session={token}; Path=/; HttpOnly"}

    # -- the app ------------------------------------------------------------------------ #

    def _login_page(self, error: bool, next_path: str) -> bytes:
        if self.mode == "sso":
            target = (
                f"{self.idp_url}/oauth/authorize?client_id=app&response_type=code"
                f"&redirect_uri={quote(self.app_url + '/sso/callback')}&state=xyz"
            )
            return _page(
                "Sign in",
                "<h1>Sign in</h1><p>Your organisation uses single sign-on.</p>"
                f"<button type='button' onclick=\"location.href='{target}'\">Sign in with SSO</button>"
                "<input type='hidden' name='marker_login'>",
            )
        alert = "<div class='alert'>Invalid username or password</div>" if error else ""
        return _page(
            "Sign in",
            f"<h1>Sign in</h1>{alert}"
            "<form method='post' action='/login'>"
            f"<input type='hidden' name='next' value='{next_path}'>"
            "<label>Email <input name='username' type='text'></label>"
            "<label>Password <input name='password' type='password'></label>"
            "<button type='submit'>Sign in</button></form>"
            "<input type='hidden' name='marker_login'>",
        )

    def _protected(self, path: str) -> bytes:
        links = {
            "/account": "<a href='/account/orders'>Orders</a> <a href='/account/settings'>Settings</a>",
            "/account/orders": "<a href='/account'>Back</a>",
            "/account/settings": "<a href='/account'>Back</a>",
        }[path]
        return _page(
            f"Account {path}",
            f"<h1>My account</h1><p>SECRET-CONTENT-{path}</p>{links}"
            f"<input type='hidden' name='marker{path.replace('/', '_')}'>",
        )

    def _app_handler(self):
        site = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _need_login(self, path: str) -> None:
                if site.bounce_to_untrusted:
                    site._redirect(self, f"{site.evil_url}/login")
                else:
                    site._redirect(self, f"/login?next={quote(path)}")

            def do_GET(self):
                parts = urlsplit(self.path)
                path, query = parts.path, parse_qs(parts.query)
                if path == "/":
                    site._send(
                        self,
                        _page(
                            "Acme",
                            "<h1>Acme</h1><nav><a href='/about'>About</a> "
                            "<a href='/account'>My account</a></nav>"
                            "<input type='hidden' name='marker_home'>",
                        ),
                    )
                elif path == "/about":
                    site._send(
                        self,
                        _page(
                            "About",
                            "<h1>About us</h1><p>We make things.</p><input type='hidden' name='marker_about'>",
                        ),
                    )
                elif path == "/login":
                    site._send(
                        self, site._login_page("error" in query, query.get("next", ["/"])[0])
                    )
                elif path == "/mfa":
                    site._send(
                        self,
                        _page(
                            "Verify",
                            "<h1>Two-factor authentication</h1>"
                            "<p>Enter the code from your authenticator app</p>"
                            "<form method='post' action='/mfa'>"
                            "<input name='otp' type='text' autocomplete='one-time-code'>"
                            "<button type='submit'>Verify</button></form>",
                        ),
                    )
                elif path == "/sso/callback":
                    self._callback(query.get("code", [""])[0])
                elif path in PROTECTED:
                    token = site._cookie(self, "app_session")
                    if site.mode == "mfa" or token not in site.sessions:
                        self._need_login(path)
                        return
                    site.protected_ok += 1
                    if site.expire_after == site.protected_ok and not site._expired:
                        site._expired = True
                        site.sessions.clear()  # the session ends on the server
                        self._need_login(path)
                        return
                    site._send(self, site._protected(path))
                else:
                    site._send(self, _page("Not found", "<h1>404</h1>"), status=404)

            def _callback(self, code: str) -> None:
                if code in site.codes:
                    site._redirect(self, "/account", site._open_session())
                else:
                    site._redirect(self, "/login?error=1")

            def do_POST(self):
                path = urlsplit(self.path).path
                form = site._form(self)
                if path == "/login":
                    site.login_posts += 1
                    if form.get("username") == USER and form.get("password") == PASSWORD:
                        if site.mode == "mfa":
                            site._redirect(self, "/mfa", site._open_session())
                        else:
                            site._redirect(
                                self, form.get("next") or "/account", site._open_session()
                            )
                    else:
                        site._redirect(self, "/login?error=1")
                elif path == "/mfa":
                    site.mfa_posts += 1
                    site._redirect(self, "/mfa")
                elif path == "/sso/callback":
                    self._callback(form.get("code", ""))
                else:
                    site._send(self, _page("Not found", "<h1>404</h1>"), status=404)

        return Handler

    # -- the identity provider (and the untrusted login page) -------------------------- #

    def _idp_form(
        self, redirect_uri: str, state: str, *, step: int, identifier: str, error: bool
    ) -> bytes:
        alert = "<div class='alert'>Invalid username or password</div>" if error else ""
        hidden = (
            f"<input type='hidden' name='redirect_uri' value='{redirect_uri}'>"
            f"<input type='hidden' name='state' value='{state}'>"
            f"<input type='hidden' name='step' value='{step}'>"
        )
        if self.idp_flow == "identifier-first" and step == 1:
            fields = "<input name='identifier' type='email'><button type='submit'>Next</button>"
        elif self.idp_flow == "identifier-first":
            fields = (
                f"<input type='hidden' name='identifier' value='{identifier}'>"
                "<input name='password' type='password'><button type='submit'>Sign in</button>"
            )
        else:
            fields = (
                "<input name='username' type='text'><input name='password' type='password'>"
                "<button type='submit'>Sign in</button>"
            )
        return _page(
            "Example ID",
            f"<h1>Sign in to Example ID</h1>{alert}"
            f"<form method='post' action='/oauth/authorize'>{hidden}{fields}</form>",
        )

    def _idp_handler(self):
        site = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                parts = urlsplit(self.path)
                query = parse_qs(parts.query)
                if parts.path == "/oauth/authorize":
                    site._send(
                        self,
                        site._idp_form(
                            query.get("redirect_uri", [""])[0],
                            query.get("state", [""])[0],
                            step=1,
                            identifier="",
                            error=False,
                        ),
                    )
                elif parts.path == "/login":  # not an SSO endpoint: must never receive credentials
                    site._send(
                        self,
                        _page(
                            "Sign in",
                            "<h1>Sign in</h1><form method='post' action='/login'>"
                            "<input name='username' type='text'><input name='password' type='password'>"
                            "<button type='submit'>Sign in</button></form>",
                        ),
                    )
                else:
                    site._send(self, _page("Not found", "<h1>404</h1>"), status=404)

            def do_POST(self):
                path = urlsplit(self.path).path
                form = site._form(self)
                if path == "/login":
                    site.evil_posts += 1
                    site._send(self, _page("Sign in", "<h1>Sign in</h1><p>Thanks.</p>"))
                    return
                redirect_uri, state = form.get("redirect_uri", ""), form.get("state", "")
                if site.idp_flow == "identifier-first" and form.get("step") == "1":
                    site._send(
                        self,
                        site._idp_form(
                            redirect_uri,
                            state,
                            step=2,
                            identifier=form.get("identifier", ""),
                            error=False,
                        ),
                    )
                    return
                site.idp_posts += 1
                username = form.get("identifier") or form.get("username")
                if username == USER and form.get("password") == PASSWORD:
                    code = secrets.token_hex(6)
                    site.codes.add(code)
                    if site.idp_postback:
                        site._send(
                            self,
                            _page(
                                "Redirecting",
                                f"<form method='post' action='{redirect_uri}'>"
                                f"<input type='hidden' name='code' value='{code}'>"
                                f"<input type='hidden' name='state' value='{state}'></form>"
                                "<script>document.forms[0].submit()</script>",
                            ),
                        )
                    else:
                        site._redirect(self, f"{redirect_uri}?code={code}&state={state}")
                else:
                    site._send(
                        self,
                        site._idp_form(
                            redirect_uri, state, step=2, identifier=username or "", error=True
                        ),
                    )

        return Handler
