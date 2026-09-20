"""A tiny server for pages that change after they load, and answer slowly."""

from __future__ import annotations

import http.server
import threading
import time

PAGES = {
    # Content that arrives from a request that takes a moment to answer.
    "/late": (
        "<!doctype html><body><h1>Late</h1><script>"
        "fetch('/data').then(r => r.text()).then(t => {"
        "  document.body.insertAdjacentHTML('beforeend', '<p id=\"late\">' + t + '</p>'); });"
        "</script></body>"
    ),
    # The same, but the answer is slow enough that no realistic quiet window covers it, however
    # slow the machine running the test is.
    "/slow": (
        "<!doctype html><body><h1>Slow</h1><script>"
        "fetch('/slow-data').then(r => r.text()).then(t => {"
        "  document.body.insertAdjacentHTML('beforeend', '<p id=\"late\">' + t + '</p>'); });"
        "</script></body>"
    ),
    # Content rendered from a timer, with no request at all: network idle cannot see it coming. The
    # delay is well past the ~0.5 s network idle takes, and well inside the quiet window the test
    # asks for, so neither a slow nor a fast machine changes the outcome.
    "/timer": (
        "<!doctype html><body><h1>Timer</h1><script>setTimeout(() => {"
        "  document.body.insertAdjacentHTML('beforeend', '<p id=\"rendered\">done</p>'); }, 2000);"
        "</script></body>"
    ),
    # A page that keeps making requests for as long as it is open.
    "/chatty": (
        "<!doctype html><body><h1>Chatty</h1><script>"
        "setInterval(() => fetch('/tick'), 80);</script></body>"
    ),
    # Controls that render well after load — later than a driver waits for the page to go quiet.
    "/appears": (
        "<!doctype html><body><h1>Appears</h1><script>window.__clicks = []; setTimeout(() => {"
        "  document.body.insertAdjacentHTML('beforeend', '<input id=later-input>"
        "<button id=later onclick=window.__clicks.push(1)>Go</button>'); }, 1500);</script></body>"
    ),
    "/twins": (
        "<!doctype html><body><button class='twin'>One</button>"
        "<button class='twin'>Two</button></body>"
    ),
    "/alert": (
        "<!doctype html><body><script>alert('hello');</script><p id='after'>after</p></body>"
    ),
    "/data": "ready",
    "/slow-data": "ready",
    "/tick": "ok",
}
DELAYS = {"/data": 0.15, "/slow-data": 1.2}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path not in PAGES:
            self.send_error(404)
            return
        time.sleep(DELAYS.get(path, 0))
        body = PAGES[path].encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class DynamicSite:
    def __init__(self) -> None:
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
