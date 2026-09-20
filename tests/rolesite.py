"""A small site with two kinds of user, for multi-role runs through a real browser.

* ``/`` and ``/login`` are public;
* ``/dashboard`` and ``/reports`` need a session, and show an administrator more (a link to
  ``/admin`` and an export button);
* ``/admin`` is for administrators only and is linked from nowhere else.

The server counts every credential submission and records the role of the session behind each
request, so tests can prove which password went where, and that one role's session never leaked
into the next role's crawl.
"""

from __future__ import annotations

import http.server
import secrets
import threading
from urllib.parse import parse_qs

USERS = {
    "admin": ("admin@example.com", "Adm1n-PASSWORD-UNIQUE"),
    "standard": ("sam@example.com", "Stand4rd-PASSWORD-UNIQUE"),
}


def _page(title: str, body: str, status: int = 200) -> tuple[int, bytes]:
    html = (
        f"<!doctype html><html><head><meta charset=utf-8><title>{title}</title></head>"
        f"<body>{body}</body></html>"
    )
    return status, html.encode()


class RoleSite:
    def __init__(self) -> None:
        self.login_posts: list[str] = []  # the username of every credential submission
        self.hits: list[tuple[str | None, str]] = []  # (role of the session, path)
        self._sessions: dict[str, str] = {}
        site = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _role(self) -> str | None:
                cookie = self.headers.get("Cookie", "")
                for part in cookie.split(";"):
                    name, _, value = part.strip().partition("=")
                    if name == "sid":
                        return site._sessions.get(value)
                return None

            def _send(
                self, status: int, body: bytes, headers: dict[str, str] | None = None
            ) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def _redirect(self, where: str, headers: dict[str, str] | None = None) -> None:
                self._send(302, b"", {"Location": where, **(headers or {})})

            def do_GET(self) -> None:
                path = self.path.split("?")[0]
                role = self._role()
                site.hits.append((role, path))
                if path == "/":
                    self._send(
                        *_page(
                            "Home",
                            (
                                "<h1>Home</h1><a href='/login'>Sign in</a> "
                                "<a href='/dashboard'>Dashboard</a> <a href='/reports'>Reports</a>"
                            ),
                        )
                    )
                elif path == "/login":
                    self._send(*_page("Sign in", _login_form()))
                elif path in ("/dashboard", "/reports", "/admin"):
                    if role is None:
                        self._redirect("/login")
                    elif path == "/admin" and role != "admin":
                        self._send(*_page("Forbidden", "<h1>Forbidden</h1>", 403))
                    else:
                        self._send(*_page(path.strip("/").title(), _body(path, role)))
                else:
                    self._send(*_page("Not found", "<h1>Not found</h1>", 404))

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                form = parse_qs(self.rfile.read(length).decode())
                username = (form.get("username") or [""])[0]
                password = (form.get("password") or [""])[0]
                site.login_posts.append(username)
                for role, (user, secret) in USERS.items():
                    if (username, password) == (user, secret):
                        sid = secrets.token_hex(8)
                        site._sessions[sid] = role
                        self._redirect("/dashboard", {"Set-Cookie": f"sid={sid}; Path=/"})
                        return
                self._send(
                    *_page(
                        "Sign in", "<p role='alert'>Invalid credentials</p>" + _login_form(), 401
                    )
                )

            def log_message(self, *args: object) -> None:
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _login_form() -> str:
    return (
        "<h1>Sign in</h1><form method='post' action='/login'>"
        "<label>Email <input name='username' type='text'></label>"
        "<label>Password <input name='password' type='password'></label>"
        "<button type='submit'>Sign in</button></form>"
    )


def _body(path: str, role: str) -> str:
    admin = role == "admin"
    if path == "/dashboard":
        extra = " <a href='/admin'>Administration</a>" if admin else ""
        return f"<h1>Dashboard</h1><a href='/reports'>Reports</a>{extra}"
    if path == "/reports":
        export = "<button data-testid='export-csv'>Export CSV</button>" if admin else ""
        return f"<h1>Reports</h1><table><tr><td>Q1</td></tr></table>{export}"
    return (
        "<h1>Administration</h1><input name='new-user' type='text'>"
        "<button type='button'>Add</button>"
    )
