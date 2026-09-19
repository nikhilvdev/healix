"""A login page that changes between versions, served from one URL, for real-browser tests.

``set(version)`` swaps what ``/login`` serves, like a deploy, so a fingerprint recorded against
one version is looked up against another at the *same page URL*.
"""

from __future__ import annotations

import http.server
import threading

_SCRIPT = """
<script>
  window.__typed = {}; window.__submitted = 0;
  document.addEventListener('input', e => { window.__typed[e.target.type] = e.target.value; });
  document.addEventListener('submit', e => { e.preventDefault(); window.__submitted++; });
</script>
"""


def _page(body: str, title: str = "Sign in") -> str:
    return (
        f"<!doctype html><html><head><meta charset=utf-8><title>{title}</title>"
        "<style>body{margin:0;font:14px sans-serif}main{padding:16px}</style></head>"
        f"<body>{body}{_SCRIPT}</body></html>"
    )


VERSIONS: dict[str, str] = {
    # The baseline: test ids, ids, names, aria-labels, classes — everything a locator could use.
    "v1": _page(
        """<main><h1>Sign in</h1>
        <form id="login-form" action="/session" method="post">
          <label for="user-4471">Username</label>
          <input id="user-4471" name="username" class="form-control input-lg" type="text"
                 data-testid="login-username" aria-label="Username">
          <label for="pw-1">Password</label>
          <input id="pw-1" name="password" class="form-control input-lg" type="password"
                 data-testid="login-password" aria-label="Password">
          <button id="submit-btn" class="btn btn-primary" type="submit"
                  data-testid="login-submit">Sign in</button>
        </form></main>"""
    ),
    # Only the generated ids changed. Test ids are intact, so nothing needs healing.
    "ids-only": _page(
        """<main><h1>Sign in</h1>
        <form id="login-form" action="/session" method="post">
          <label for="user-9032">Username</label>
          <input id="user-9032" name="username" class="form-control input-lg" type="text"
                 data-testid="login-username" aria-label="Username">
          <label for="pw-7">Password</label>
          <input id="pw-7" name="password" class="form-control input-lg" type="password"
                 data-testid="login-password" aria-label="Password">
          <button id="submit-3" class="btn btn-primary" type="submit"
                  data-testid="login-submit">Sign in</button>
        </form></main>"""
    ),
    # Routine churn: regenerated ids, hashed class names, test ids stripped. Names and aria-labels
    # survive, so a lower-priority locator still finds each field.
    "churn": _page(
        """<main><h1>Sign in</h1>
        <form class="login__form" action="/session" method="post">
          <label for="f_8d2a">Username</label>
          <input id="f_8d2a" name="username" class="css-1k2j3 css-9x" type="text"
                 aria-label="Username">
          <label for="f_77ce">Password</label>
          <input id="f_77ce" name="password" class="css-1k2j3 css-9x" type="password"
                 aria-label="Password">
          <button id="btn_31" class="css-btn" type="submit">Sign in</button>
        </form></main>"""
    ),
    # A heavy refactor: new wrappers, new ids and classes, no test ids, names or aria-labels.
    # Only the labels, input types, and rough position remain: the fields must be *scored*.
    "refactor": _page(
        """<div class="page"><div class="card"><div class="card-body"><h1>Sign in</h1>
        <form action="/session" method="post">
          <div class="field"><label>Username</label>
            <input id="x1" class="c1" type="text" autocomplete="username"></div>
          <div class="field"><label>Password</label>
            <input id="x2" class="c1" type="password"></div>
          <button id="go" class="c2" type="submit">Sign in</button>
        </form></div></div></div>"""
    ),
    # The username field is still findable by name, but it now asks for something else entirely.
    "meaning-changed": _page(
        """<main><h1>Sign in</h1>
        <form id="login-form" action="/session" method="post">
          <label for="a1">Email address or mobile number</label>
          <input id="a1" name="username" class="form-control input-lg" type="text"
                 aria-label="Username">
          <label for="b1">Password</label>
          <input id="b1" name="password" class="form-control input-lg" type="password"
                 aria-label="Password">
          <button id="c1" class="btn btn-primary" type="submit">Sign in</button>
        </form></main>"""
    ),
    # The form is gone; only an unrelated search box is left.
    "form-removed": _page(
        """<header><input type="search" name="q" placeholder="Search the site"
                          class="search" id="site-search"></header>
        <main><h1>Down for maintenance</h1><p>Please come back soon.</p></main>""",
        title="Maintenance",
    ),
    # Two identical, unlabelled text boxes: nothing to tell them apart.
    "ambiguous": _page(
        """<main><h1>Sign in</h1><form>
          <input type="text" class="f"><input type="text" class="f">
          <button type="submit">Sign in</button></form></main>"""
    ),
    # The UI is drawn on a canvas: no DOM controls at all.
    "canvas": (
        "<!doctype html><html><head><meta charset=utf-8><title>Canvas app</title>"
        "<style>html,body{margin:0}canvas{display:block}</style></head><body>"
        '<canvas id="app" width="1200" height="700"></canvas>'
        '<script>const c=document.getElementById("app").getContext("2d");'
        'c.fillStyle="#eee";c.fillRect(0,0,1200,700);c.fillStyle="#333";'
        'c.fillText("Username: [____]   Password: [____]   [Sign in]",40,60);'
        "</script></body></html>"
    ),
}


class SwitchSite:
    """Serves ``/login`` from the current version. ``url`` is stable across versions."""

    def __init__(self, version: str = "v1") -> None:
        self.version = version
        site = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = VERSIONS[site.version].encode() if self.path.startswith("/login") else b""
                self.send_response(200 if body else 404)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/login"

    def set(self, version: str) -> None:
        assert version in VERSIONS, version
        self.version = version

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
