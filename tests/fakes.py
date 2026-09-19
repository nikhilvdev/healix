"""A scripted ``Driver`` for tests that need a site without a browser."""

from __future__ import annotations

from healix.driver.base import Driver, Element


class Interrupt(BaseException):
    """Stands in for KeyboardInterrupt (a BaseException that must not be swallowed)."""


class FakeSiteDriver(Driver):
    """``pages`` maps a normalized URL to ``(links, structure_key)``.

    Distinct ``structure_key`` values give pages distinct structural hashes.
    """

    def __init__(self, pages, *, broken=(), interrupt_on=None, interrupt_on_nth=None):
        self.pages = pages
        self.broken = set(broken)
        self.interrupt_on = interrupt_on
        self.interrupt_on_nth = interrupt_on_nth  # 1-based navigation count
        self.navigations: list[str] = []
        self.started = 0
        self.closed = 0
        self._current = ""

    def start(self):
        self.started += 1

    def close(self):
        self.closed += 1

    @property
    def current_url(self):
        return self._current

    def navigate(self, url):
        self.navigations.append(url)
        if url == self.interrupt_on or len(self.navigations) == self.interrupt_on_nth:
            raise Interrupt
        if url in self.broken:
            raise TimeoutError(f"timeout loading {url}")
        self._current = url

    def get_elements(self, *, iframe_traversal=True):
        links, structure = self.pages[self._current]
        anchors = [
            Element.from_dict(
                {
                    "tag": "a",
                    "attributes": {"href": href},
                    "computed": {"visible": True, "href": href},
                    "css_selector": f"a:nth-of-type({i})",
                },
                iframe_path=["main"],
            )
            for i, href in enumerate(links, 1)
        ]
        marker = Element.from_dict(
            {
                "tag": "input",
                "name": structure,
                "computed": {"visible": True},
                "css_selector": "input",
            },
            iframe_path=["main"],
        )
        return [*anchors, marker]

    def find(self, fingerprint):
        raise NotImplementedError

    def click(self, target):
        raise NotImplementedError

    def write(self, text, into):
        raise NotImplementedError

    def get_frames(self):
        return []

    def screenshot(self):
        return b""


def small_site():
    """home -> a, b (each with its own structure)."""
    return {
        "https://e.com/": (["https://e.com/a", "https://e.com/b"], "home"),
        "https://e.com/a": (["https://e.com/"], "a"),
        "https://e.com/b": ([], "b"),
    }
