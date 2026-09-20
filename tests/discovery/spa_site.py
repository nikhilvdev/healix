"""A single-page app whose navigation is only script, and a server that records what it is sent.

Every path that is not ``/api/...`` gets the same HTML shell, as a real SPA host does, so a route
found by clicking can also be loaded directly. Routes change with ``history.pushState`` (``/``
site) or the URL hash (``/hash``).

The server records every request in ``hits``. Tests use it to prove the dangerous buttons were never
pressed: nothing may ever arrive here with a method other than GET.
"""

from __future__ import annotations

import http.server
import json
import threading

SHELL = """<!doctype html>
<html><head><title>SPA</title></head><body><div id="app"></div><script>
const EXTERNAL = %(external)s;
const post = (path) => fetch(path, { method: 'POST', body: '{}' });
function go(path) { history.pushState({}, '', path); render(); }
function hashTo(path) { location.hash = '#' + path; }
const nav = `
  <nav>
    <button id="nav-orders" onclick="go('/orders')">Orders</button>
    <div id="nav-settings" role="button" tabindex="0" onclick="go('/settings')">Settings</div>
    <a id="nav-reports" href="#" onclick="go('/reports'); return false;">Reports</a>
    <span id="nav-help" role="tab" onclick="go('/help')">Help</span>
  </nav>`;
const danger = `
  <button id="delete-all" onclick="post('/api/delete')">Delete everything</button>
  <button id="icon-out" aria-label="Sign out" onclick="post('/api/logout')"><i></i></button>
  <button id="trash" class="btn btn-danger" onclick="post('/api/trash')"><i></i></button>
  <button id="refresh" onclick="post('/api/refresh')">Refresh</button>
  <form id="profile" onsubmit="post('/api/form'); return false;">
    <input name="q"><button id="details">Details</button>
  </form>`;
const pages = {
  '/': () => '<h1>Home</h1>' + nav + danger
    + `<button id="away" onclick="location.href = EXTERNAL">Elsewhere</button>`,
  '/orders': () => '<h1>Orders</h1>'
    + [1, 2, 3, 4, 5].map((n) => `<button class="edit" onclick="go('/orders/${n}')">Edit</button>`).join('')
    + `<button id="new-order" onclick="go('/orders/new')">New order</button>`,
  '/orders/new': () => '<h1>New order</h1>'
    + `<button id="place" onclick="post('/api/order'); go('/orders/placed')">Place order</button>`,
  '/settings': () => `<h1>Settings</h1><button id="to-billing" onclick="go('/billing')">Billing</button>`,
  '/reports': () => '<h1>Reports</h1>',
  '/help': () => '<h1>Help</h1>',
  '/billing': () => '<h1>Billing</h1>',
  '/many': () => '<h1>Many</h1>'
    + Array.from({ length: 12 }, (_, i) => `<button onclick="go('/many/${i + 1}')">Section ${i + 1}</button>`).join(''),
  '/hash': () => '<h1>Hash</h1>' + `<button id="team" onclick="hashTo('/team')">Team</button>`
    + `<button id="admins" onclick="hashTo('/admins')">Admins</button>`,
};
function render() {
  const path = location.pathname === '/hash' && location.hash.startsWith('#/')
    ? '/hash' + location.hash.slice(1) : location.pathname;
  const page = pages[path] || (() => '<h1>Not found: ' + path + '</h1>');
  document.getElementById('app').innerHTML = page();
}
window.addEventListener('popstate', render);
window.addEventListener('hashchange', render);
render();
</script></body></html>"""


class SpaSite:
    def __init__(self, external: str = "http://127.0.0.1:1/") -> None:
        self.hits: list[tuple[str, str]] = []
        site = self
        shell = (SHELL % {"external": json.dumps(external)}).encode()

        class Handler(http.server.BaseHTTPRequestHandler):
            def _record(self) -> None:
                site.hits.append((self.command, self.path.split("?")[0]))

            def do_GET(self) -> None:
                self._record()
                if self.path.startswith("/api/"):
                    self.send_response(200)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(shell)))
                self.end_headers()
                self.wfile.write(shell)

            def do_POST(self) -> None:
                self._record()
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args: object) -> None:
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"

    @property
    def writes(self) -> list[tuple[str, str]]:
        """Everything the server was sent that was not a plain GET."""
        return [hit for hit in self.hits if hit[0] != "GET"]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
