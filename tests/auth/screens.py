"""A scripted screen-by-screen ``Driver`` for testing login flows without a browser."""

from __future__ import annotations

from dataclasses import dataclass, field

from healix.driver.base import Driver, Element
from tests.elements import el


@dataclass
class Screen:
    url: str
    elements: list[Element] = field(default_factory=list)


class ScreenDriver(Driver):
    """Shows one ``Screen`` at a time. A click moves to another screen per ``on_click``.

    ``writes`` records every ``(css_selector, text, url)`` typed, so tests can assert exactly
    what was entered where. ``unreadable_reads`` makes the next N ``get_elements`` calls raise,
    the way a page does mid-navigation.
    """

    def __init__(self, screens, start, on_click, unreadable_reads=0):
        self.screens = screens
        self.current = start
        self.on_click = on_click
        self.unreadable_reads = unreadable_reads
        self.writes: list[tuple[str, str, str]] = []
        self.clicks: list[str] = []
        self.shots = 0

    @property
    def current_url(self):
        return self.screens[self.current].url

    def get_elements(self, *, iframe_traversal=True):
        if self.unreadable_reads:
            self.unreadable_reads -= 1
            raise RuntimeError("Execution context was destroyed")
        return list(self.screens[self.current].elements)

    def write(self, text, into):
        self.writes.append((into.css_selector, text, self.current_url))

    def click(self, target):
        self.clicks.append(target.css_selector)
        self.current = self.on_click.get((self.current, target.css_selector), self.current)

    def screenshot(self):
        self.shots += 1
        return b"\x89PNG-fake"

    def navigate(self, url): ...
    def find(self, fingerprint):
        raise NotImplementedError

    def get_frames(self):
        return []


def password_form(prefix="", *, error=None):
    """A username + password + submit form with stable selectors (``#u``, ``#p``, ``#go``)."""
    page = [
        el("form", sel=f"{prefix}#f"),
        el("input", sel=f"{prefix}#f > #u", name="username", type="text"),
        el("input", sel=f"{prefix}#f > #p", name="password", type="password"),
        el("button", sel=f"{prefix}#f > #go", type="submit", text="Sign in"),
    ]
    if error:
        page.append(el("div", sel=f"{prefix}#err", text=error))
    return page


def dashboard():
    return [
        el("h1", sel="h", text="Dashboard"),
        *[el("div", sel=f"k{i}", classes=["kpi-card"]) for i in range(4)],
        *[el("canvas", sel=f"c{i}", classes=["chart"]) for i in range(2)],
    ]
