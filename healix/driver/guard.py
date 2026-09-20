"""A best-effort guard that stops a page from sending data while Healix clicks around in it.

Click-through discovery clicks elements it cannot fully vouch for. The main protection is choosing
what to click (``healix.discovery.clicks``); this is a second layer for what gets through. Injected
into a page, it makes the ways a script normally sends data do nothing and count the attempt:

* ``fetch`` and ``XMLHttpRequest`` with any method but GET, HEAD or OPTIONS;
* ``navigator.sendBeacon``;
* form submission (``submit``, ``requestSubmit`` and the ``submit`` event).

What it does **not** stop: a GET request that has a side effect, WebSocket messages, a request
made from a frame it was not injected into (cross-origin frames), and a page that saved its own
reference to ``fetch`` before the guard ran. It is a safety net, not a sandbox.

Nothing here imports a browser library: the adapters run ``GUARD_JS`` and ``BLOCKED_JS``.
"""

from __future__ import annotations

# Function expression run in every same-origin frame after each navigation. Idempotent: running it
# again in a frame that already has the guard changes nothing.
GUARD_JS = """() => {
  if (window.__healixGuard) return;
  const state = { blocked: 0 };
  Object.defineProperty(window, '__healixGuard', { value: state });
  const SAFE = ['GET', 'HEAD', 'OPTIONS'];
  const safe = (method) => SAFE.indexOf(String(method || 'GET').toUpperCase()) >= 0;

  const realFetch = window.fetch;
  if (realFetch) {
    window.fetch = function (input, init) {
      const method = (init && init.method) || (input && input.method) || 'GET';
      if (!safe(method)) {
        state.blocked++;
        return Promise.reject(new TypeError('blocked by healix: ' + method + ' request'));
      }
      return realFetch.apply(this, arguments);
    };
  }

  const realOpen = XMLHttpRequest.prototype.open;
  const realSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method) {
    this.__healixUnsafe = !safe(method);
    return realOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function () {
    if (this.__healixUnsafe) {
      state.blocked++;
      throw new DOMException('blocked by healix', 'NetworkError');
    }
    return realSend.apply(this, arguments);
  };

  if (navigator.sendBeacon) navigator.sendBeacon = function () { state.blocked++; return false; };

  document.addEventListener('submit', (event) => {
    event.preventDefault();
    event.stopImmediatePropagation();
    state.blocked++;
  }, true);
  HTMLFormElement.prototype.submit = function () { state.blocked++; };
  HTMLFormElement.prototype.requestSubmit = function () { state.blocked++; };
}"""

# Function expression: how many requests the guard has stopped in the current document.
BLOCKED_JS = "() => (window.__healixGuard ? window.__healixGuard.blocked : 0)"
