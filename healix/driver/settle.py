"""Waiting for a page to stop changing, the same way on every backend.

Nothing here imports a browser library. A backend supplies a callable that reads a short
"activity" string from the page; ``wait_until_quiet`` watches it. The page is quiet once the string
has not changed for a while.

Playwright can wait for network idle by itself, but a page that renders from a timer, or after a
script it started itself, can pass network idle and still be empty. WebDriver has no network idle
at all. Watching the page's own resources and elements covers both, at the cost of being a
heuristic: a request that is still in flight is invisible to it.
"""

from __future__ import annotations

import time
from collections.abc import Callable

# A JavaScript expression: the document's load state, how many resources it has fetched, and how
# many elements it has. Any change means the page is still doing something.
ACTIVITY_EXPRESSION = (
    "document.readyState + '|' + performance.getEntriesByType('resource').length"
    " + '|' + document.getElementsByTagName('*').length"
)

POLL_SECONDS = 0.05


def wait_until_quiet(
    read_activity: Callable[[], str],
    *,
    quiet_s: float,
    deadline: float,
    initial: str | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Poll ``read_activity`` until it stays the same for ``quiet_s`` seconds.

    ``deadline`` is an absolute ``clock()`` time; a busy page (polling, websockets) never goes
    quiet, so the wait always ends there. ``initial`` is a reading taken just before the call,
    which saves a second read. Returns ``True`` if the page went quiet, ``False`` if it ran out
    of time.
    """
    last = read_activity() if initial is None else initial
    since = clock()
    while clock() < deadline:
        sleep(POLL_SECONDS)
        state = read_activity()
        if state != last:
            last, since = state, clock()
        elif clock() - since >= quiet_s:
            return True
    return False
