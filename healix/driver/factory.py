"""Create the ``Driver`` for a configured backend."""

from __future__ import annotations

from healix.driver.base import Driver

BACKENDS = ("playwright", "selenium")


class BackendUnavailableError(RuntimeError):
    """The requested backend can't be used (not installed, or not implemented yet)."""


def create_driver(backend: str = "playwright", *, headless: bool = True) -> Driver:
    """A new, not-yet-started driver for ``backend``."""
    if backend == "playwright":
        try:
            from healix.driver.playwright_adapter import PlaywrightDriverAdapter
        except ImportError as exc:
            raise BackendUnavailableError(
                "the playwright backend needs Playwright: "
                "pip install 'healix[playwright]' && playwright install chromium"
            ) from exc
        return PlaywrightDriverAdapter(headless=headless)
    if backend == "selenium":
        raise BackendUnavailableError("the selenium backend is not implemented yet")
    raise ValueError(f"unknown backend {backend!r}; expected one of {BACKENDS}")
